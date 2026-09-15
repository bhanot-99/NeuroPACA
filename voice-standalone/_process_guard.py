"""
Must be imported and called as the very first thing in any entry point
that loads both onnxruntime (openwakeword, fastembed) and torch (Kokoro/
RealtimeTTS) in the same process — currently daemon.py and main.py.

Root cause, found and verified live (2026-09-16), not guessed: torch ships
its own private libgomp.so.1 bundled inside its package
(`torch/lib/libgomp.so.1`), separate from the system's. With both
onnxruntime and torch loaded in one process, constructing Kokoro's engine
(specifically inside a torch nn.LSTM layer) reliably hung — reproduced 3
times against the real, unmodified daemon.py (the systemd service stuck
14 real minutes at 0% CPU; direct runs stuck 60+s burning real CPU). Two
earlier attempts — capping thread-count env vars, then also calling
torch.set_num_threads(1) explicitly — both failed against the real entry
point despite fixing simplified reproductions that didn't share the same
full import graph. The actual fix, isolated by testing each hypothesis in
turn against the same unmodified daemon.py: preload ONE canonical libgomp
before either library gets a chance to load its own copy.

LD_PRELOAD can only be set before a process starts, not from within
already-running Python code — so this re-execs the process once,
transparently, with it set. `execve` replaces the process image in place
(same PID), so this is safe under systemd's process tracking, same as
`exec` in a shell script.
"""

import glob
import os
import signal
import sys
import time


def safe_run(argv: list[str], timeout: float = 10.0) -> int:
    """Drop-in-ish replacement for `subprocess.run(argv, check=False)` for
    fire-and-forget external commands (notify-send, aplay, etc.) called
    from a process that has already loaded onnxruntime/torch/ctranslate2
    — i.e. daemon.py, once it's past its startup model loads. Deliberately
    NOT subprocess.run/Popen: those use fork()+exec() on POSIX, and
    fork() in a multi-threaded process is a well-documented hazard (it
    only duplicates the calling thread; if another thread held a lock —
    e.g. malloc's, inside onnxruntime's or numpy's own internal thread
    pools — at that exact instant, the child inherits it permanently
    locked). Found live (2026-09-16): daemon.py's own `_notify()` calling
    plain `subprocess.run(["notify-send", ...])` was exactly this — the
    real, previously-unidentified cause of daemon.py hanging on its very
    first "Ready" notification in testing, even after tts.py's own
    subprocess calls (a different, earlier-suspected cause) were already
    fixed the same way. Uses os.posix_spawn (never forks) and enforces a
    hard timeout, so a call here can never block the daemon forever —
    worst case this raises TimeoutError after `timeout` seconds and the
    caller decides what a failed/stuck external command means, same
    principle as tts.py's _spawn_worker."""
    # posix_spawnp, not posix_spawn: argv[0] here is typically a bare
    # command name ("notify-send") that needs a PATH search, same as
    # subprocess.run's default behavior — posix_spawn (no 'p') requires a
    # full path and would fail closed on exactly the calls this exists for.
    pid = os.posix_spawnp(argv[0], argv, os.environ.copy())
    deadline = time.monotonic() + timeout
    while True:
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid == pid:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise TimeoutError(f"safe_run timed out after {timeout}s: {argv}")
        time.sleep(0.05)


def ensure_libgomp_preloaded() -> None:
    if os.environ.get("_VOICE_STANDALONE_LIBGOMP_FIXED") == "1":
        return
    torch_libgomp = glob.glob(
        os.path.join(os.path.dirname(sys.executable), "..", "lib", "python3.*",
                      "site-packages", "torch", "lib", "libgomp.so*")
    )
    if not torch_libgomp:
        # torch isn't installed yet in this environment — fall through so
        # the later `import torch` (inside tts.py) fails with a clear
        # error instead of this silently masking a real setup problem.
        return
    env = os.environ.copy()
    existing_preload = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = torch_libgomp[0] + (":" + existing_preload if existing_preload else "")
    env["_VOICE_STANDALONE_LIBGOMP_FIXED"] = "1"
    # Defense in depth — cheap to also cap thread counts, in case a future
    # dependency change reintroduces separate OpenMP runtimes on top of
    # this. Not the fix that was actually verified to matter on its own
    # (tested and found insufficient alone against this exact bug), but
    # harmless to keep alongside the real fix above.
    for threads_var in (
        "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "CT2_NUM_THREADS",
    ):
        env.setdefault(threads_var, "1")
    os.execve(sys.executable, [sys.executable] + sys.argv, env)
