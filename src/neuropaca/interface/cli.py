"""L9 · the thin CLI client — a read-only project guide, terminal-side (B5, B12).

`neuropaca` (the console script) is a *thin* client. Two kinds of command:

- **deterministic, offline** — `tell` / `overview` (this repo's own docstrings,
  `interface/describe.py`) and `doctor` / `export` / `panic` (`interface/offline.py`).
  No daemon, no model.
- **daemon state** — `health`, `insights`, `notifications`, `confirmations`,
  `confirm`, `run`: parse, open the Unix socket, send one JSONL request, render
  one JSONL response with `rich`, exit. **No daemon logic, no graph, no model**
  here — the answer comes from the running daemon.

    neuropaca                                  # no args → the command menu
    neuropaca help                             # the full guide (interface/repl.py)
    neuropaca tell src/neuropaca/interface/cli.py   # what a file / folder does
    neuropaca tell drive/pressure.py --explain  # + a plain-words model paraphrase
    neuropaca overview                         # what NeuroPACA is + the layer map
    neuropaca health                           # daemon + module health
    neuropaca insights                         # drain surfaced insights
    neuropaca notifications                    # drain what L7 wants to tell you
    neuropaca confirmations                    # dangerous actions awaiting you
    neuropaca confirm <id> [--deny]            # answer one of them
    neuropaca run "pkill -f webpack"           # hand a command to the action layer
    neuropaca run --backup "systemctl --user restart x"   # same, state backed up first
    neuropaca doctor                           # offline diagnosis (B9, no daemon)
    neuropaca export <path>                    # dump the graph out of data/ (B9)
    neuropaca panic                            # kill the daemon, wipe state (B9)

Socket: ``--socket PATH`` > ``$NEUROPACA_SOCKET`` > ``$XDG_RUNTIME_DIR/neuropaca.sock``.
`health` / `insights` and the like never touch a model and return in well under
100 ms against a warm daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

from neuropaca.interface import describe, offline
from neuropaca.interface.layer import default_socket_path

_USAGE = (
    "usage: neuropaca (health|insights|notifications|confirmations)\n"
    "       neuropaca confirm <request-id> [--deny]\n"
    "       neuropaca tell <path> [--explain]    # what a file or folder does\n"
    "       neuropaca overview                   # what NeuroPACA is + the layer map\n"
    '       neuropaca run [--backup] "<command>" # hand a command to the action layer\n'
    "       neuropaca doctor                     # offline diagnosis (no daemon needed)\n"
    "       neuropaca export <path> [--force]    # dump the graph out of data/\n"
    "       neuropaca panic [--yes]              # kill the daemon and wipe all state\n"
)
_CONNECT_TIMEOUT = 3.0
_RESPONSE_TIMEOUT = 75.0  # a `tell --explain` paraphrase is a CPU inference — allow for it


class _CliError(Exception):
    pass


def _parse(argv: list[str]) -> tuple[dict[str, Any], str | None]:
    """Return (request, socket_override)."""
    socket_override: str | None = None
    deny = False
    backup = False
    args: list[str] = []
    it = iter(argv)
    for tok in it:
        if tok == "--socket":
            socket_override = next(it, None)
            if socket_override is None:
                raise _CliError("--socket needs a path")
        elif tok == "--deny":
            deny = True
        elif tok == "--backup":
            backup = True
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
    if head == "run":
        cmd = " ".join(rest).strip()
        if not cmd:
            raise _CliError("'run' needs a command, e.g. neuropaca run \"pkill -f webpack\"")
        # `$!` / `$$` stay the internal wire enum L7 dispatches on (layer.py);
        # the user surface is just `run` / `run --backup`.
        return {"op": "run", "cmd": cmd, "backup": backup}, socket_override

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
    from rich.markup import escape as _e
    from rich.table import Table

    console = Console()
    err_console = Console(stderr=True)

    if not resp.get("ok"):
        err_console.print(f"[red]✕[/red] {_e(str(resp.get('error', 'unknown error')))}")
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
            name = _e(str(mod.get("name", "?")))
            detail = _e(str(mod.get("detail", "")))
            table.add_row(f"  {name}", f"{mark} {detail}")
        console.print(table)
        return 0

    if op == "insights":
        insights = resp.get("insights", [])
        if not insights:
            console.print("[dim]no new insights[/dim]")
            return 0
        for ins in insights:
            console.print(f"[magenta]◆[/magenta] {_e(str(ins.get('text', '')))}")
            meta = f"{ins.get('category')} · confidence {ins.get('confidence')}"
            console.print(f"  [dim]{_e(meta)}[/dim]")
        return 0

    if op == "notifications":
        notifications = resp.get("notifications", [])
        if not notifications:
            console.print("[dim]nothing from the action layer[/dim]")
            return 0
        for note in notifications:
            mark = "[yellow]▲[/yellow]" if note.get("dry_run") else "[cyan]▶[/cyan]"
            console.print(f"{mark} {_e(str(note.get('text', '')))}")
            tail = str(note.get("reason", ""))
            if note.get("dry_run"):
                tail = f"{tail} · dry-run (nothing was executed)" if tail else "dry-run"
            if tail:
                console.print(f"  [dim]{_e(tail)}[/dim]")
        return 0

    if op == "confirmations":
        confirmations = resp.get("confirmations", [])
        if not confirmations:
            console.print("[dim]nothing is waiting for you[/dim]")
            return 0
        for pending in confirmations:
            console.print(
                f"[red]⚠[/red] {_e(str(pending.get('action', '?')))} "
                f"[dim]({_e(str(pending.get('tier', '?')))})[/dim] — "
                f"{_e(str(pending.get('summary', '')))}"
            )
            console.print(f"  [dim]{_e(str(pending.get('reason', '')))}[/dim]")
            console.print(
                f"  [dim]approve:[/dim] neuropaca confirm {_e(str(pending.get('request_id', '')))}"
                f"   [dim]refuse:[/dim] neuropaca confirm "
                f"{_e(str(pending.get('request_id', '')))} --deny"
            )
        return 0

    if op == "confirm":
        verdict = "[green]approved[/green]" if resp.get("approved") else "[yellow]denied[/yellow]"
        console.print(
            f"{verdict} {_e(str(resp.get('action', '')))} ({_e(str(resp.get('request_id', '')))})"
        )
        return 0

    if resp.get("queued"):  # a `run` command handed to L7
        console.print("[cyan]▶[/cyan] handed to the action layer")
        console.print(f"  [dim]{_e(str(resp.get('note', '')))}[/dim]")
        return 0

    # anything else with an `answer` field (an `explain` paraphrase reached
    # through the socket rather than the `tell` fast path).
    answer = str(resp.get("answer", "")).strip()
    if answer:
        console.print(f"[magenta]◆[/magenta] {_e(answer)}")
        console.print(f"  [dim]confidence {resp.get('confidence')}[/dim]")
    return 0


def _describe_dispatch(raw_argv: list[str]) -> int | None:
    """`tell` / `overview` — deterministic and offline (B12). Returns an exit
    code, or None to let the next dispatcher take the argv.

    `tell <path> --explain` prints the deterministic block, then makes ONE socket
    call for the model paraphrase; a missing daemon just drops the paraphrase.
    """
    if not raw_argv or raw_argv[0] not in ("tell", "overview"):
        return None

    from rich.console import Console

    console = Console()
    err = Console(stderr=True)

    # Peel the flags this command understands; whatever is left is positional.
    want_explain = False
    socket_override: str | None = None
    positional: list[str] = []
    it = iter(raw_argv[1:])
    for tok in it:
        if tok == "--explain":
            want_explain = True
        elif tok == "--socket":
            socket_override = next(it, None)
        else:
            positional.append(tok)

    if raw_argv[0] == "overview":
        console.print(describe.render_overview())
        return 0

    if len(positional) != 1:
        err.print("usage: neuropaca tell <path> [--explain]")
        return 2
    try:
        path = describe.resolve(positional[0])
        console.print(describe.render_tell(path))
        if want_explain:
            target, summary = describe.deterministic_summary(path)
    except describe.DescribeError as exc:
        err.print(f"[red]✕[/red] {exc}")
        return 2

    if not want_explain:
        return 0

    socket_path = (
        socket_override or os.environ.get("NEUROPACA_SOCKET") or str(default_socket_path())
    )
    try:
        resp = asyncio.run(
            _call({"op": "explain", "target": target, "summary": summary}, socket_path)
        )
    except _CliError:
        console.print(
            "\n[dim](--explain needs the daemon running — showed the summary above)[/dim]"
        )
        return 0
    answer = str(resp.get("answer", "")).strip() if resp.get("ok") else ""
    if answer:
        from rich.markup import escape as _e

        console.print(f"\n[magenta]◆ in plain words[/magenta]  {_e(answer)}")
        conf = resp.get("confidence")
        console.print(f"  [dim]confidence {conf} · a model paraphrase, not the facts above[/dim]")
    else:
        console.print("\n[dim](--explain needs the daemon running with an interactive model)[/dim]")
    return 0


def _run_once(raw_argv: list[str]) -> int:
    """One verb: offline / describe dispatch, else parse + one socket round-trip
    + render.

    Shared by `main` and the interactive shell (`interface/repl.py`) so both
    reach the daemon through exactly the same path.
    """
    # Offline verbs are handled before anything touches the socket (B9/BL-7):
    # `doctor` in particular exists for when the daemon will not start, and
    # `tell` / `overview` answer from the source tree with no daemon at all.
    offline_result = offline.dispatch(raw_argv)
    if offline_result is not None:
        return offline_result
    described = _describe_dispatch(raw_argv)
    if described is not None:
        return described

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

    try:
        return _render(request, resp)
    except Exception as exc:  # a rendering bug must not swallow the daemon's answer
        print(f"✕ could not render the response ({exc})", file=sys.stderr)
        print(json.dumps(resp, indent=2, default=str), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # `neuropaca help` / `--help` / `-h` / `-help` → the full guide, not the
    # one-line usage a parse error prints. `-help` is accepted because that is
    # the form the README tells a new user to try.
    if raw_argv and raw_argv[0] in ("help", "--help", "-h", "-help"):
        from neuropaca.interface import repl

        repl.print_help()
        return 0

    # No verb + a real terminal → the interactive shell, where the `$`/`!`
    # sigils are read by us instead of the surrounding shell (B10). A
    # non-interactive stdin keeps the usage error, so a script that runs
    # `neuropaca` with no args fails loudly instead of hanging on a prompt.
    if not raw_argv:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from neuropaca.interface import repl

            return repl.run()
        print(_USAGE, file=sys.stderr)
        return 2

    return _run_once(raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
