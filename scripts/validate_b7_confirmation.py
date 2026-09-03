#!/usr/bin/env python3
"""B7 · Exit Criteria 3 & 4 — the confirmation gate and the audit log
(phases.md B7, D-14).

Runs the **whole daemon** on the real box — `build_modules` (L2 → L3 → L4 → L5 →
L7 → L6 → L9), the real Unix socket, the real `neuropaca` JSONL protocol — and
drives it exactly as a human at a terminal would. Nothing is mocked but the
model backend.

Three cases, live, with `action_dry_run = False` and both tiers enabled — i.e.
the most permissive configuration the daemon can be put into:

  A. `$!` and nobody answers. The confirmation expires after
     `action_confirmation_timeout_seconds`; the command must not run.
  B. `$!` and the human **denies** over the socket (`confirm --deny`). The
     command must not run.
  C. `$$` and the human **approves**. The command runs, and the daemon's own
     graph was quarantined first (the `$$` backup), so it is restorable.

Then the audit log is checked as a whole: every attempt is an `attempt` +
`result` pair sharing a `request_id`, and no execution appears without a
recorded confirmation.

    uv run python scripts/validate_b7_confirmation.py

Exit 0 = all assertions pass. Exit 1 = any failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from neuropaca.core import logging as np_logging
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.config import Config
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.inference import FakeInferenceBackend
from neuropaca.orchestration.modules import build_modules
from neuropaca.orchestration.orchestrator import NeuroPACAOrchestrator

_CONFIRM_TIMEOUT_S = 5
_SETTLE_S = 1.0


async def _call(sock: str, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(sock)
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), 30)
    finally:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 5)
    return json.loads(line)


async def _await_prompt(sock: str, wait_seconds: float = 5.0) -> dict | None:
    """Poll the socket the way `neuropaca confirmations` does."""
    deadline = time.perf_counter() + wait_seconds
    while time.perf_counter() < deadline:
        resp = await _call(sock, {"op": "confirmations"})
        pending = resp.get("confirmations", [])
        if pending:
            return dict(pending[0])
        await asyncio.sleep(0.1)
    return None


async def _main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="neuropaca-b7-confirm-"))
    work = workdir / "work"
    work.mkdir()
    audit_path = workdir / "actions.jsonl"
    sock = str(workdir / "neuropaca.sock")

    EventBus._reset_for_tests()
    GraphMemory._reset_for_tests()
    BitNetRuntime._reset_for_tests()
    BitNetRuntime.get_instance(FakeInferenceBackend(), FakeInferenceBackend())

    cfg = Config(
        inference_backend="fake",
        graph_db_path=str(workdir / "graph.json"),
        action_log_path=str(audit_path),
        quarantine_path=str(workdir / "quarantine"),
        interface_socket_path=sock,
        watch_paths=[str(work)],
        action_dry_run=False,  # the most permissive configuration possible
        action_enabled_tiers=["safe", "dangerous"],
        action_confirmation_timeout_seconds=_CONFIRM_TIMEOUT_S,
        graph_save_interval_seconds=3600,
        log_level="WARNING",
    )
    np_logging.configure(cfg.log_level)

    orch = NeuroPACAOrchestrator(cfg, module_builder=build_modules)
    await orch.initialize()
    await orch.start()
    Path(cfg.graph_db_path).write_text("{}", encoding="utf-8")  # something to back up

    marker_a = work / "expired.txt"
    marker_b = work / "denied.txt"
    marker_c = work / "approved.txt"

    def _cmd(marker: Path) -> str:
        return f"{sys.executable} -c \"open(r'{marker}','w')\""

    results: dict[str, object] = {}
    try:
        # --- A. nobody answers -------------------------------------------
        t0 = time.perf_counter()
        queued_a = await _call(sock, {"op": "query", "prefix": "$!", "text": _cmd(marker_a)})
        prompt_a = await _await_prompt(sock)
        await asyncio.sleep(_CONFIRM_TIMEOUT_S + _SETTLE_S)
        results["a_elapsed"] = time.perf_counter() - t0

        # --- B. the human denies ------------------------------------------
        await _call(sock, {"op": "query", "prefix": "$!", "text": _cmd(marker_b)})
        prompt_b = await _await_prompt(sock)
        denied = (
            await _call(
                sock,
                {"op": "confirm", "request_id": prompt_b["request_id"], "approved": False},
            )
            if prompt_b
            else {}
        )
        await asyncio.sleep(_SETTLE_S)

        # --- C. the human approves ----------------------------------------
        await _call(sock, {"op": "query", "prefix": "$$", "text": _cmd(marker_c)})
        prompt_c = await _await_prompt(sock)
        approved = (
            await _call(
                sock,
                {"op": "confirm", "request_id": prompt_c["request_id"], "approved": True},
            )
            if prompt_c
            else {}
        )
        await asyncio.sleep(_SETTLE_S)

        daemon_ok = orch.is_running and orch.health_check().ok
    finally:
        await orch.stop()

    lines = [json.loads(x) for x in audit_path.read_text().splitlines() if x]
    attempts = [line for line in lines if line["phase"] == "attempt"]
    outcomes = [line for line in lines if line["phase"] == "result"]
    executed = [line for line in outcomes if line.get("ok") and not line.get("dry_run")]
    stashed = list((workdir / "quarantine").glob("*.bin"))

    print(f"queued (A)      : {queued_a.get('ok')} {queued_a.get('note', '')[:60]}")
    print(f"prompt (A)      : {(prompt_a or {}).get('summary', '(none)')}")
    print(
        f"A expired after : {results['a_elapsed']:.1f} s "
        f"(timeout {_CONFIRM_TIMEOUT_S} s) -> ran: {marker_a.exists()}"
    )
    print(f"B denied        : {denied.get('approved')} -> ran: {marker_b.exists()}")
    print(f"C approved      : {approved.get('approved')} -> ran: {marker_c.exists()}")
    print(f"audit lines     : {len(lines)}  ({len(attempts)} attempts, {len(outcomes)} results)")
    print(f"executed        : {[line['action'] for line in executed]}")
    print(f"quarantined     : {len(stashed)} file(s) (the $$ state backup)")
    print(f"daemon healthy  : {daemon_ok}")

    fails: list[str] = []
    if marker_a.exists():
        fails.append("A: a command ran with no confirmation (expiry must refuse)")
    if marker_b.exists():
        fails.append("B: a denied command ran")
    if not marker_c.exists():
        fails.append("C: an approved command did not run")
    if prompt_a is None or prompt_b is None or prompt_c is None:
        fails.append("a dangerous action did not raise a confirmation prompt")
    if len(attempts) != 3 or len(outcomes) != 3:
        fails.append(
            f"audit incomplete: {len(attempts)} attempts / {len(outcomes)} results, want 3/3"
        )
    for attempt in attempts:
        if not any(o["request_id"] == attempt["request_id"] for o in outcomes):
            fails.append(f"attempt {attempt['request_id']} has no matching result line")
    for line in executed:
        if line.get("confirmed") is not True:
            fails.append(f"{line['action']} executed without a recorded confirmation")
    if len(executed) != 1:
        fails.append(f"{len(executed)} actions executed, expected exactly 1 (the approved one)")
    if not stashed:
        fails.append("$$ ran without quarantining the daemon's state first")
    if not daemon_ok:
        fails.append("daemon was not healthy after the three attempts")

    print()
    if fails:
        for f in fails:
            print(f"  FAIL — {f}")
        print("\n=== RESULT (FAIL) ===")
        return 1
    print(
        "=== RESULT (PASS) — expiry and denial both refused; only the approved "
        f"command ran; {len(lines)} audit lines, 3/3 attempt+result pairs ==="
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
