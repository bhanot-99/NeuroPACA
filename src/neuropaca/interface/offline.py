"""L9 · the three verbs that do **not** go over the socket (B9/BL-7).

Every other `neuropaca` verb is a thin client: parse, send one JSONL request to
the daemon, render one JSONL response (`interface/cli.py`). These three cannot
be, and the exception is deliberate:

- **`doctor`** exists precisely for the case where the daemon will *not* start.
  Routing it through the L9 socket would make the one diagnostic tool useless in
  the one situation it is for. It therefore reads `Config` and `data/` directly
  and never opens the socket except to *report* on it.
- **`panic`** must work when the daemon is wedged and not answering its socket,
  so it signals the process rather than asking it politely.
- **`export`** reads the graph file; asking a possibly-dead daemon to dump a
  file that is sitting on disk would add a failure mode for nothing.

This module is the *only* place in L9 that touches `data/` directly. It reads
config and files and imports no other layer — `rules.md §0` is intact.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neuropaca.core.config import Config
from neuropaca.core.errors import ConfigError
from neuropaca.core.graph_memory import graph_schema_version

_DEFAULT_CONFIG_PATH = "neuropaca.toml"

# `panic` is irreversible, so the confirmation is a typed word rather than a
# y/n keypress — the same reasoning as D-14's dangerous-action prompt.
_PANIC_WORD = "PANIC"

OFFLINE_VERBS = ("doctor", "export", "panic")


def _config_path() -> str:
    return os.environ.get("NEUROPACA_CONFIG", _DEFAULT_CONFIG_PATH)


def _load_config() -> tuple[Config | None, str | None]:
    """Return (config, error). `doctor` must report a broken config, not die of it."""
    try:
        return Config.from_file(_config_path()), None
    except ConfigError as exc:
        return None, str(exc)
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _socket_path() -> str:
    from neuropaca.interface.layer import default_socket_path

    return os.environ.get("NEUROPACA_SOCKET") or str(default_socket_path())


def _daemon_pid(socket_path: str) -> int | None:
    """PID of whatever is listening on the L9 socket, via SO_PEERCRED.

    Asking the kernel who is on the other end of the socket beats scanning the
    process table: it cannot match a stale process, an editor with the name in a
    buffer, or a second checkout of the repo.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(socket_path)
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", raw)
        return pid or None
    except (OSError, struct.error):
        return None
    finally:
        sock.close()


def _size(path: Path) -> str:
    try:
        return f"{path.stat().st_size / 1024:.1f} KiB"
    except OSError:
        return "?"


def _graph_report(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Read the graph the way the daemon would; return (rows, problems)."""
    if not path.exists():
        return [("graph", f"absent at {path} — a first run seeds 11 hubs")], []
    try:
        payload = json.loads(path.read_text("utf-8"))
    except OSError as exc:
        return [("graph", f"UNREADABLE at {path}: {exc}")], ["graph unreadable"]
    except ValueError as exc:
        return [("graph", f"CORRUPT at {path}: not valid JSON ({exc})")], ["graph corrupt"]
    if not isinstance(payload, dict):
        return (
            [("graph", f"CORRUPT at {path}: top level is {type(payload).__name__}")],
            ["graph corrupt"],
        )

    rows = [
        (
            "graph",
            f"{path} · {len(payload.get('nodes', []))} nodes, "
            f"{len(payload.get('edges', []))} edges",
        )
    ]
    problems: list[str] = []
    on_disk = payload.get("schema_version", 1)
    current = graph_schema_version()
    if isinstance(on_disk, bool) or not isinstance(on_disk, int):
        rows.append(("schema", f"INVALID schema_version {on_disk!r}"))
        problems.append("invalid schema_version")
    elif on_disk > current:
        rows.append(
            (
                "schema",
                f"v{on_disk} on disk > v{current} supported — this build refuses to "
                "load it; upgrade NeuroPACA",
            )
        )
        problems.append("graph is from a newer build")
    else:
        rows.append(("schema", f"v{on_disk} (this build reads up to v{current})"))
    return rows, problems


def _table(rows: list[tuple[str, str]]) -> Any:
    from rich.table import Table

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(overflow="fold")
    for label, text in rows:
        table.add_row(label, text)
    return table


def doctor(argv: list[str]) -> int:
    """Offline health report. Exit 0 if nothing is wrong, 1 otherwise (B9/BL-7)."""
    from rich.console import Console

    console = Console()
    rows: list[tuple[str, str]] = []
    problems: list[str] = []

    config, config_error = _load_config()
    if config is None:
        console.print(_table([("config", f"INVALID {_config_path()}: {config_error}")]))
        console.print("[red]✕[/red] config is unreadable — nothing else can be checked.")
        return 1
    rows.append(("config", f"{_config_path()} · ok"))

    graph_path = Path(config.graph_db_path)
    graph_rows, graph_problems = _graph_report(graph_path)
    rows.extend(graph_rows)
    problems.extend(graph_problems)

    # Quarantined graphs are the fingerprint of a BL-2 boot recovery.
    corrupt = sorted(graph_path.parent.glob(f"{graph_path.name}.corrupt.*"))
    if corrupt:
        rows.append(
            (
                "recovery",
                f"{len(corrupt)} quarantined graph(s) — the daemon booted on a reseeded "
                f"graph at least once; newest: {corrupt[-1].name}",
            )
        )
        problems.append("a previous boot recovered from an unreadable graph")

    log_path = Path(config.log_file_path)
    if log_path.exists():
        rows.append(("log", f"{log_path} · {_size(log_path)}"))
    else:
        suffix = "" if config.log_to_file else " (log_to_file is off)"
        rows.append(("log", f"{log_path} · not yet created{suffix}"))

    audit = Path(config.action_log_path)
    rows.append(
        ("audit", f"{audit} · {_size(audit)}" if audit.exists() else f"{audit} · no actions yet")
    )

    quarantine = Path(config.quarantine_path)
    if quarantine.exists():
        held = list(quarantine.iterdir())
        rows.append(
            (
                "quarantine",
                f"{quarantine} · {len(held)} item(s), ttl {config.quarantine_ttl_hours}h",
            )
        )

    sock = _socket_path()
    if not Path(sock).exists():
        rows.append(("daemon", f"not running (no socket at {sock})"))
    else:
        pid = _daemon_pid(sock)
        if pid:
            rows.append(("daemon", f"running · pid {pid} · socket {sock}"))
        else:
            rows.append(("daemon", f"socket {sock} exists but nothing answers — stale"))
            problems.append("stale socket")

    try:
        probe = graph_path.parent if graph_path.parent.exists() else Path(".")
        free_mb = shutil.disk_usage(probe).free / 1024 / 1024
        rows.append(("disk", f"{free_mb:.0f} MiB free for {probe}"))
        if free_mb < 100:
            problems.append("under 100 MiB free — saves will start failing")
    except OSError:
        pass

    console.print(_table(rows))
    if problems:
        console.print(f"[yellow]![/yellow] {len(problems)} problem(s): " + "; ".join(problems))
        return 1
    console.print("[green]✓[/green] nothing wrong found")
    return 0


def export(argv: list[str]) -> int:
    """Dump the graph to a path of the user's choosing (B9/BL-7).

    This is the first code path in the project that deliberately writes graph
    contents *outside* `data/`, so it says so loudly. `rules.md §6` is about the
    daemon never sending data anywhere on its own; a human copying their own
    graph to their own disk is not a violation, but it does move the data out
    from under every guarantee the rest of the system makes about it.
    """
    from rich.console import Console

    console, err = Console(), Console(stderr=True)
    force = "--force" in argv
    positional = [a for a in argv if not a.startswith("-")]
    if len(positional) != 1:
        err.print("usage: neuropaca export <path> [--force]")
        return 2
    dest = Path(positional[0]).expanduser()

    config, config_error = _load_config()
    if config is None:
        err.print(f"[red]✕[/red] config does not load: {config_error}")
        return 1

    source = Path(config.graph_db_path)
    if not source.exists():
        err.print(f"[red]✕[/red] no graph at {source} — nothing to export")
        return 1
    if dest.exists() and not force:
        err.print(f"[red]✕[/red] {dest} exists — pass --force to overwrite")
        return 1

    try:
        payload = json.loads(source.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        err.print(f"[red]✕[/red] cannot read {source}: {exc}")
        err.print("  run `neuropaca doctor` for detail")
        return 1

    wrapped = {
        "exported_at": datetime.now(UTC).isoformat(),
        "schema_version": payload.get("schema_version", 1) if isinstance(payload, dict) else 1,
        "graph": payload,
    }
    try:
        if dest.parent != Path(""):
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(wrapped, indent=2), encoding="utf-8")
        dest.chmod(0o600)
    except OSError as exc:
        err.print(f"[red]✕[/red] cannot write {dest}: {exc}")
        return 1

    nodes = len(payload.get("nodes", [])) if isinstance(payload, dict) else 0
    edges = len(payload.get("edges", [])) if isinstance(payload, dict) else 0
    console.print(f"[green]✓[/green] exported {nodes} nodes / {edges} edges to {dest}")
    console.print(
        "[yellow]![/yellow] [bold]This data has left data/.[/bold] It is a map of how you "
        "work — process names, file paths, activity patterns, and the label of every node "
        "the daemon has ever built."
    )
    console.print(
        "  Nothing in NeuroPACA copies, uploads or syncs it; from here it is yours to "
        "protect. The file is mode 0600. Deleting it is the only way to un-export it."
    )
    return 0


def panic(argv: list[str]) -> int:
    """Kill the daemon and destroy every trace of local state (B9/BL-7).

    Order matters and is not a style choice: the daemon is killed **first**. A
    running daemon holds the graph in memory and re-persists it on the next
    scheduler tick, so wiping `data/` while it lives would delete a file that
    reappears seconds later — a panic button that does not work is worse than
    none. SIGKILL rather than SIGTERM is equally deliberate: SIGTERM runs the
    graceful shutdown path, whose *last act is to save the graph*.
    """
    from rich.console import Console

    console, err = Console(), Console(stderr=True)
    assume_yes = "--yes" in argv or "-y" in argv

    config, config_error = _load_config()
    if config is None:
        err.print(f"[red]✕[/red] config does not load: {config_error}")
        err.print("  cannot tell which directory to wipe — refusing to guess")
        return 1

    data_dir = Path(config.graph_db_path).parent
    if not assume_yes:
        console.print(f"[red bold]This irreversibly destroys everything in {data_dir}[/red bold]")
        console.print(
            "  the graph, the idle cache, the action audit log, the quarantine and the "
            "daemon log — and SIGKILLs the daemon without saving."
        )
        console.print("  There is no undo and no backup. Export first if you want one.")
        try:
            typed = input(f"Type {_PANIC_WORD} to confirm: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\naborted")
            return 1
        if typed != _PANIC_WORD:
            console.print("aborted — nothing was touched")
            return 1

    # 1. Kill first, so nothing rewrites what we are about to delete.
    sock = _socket_path()
    pid = _daemon_pid(sock) if Path(sock).exists() else None
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
            for _ in range(50):  # up to ~1 s for the kernel to reap it
                time.sleep(0.02)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            console.print(f"[green]✓[/green] daemon (pid {pid}) killed")
        except OSError as exc:
            err.print(f"[yellow]![/yellow] could not kill pid {pid}: {exc}")
    else:
        console.print("[dim]no running daemon found[/dim]")

    # 2. Then wipe. Contents, not the directory itself — the daemon and the
    #    systemd unit's ReadWritePaths both expect data/ to exist.
    failures: list[str] = []
    removed = 0
    if data_dir.exists():
        for entry in sorted(data_dir.iterdir()):
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except OSError as exc:
                failures.append(f"{entry.name}: {exc}")

    socket_file = Path(sock)
    if socket_file.exists():
        try:
            socket_file.unlink()
        except OSError as exc:
            failures.append(f"{socket_file}: {exc}")

    if failures:
        err.print(f"[red]✕[/red] {len(failures)} item(s) survived:")
        for line in failures:
            err.print(f"    {line}")
        return 1
    console.print(f"[green]✓[/green] wiped {removed} item(s) from {data_dir}")
    console.print("[dim]NeuroPACA knows nothing about you. A restart seeds an empty graph.[/dim]")
    return 0


def dispatch(argv: list[str]) -> int | None:
    """Handle an offline verb, or return None to let the socket client take it."""
    if not argv:
        return None
    head, rest = argv[0], argv[1:]
    if head == "doctor":
        return doctor(rest)
    if head == "export":
        return export(rest)
    if head == "panic":
        return panic(rest)
    return None


if __name__ == "__main__":
    raise SystemExit(dispatch(sys.argv[1:]) or 0)
