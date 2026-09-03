"""L9 · the thin CLI client — the `$` shell grammar, terminal-side (B5, B2).

`neuropaca` (the console script) is a *thin* client: it parses the prefix,
opens the Unix socket, sends one JSONL request, renders one JSONL response with
`rich` (design.md), and exits. **No daemon logic, no graph, no model** — every
answer comes from the running daemon.

    neuropaca ask "what is using my CPU"      # $  — natural-language question
    neuropaca diagnose "why is the disk full" # $? — question + live snapshot
    neuropaca "$ how many meetings today"     # raw prefix form
    neuropaca "$! kill webpack"               # $! — emergency command (L7)
    neuropaca "$$ systemctl --user restart x" # $$ — same, with a state backup
    neuropaca health                          # daemon health (non-inference)
    neuropaca insights                        # drain surfaced insights
    neuropaca notifications                   # drain what L7 wants to tell you
    neuropaca confirmations                   # dangerous actions awaiting you
    neuropaca confirm <id> [--deny]           # answer one of them
    neuropaca doctor                          # offline diagnosis (B9, no daemon)
    neuropaca export <path>                   # dump the graph out of data/ (B9)
    neuropaca panic                           # kill the daemon, wipe state (B9)

Socket: ``--socket PATH`` > ``$NEUROPACA_SOCKET`` > ``$XDG_RUNTIME_DIR/neuropaca.sock``.
Non-inference commands (`health`, `insights`, a refused prefix) never touch the
model and return in well under 100 ms against a warm daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

from neuropaca.interface import offline
from neuropaca.interface.layer import default_socket_path

_USAGE = (
    "usage: neuropaca (ask|diagnose|health|insights|notifications|confirmations) [text]\n"
    "       neuropaca confirm <request-id> [--deny]\n"
    '       neuropaca "$ <question>" | "$? <question>" | "$! <command>" | "$$ <command>"\n'
    "       neuropaca doctor                  # offline diagnosis (no daemon needed)\n"
    "       neuropaca export <path> [--force] # dump the graph out of data/\n"
    "       neuropaca panic [--yes]           # kill the daemon and wipe all state\n"
)
_PREFIXES = ("$?", "$!", "$$", "$")  # longest-first so `$?` wins over `$`
_CONNECT_TIMEOUT = 3.0
_RESPONSE_TIMEOUT = 60.0  # a `$?` answer is a CPU inference — allow for it


class _CliError(Exception):
    pass


def _parse(argv: list[str]) -> tuple[dict[str, Any], str | None]:
    """Return (request, socket_override)."""
    socket_override: str | None = None
    deny = False
    args: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok == "--socket":
            socket_override = next(it, None)
            if socket_override is None:
                raise _CliError("--socket needs a path")
        elif tok == "--deny":
            deny = True
        elif tok in ("-h", "--help"):
            raise _CliError(_USAGE)
        else:
            args.append(tok)

    if not args:
        raise _CliError(_USAGE)

    head, *rest = args
    if head == "health":
        return {"op": "health"}, socket_override
    if head == "insights":
        return {"op": "insights"}, socket_override
    if head == "notifications":
        return {"op": "notifications"}, socket_override
    if head == "confirmations":
        return {"op": "confirmations"}, socket_override
    if head == "confirm":
        if len(rest) != 1:
            raise _CliError("'confirm' needs exactly one request id (add --deny to refuse)")
        # Approval is the explicit, typed act rules.md §5.2 asks for: you name the
        # request id, in your own terminal, while L7 is blocked waiting for it.
        return (
            {"op": "confirm", "request_id": rest[0], "approved": not deny},
            socket_override,
        )
    if head in ("ask", "diagnose"):
        text = " ".join(rest).strip()
        if not text:
            raise _CliError(f"'{head}' needs a question")
        prefix = "$?" if head == "diagnose" else "$"
        return {"op": "query", "prefix": prefix, "text": text}, socket_override

    # raw prefix form: the whole thing is one string like "$? why ..."
    raw = " ".join(args).strip()
    for prefix in _PREFIXES:
        if raw == prefix or raw.startswith(prefix + " "):
            return (
                {"op": "query", "prefix": prefix, "text": raw[len(prefix) :].strip()},
                socket_override,
            )
    raise _CliError(_USAGE)


async def _call(request: dict[str, Any], socket_path: str) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path), _CONNECT_TIMEOUT
        )
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as exc:
        raise _CliError(
            f"cannot reach the daemon at {socket_path} — is `neuropacad` running? ({exc})"
        ) from exc

    try:
        writer.write((json.dumps(request) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), _RESPONSE_TIMEOUT)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    if not line:
        raise _CliError("daemon closed the connection without a response")
    try:
        parsed = json.loads(line)
    except ValueError as exc:
        raise _CliError(f"unreadable response: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {"ok": False, "error": "unexpected response"}


# --------------------------------------------------------------------------- render


def _render(request: dict[str, Any], resp: dict[str, Any]) -> int:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    err_console = Console(stderr=True)

    if not resp.get("ok"):
        err_console.print(f"[red]✕[/red] {resp.get('error', 'unknown error')}")
        return 1

    op = request.get("op")
    if op == "health":
        health = resp.get("health", {})
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_row("state", "[green]ok[/green]" if health.get("ok") else "[red]DEGRADED[/red]")
        for key in ("uptime_seconds", "graph_nodes", "graph_edges", "queue_depth", "rss_mb"):
            if key in health:
                table.add_row(key, str(health[key]))
        for mod in health.get("modules", []):
            mark = "[green]✓[/green]" if mod.get("ok") else "[red]✕[/red]"
            table.add_row(f"  {mod.get('name', '?')}", f"{mark} {mod.get('detail', '')}")
        console.print(table)
        return 0

    if op == "insights":
        insights = resp.get("insights", [])
        if not insights:
            console.print("[dim]no new insights[/dim]")
            return 0
        for ins in insights:
            console.print(f"[magenta]◆[/magenta] {ins.get('text', '')}")
            meta = f"{ins.get('category')} · confidence {ins.get('confidence')}"
            console.print(f"  [dim]{meta}[/dim]")
        return 0

    if op == "notifications":
        notifications = resp.get("notifications", [])
        if not notifications:
            console.print("[dim]nothing from the action layer[/dim]")
            return 0
        for note in notifications:
            mark = "[yellow]▲[/yellow]" if note.get("dry_run") else "[cyan]▶[/cyan]"
            console.print(f"{mark} {note.get('text', '')}")
            tail = note.get("reason", "")
            if note.get("dry_run"):
                tail = f"{tail} · dry-run (nothing was executed)" if tail else "dry-run"
            if tail:
                console.print(f"  [dim]{tail}[/dim]")
        return 0

    if op == "confirmations":
        confirmations = resp.get("confirmations", [])
        if not confirmations:
            console.print("[dim]nothing is waiting for you[/dim]")
            return 0
        for pending in confirmations:
            console.print(
                f"[red]⚠[/red] {pending.get('action', '?')} "
                f"[dim]({pending.get('tier', '?')})[/dim] — {pending.get('summary', '')}"
            )
            console.print(f"  [dim]{pending.get('reason', '')}[/dim]")
            console.print(
                f"  [dim]approve:[/dim] neuropaca confirm {pending.get('request_id', '')}"
                f"   [dim]refuse:[/dim] neuropaca confirm "
                f"{pending.get('request_id', '')} --deny"
            )
        return 0

    if op == "confirm":
        verdict = "[green]approved[/green]" if resp.get("approved") else "[yellow]denied[/yellow]"
        console.print(f"{verdict} {resp.get('action', '')} ({resp.get('request_id', '')})")
        return 0

    if resp.get("queued"):
        console.print(f"[cyan]▶[/cyan] {resp.get('prefix', '')} handed to the action layer")
        console.print(f"  [dim]{resp.get('note', '')}[/dim]")
        return 0

    # a query answer
    console.print(f"[magenta]◆[/magenta] {resp.get('answer', '')}")
    cited = resp.get("cited", [])
    if cited:
        console.print(f"  [dim]based on {' · '.join(cited)}[/dim]")
    tail = f"  [dim]confidence {resp.get('confidence')}"
    if resp.get("source") == "template":
        tail += " · extractive (no interactive model)"
    console.print(tail + "[/dim]")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # The three offline verbs are handled before anything touches the socket
    # (B9/BL-7). `doctor` in particular exists for the case where the daemon is
    # not running, so it must not be routed through the daemon.
    offline_result = offline.dispatch(raw_argv)
    if offline_result is not None:
        return offline_result

    try:
        request, socket_override = _parse(raw_argv)
    except _CliError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    socket_path = (
        socket_override or os.environ.get("NEUROPACA_SOCKET") or str(default_socket_path())
    )
    try:
        resp = asyncio.run(_call(request, socket_path))
    except _CliError as exc:
        print(f"✕ {exc}", file=sys.stderr)
        return 1

    return _render(request, resp)


if __name__ == "__main__":
    raise SystemExit(main())
