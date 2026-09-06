"""L9 · the interactive ``neuropaca>`` shell (B10 · terminal accessibility).

Running ``neuropaca`` with no arguments in a terminal lands here. It exists for
one reason: the ``$`` / ``$?`` / ``$!`` / ``$$`` prefixes (design.md §7) are
awkward from a real shell — ``$`` opens a variable, ``!`` opens history
expansion — so every example in the README has to be quoted. Inside this loop
the line is read by *us*, so the sigils can be typed bare:

    neuropaca> $doctor                     # -> the `doctor` verb
    neuropaca> $health                     # -> the `health` verb
    neuropaca> !ask what's eating my CPU    # -> ask "what's eating my CPU"
    neuropaca> ?why is the disk full        # -> diagnose "why is the disk full"
    neuropaca> $? why is the disk full      # the raw prefix form still works

This adds **no capability**: every line is translated to the exact argv the
`neuropaca` console script already accepts and run through the same
`cli._run_once`. No daemon logic, no graph, no model live here either.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from neuropaca.interface import cli
from neuropaca.interface.layer import default_socket_path
from neuropaca.interface.offline import _daemon_pid as _peer_pid

# Short names that map onto a real verb. Kept tiny on purpose — the shell is a
# convenience, not a second grammar to learn.
_ALIASES = {
    "notes": "notifications",
    "pending": "confirmations",
    "status": "health",
}
# Longest-first so `$?` wins over `$` (same order as cli._PREFIXES).
_RAW_PREFIXES = ("$?", "$!", "$$", "$")
_QUIT = {"quit", "exit", "q", ":q"}
_HELP = {"help", "h", "?", "--help", "-h"}
_FREE_TEXT = ("ask", "diagnose")


def _translate(line: str) -> list[str]:
    """Map one REPL line to the argv the `neuropaca` console script understands."""
    # "?<question>" — diagnose, mirroring the "$?" prefix without the "$".
    if line.startswith("?"):
        return ["diagnose", line[1:].strip()]

    # Raw prefix forms pass straight through as a single token, exactly as
    # `neuropaca "$? …"` would arrive on argv — cli._parse owns them.
    for prefix in _RAW_PREFIXES:
        if line == prefix or line.startswith(prefix + " "):
            return [line]

    # "$verb …" / "!verb …" — the sigil is decoration here; drop it.
    if line[:1] in ("$", "!") and line[1:2].isalpha():
        line = line[1:]

    head, _, rest = line.partition(" ")
    head = _ALIASES.get(head, head)
    rest = rest.strip()

    # `ask` / `diagnose` take free text — never shlex-split it, or an
    # apostrophe ("what's") raises ValueError on an unbalanced quote.
    if head in _FREE_TEXT:
        return [head, rest] if rest else [head]
    if not rest:
        return [head]
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
        "  [dim]type[/dim] [cyan]help[/cyan] [dim]for commands,[/dim] "
        "[cyan]quit[/cyan] [dim]to leave[/dim]\n"
    )


def print_help() -> None:
    """The full guide — what `neuropaca help` / `neuropaca --help` prints."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    console.print(
        "\n[bold]neuropaca[/bold] — a thin client for [bold]neuropacad[/bold], the local "
        "behavioural-graph daemon.\nEvery answer comes from the running daemon; this command "
        "sends one request over a Unix\nsocket and renders the reply. Nothing here loads a "
        "model or touches the graph directly.\n"
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
        "ask the graph",
        [
            ('ask "…"', "grounded answer from your graph  (prefix: $)"),
            ('diagnose "…"', "same, plus a live system snapshot  (prefix: $?)"),
        ],
    )
    _section(
        "raw prefixes — must be quoted from a normal shell",
        [
            ('neuropaca "$ …"', "ask"),
            ('neuropaca "$? …"', "diagnose"),
            ('neuropaca "$! <cmd>"', "emergency: hand a command to the action layer (L7)"),
            ('neuropaca "$$ <cmd>"', "same, with a state backup taken first"),
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
        ],
    )
    _section(
        "offline  (work with no daemon)",
        [
            ("doctor", "offline health report — config, graph, socket, disk"),
            ("export <path> [--force]", "dump the graph out of data/"),
            ("panic [--yes]", "kill the daemon and wipe all local state"),
        ],
    )
    _section(
        "interactive shell  (run `neuropaca` with no arguments)",
        [
            ("$doctor   $health", "a `$` + verb runs that verb"),
            ("!ask what's slow", "a `!` + verb, then free text"),
            ("?why is disk full", "a leading `?` is diagnose"),
            ("$ how many meetings", "the raw prefixes work unquoted in here"),
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
        try:
            cli._run_once(argv)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as exc:  # a REPL survives one bad verb; it does not crash
            print(f"\N{MULTIPLICATION SIGN} {exc}")
