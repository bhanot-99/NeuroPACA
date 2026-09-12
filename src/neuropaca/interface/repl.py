# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L9 · the interactive ``neuropaca>`` shell — a command menu (B10, re-scoped B12).

Running ``neuropaca`` with no arguments in a terminal lands here. Since B12 the
terminal is a **read-only project guide**: there is no free text, no ``$`` / ``?``
/ ``!`` sigils, no ``chat``. This shell exists only so the predefined verbs are a
little quicker to reach — a line whose first word is not a known verb is a short
error, never a usage dump.

    neuropaca> tell src/neuropaca/interface/layer.py
    neuropaca> tell drive/pressure.py --explain
    neuropaca> overview
    neuropaca> health
    neuropaca> run "pkill -f webpack"
    neuropaca> help        quit

Every line is translated to the argv the ``neuropaca`` console script already
accepts and run through the same ``cli._run_once`` — the client stays thin.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from neuropaca.interface import cli
from neuropaca.interface.layer import default_socket_path
from neuropaca.interface.offline import _daemon_pid as _peer_pid

# Short aliases onto a real verb. Kept tiny on purpose.
_ALIASES = {
    "status": "health",
    "notes": "notifications",
    "pending": "confirmations",
}
_QUIT = {"quit", "exit", "q", ":q"}
_HELP = {"help", "h", "?", "--help", "-h"}
_KNOWN_VERBS = {
    "briefing",
    "confirm",
    "confirmations",
    "doctor",
    "export",
    "health",
    "insights",
    "notifications",
    "overview",
    "panic",
    "repair-graph",
    "run",
    "tell",
    *_ALIASES,
}


class _UnknownVerb(str):
    """Sentinel: the line's first word is not a known verb. ``run()`` prints it."""


def _translate(line: str) -> list[str] | _UnknownVerb:
    """Map one REPL line to the argv the `neuropaca` console script understands,
    or an `_UnknownVerb` describing what was typed."""
    head, _, rest = line.partition(" ")
    head = _ALIASES.get(head, head)
    rest = rest.strip()

    if head not in _KNOWN_VERBS:
        return _UnknownVerb(head)
    if not rest:
        return [head]
    if head == "tell":
        # path (+ optional flags like --explain) — split on spaces, no shlex.
        return [head, *rest.split()]
    if head == "run":
        # keep the command string intact so quotes/spaces survive; peel a
        # leading `--backup` so it is a real flag `cli._parse` can see.
        if rest.split(" ", 1)[0] == "--backup":
            tail = rest[len("--backup") :].strip()
            return [head, "--backup", tail] if tail else [head]
        return [head, rest]
    try:
        return [head, *shlex.split(rest)]
    except ValueError:
        return [head, *rest.split()]


def _socket_path() -> str:
    return os.environ.get("NEUROPACA_SOCKET") or str(default_socket_path())


def _daemon_status() -> str:
    sock = _socket_path()
    if not Path(sock).exists():
        return "[yellow]not running[/yellow] — start `neuropacad` or enable the systemd unit"
    pid = _peer_pid(sock)
    if pid is None:
        return f"[yellow]stale socket[/yellow] at {sock} — nothing is listening"
    return f"[green]running[/green] · pid {pid}"


def _banner() -> None:
    from rich.console import Console

    console = Console()
    console.print("[bold]neuropaca[/bold] · interactive shell")
    console.print(f"  daemon: {_daemon_status()}")
    console.print(
        "  [dim]commands:[/dim] [cyan]tell[/cyan] <path>, [cyan]overview[/cyan], "
        "[cyan]health[/cyan], [cyan]insights[/cyan] …   [cyan]help[/cyan] / [cyan]quit[/cyan]\n"
    )


def print_help() -> None:
    """The full guide — what `neuropaca help` / `neuropaca --help` prints."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(
        "\n[bold]neuropaca[/bold] — a read-only guide to this project and the "
        "[bold]neuropacad[/bold] daemon.\nEvery command is predefined; there is no free-text "
        "question. `tell` / `overview` answer from\nthe source tree with no daemon; the rest "
        "ask the running daemon over a Unix socket.\n"
    )

    def _section(title: str, rows: list[tuple[str, str]]) -> None:
        table = Table(
            title=title, title_justify="left", title_style="bold", box=None, pad_edge=False
        )
        table.add_column(style="cyan", no_wrap=True)
        table.add_column(overflow="fold")
        for name, text in rows:
            table.add_row(name, text)
        console.print(table)
        console.print()

    _section(
        "understand the project  (offline — no daemon needed)",
        [
            ("overview", "what NeuroPACA is, what it monitors, the L1-L10 layer map"),
            ("tell <path>", "what a file or folder does — from its docstring + top-level defs"),
            (
                "tell <path> --explain",
                "the above, plus a plain-words model paraphrase (needs the daemon)",
            ),
        ],
    )
    _section(
        "daemon & state  (need a running daemon)",
        [
            ("health", "daemon + module health"),
            ("insights", "drain surfaced insights (anomaly / distraction)"),
            ("notifications", "what the action layer wants to tell you"),
            ("confirmations", "dangerous actions waiting on your yes/no"),
            ("confirm <id> [--deny]", "answer one of them"),
            (
                'run [--backup] "<cmd>"',
                "hand a command to the action layer (confirmation still required)",
            ),
            ("briefing", "what's waiting, on demand (S0)"),
        ],
    )
    _section(
        "offline  (work with no daemon)",
        [
            ("doctor", "offline health report — config, graph, socket, disk"),
            ("export <path> [--force]", "dump the graph out of data/"),
            ("panic [--yes]", "kill the daemon and wipe all local state"),
            ("repair-graph [--yes]", "rebuild the graph from the episode log (S0)"),
        ],
    )
    _section(
        "interactive shell  (run `neuropaca` with no arguments)",
        [
            ("tell src/neuropaca/idle", "any verb above works here, unquoted"),
            ("help   quit", "this guide / leave"),
        ],
    )
    console.print(
        "[dim]Socket:[/dim] --socket PATH  >  $NEUROPACA_SOCKET  >  "
        "$XDG_RUNTIME_DIR/neuropaca.sock\n"
        "[dim]Autostart:[/dim] scripts/install-user-service.sh  — daemon on login, every login\n"
    )


def run() -> int:
    """The read-translate-dispatch loop. Returns an exit code for `cli.main`."""
    _banner()
    while True:
        try:
            raw = input("neuropaca> ")
        except EOFError:  # ^D — the shell way to leave
            print()
            return 0
        except KeyboardInterrupt:  # ^C clears the line, like a real shell
            print()
            continue

        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low in _QUIT:
            return 0
        if low in _HELP:
            print_help()
            continue

        argv = _translate(line)
        if isinstance(argv, _UnknownVerb):
            print(f"unknown command {str(argv)!r} — try: help")
            continue
        try:
            cli._run_once(argv)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as exc:  # a REPL survives one bad verb; it does not crash
            print(f"\N{MULTIPLICATION SIGN} {exc}")


# gen-ref: 55c7b35c
