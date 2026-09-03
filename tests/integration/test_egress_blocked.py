"""Affirmative egress tests (B9/BL-8) — `rules.md §6`, zero cloud calls.

Before B9 the CI egress job appended hosts to `/etc/hosts` and re-ran the same
suite. That checks the *contrapositive* ("blocked outbound breaks no test") and
nothing else: a suite that never opens a socket passes identically whether or
not the daemon would egress, so the job could not fail for the reason it exists.

These tests assert the property directly, in two independent ways:

1. `test_outbound_*` — actually attempt an outbound connection and require it to
   fail. CI runs this file inside a network namespace with no interface but
   loopback (`unshare -n`), which is a real absence of route rather than the
   old `/etc/hosts` trick: nulling resolver *names* leaves a literal IP
   perfectly reachable, so the previous job could not have caught egress to a
   hardcoded address.
2. `test_no_module_*` / `test_only_the_interface_*` — a static check over the
   shipped package for the imports and call sites that could open a socket at
   all. These hold even on a developer machine with working internet, where the
   first group is skipped.

`NEUROPACA_OFFLINE=1` marks an environment that has promised there is no
outbound path; the CI egress job sets it inside the namespace. Without it the
connect tests skip, so a developer laptop with working internet is not a red
build — the static checks still run everywhere.
"""

from __future__ import annotations

import ast
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_OFFLINE = os.environ.get("NEUROPACA_OFFLINE") == "1"
_SRC = Path(__file__).resolve().parents[2] / "src" / "neuropaca"

# Modules allowed to reference sockets at all. L9 serves a *Unix* socket; the
# B9 offline verbs read SO_PEERCRED off that same Unix socket.
_SOCKET_ALLOWED = {"interface/layer.py", "interface/cli.py", "interface/offline.py"}

# Importing any of these from the daemon package would mean an outbound path
# exists, whether or not a test happens to exercise it.
_FORBIDDEN_IMPORTS = {
    "http.client",
    "httpx",
    "requests",
    "urllib.request",
    "urllib3",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc.client",
    "aiohttp",
}

requires_blocked_network = pytest.mark.skipif(
    not _OFFLINE,
    reason="set NEUROPACA_OFFLINE=1 (CI does) — this asserts outbound is actually blocked",
)


@requires_blocked_network
def test_outbound_http_request_fails() -> None:
    """An outbound HTTP call must raise, not succeed (rules.md §6)."""
    with pytest.raises((urllib.error.URLError, OSError, TimeoutError)):
        urllib.request.urlopen("http://pypi.org/simple/", timeout=5).close()


@requires_blocked_network
def test_outbound_tcp_connect_fails() -> None:
    """Below HTTP too: a raw TCP connect to a public address must not complete.

    Separate from the HTTP case on purpose — `urlopen` failing could merely mean
    DNS was unavailable. This one is handed a literal address, so it fails only
    if there is genuinely no route out.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        with pytest.raises((OSError, TimeoutError)):
            sock.connect(("1.1.1.1", 443))
    finally:
        sock.close()


# Deliberately NOT tested: that a public hostname fails to *resolve*. Measured
# inside the CI network namespace, `gethostbyname("huggingface.co")` still
# returns a routable-looking address from a local cache even with no route out —
# so asserting on resolution would fail the build for a reason unrelated to
# egress. Resolution is not egress; the two connect tests above are.


def _python_sources() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.lineno))
    return found


def test_no_module_imports_an_outbound_client() -> None:
    """No shipped module imports an HTTP/network client library.

    Runs everywhere, including a laptop with working internet — this is the half
    of the guarantee that does not depend on the environment being blocked.
    """
    offenders: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for name, lineno in _imported_names(tree):
            root = name.split(".")[0]
            if name in _FORBIDDEN_IMPORTS or root in _FORBIDDEN_IMPORTS:
                offenders.append(f"{path.relative_to(_SRC).as_posix()}:{lineno} imports {name}")
    assert not offenders, "outbound client library imported:\n  " + "\n  ".join(offenders)


def test_only_the_interface_layer_touches_sockets() -> None:
    """`socket` may be imported only where the Unix-domain L9 socket lives.

    A new `import socket` anywhere else is the shape an accidental egress path
    would take, and this fails the build the moment one appears.
    """
    offenders: list[str] = []
    for path in _python_sources():
        rel = path.relative_to(_SRC).as_posix()
        if rel in _SOCKET_ALLOWED:
            continue
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for name, lineno in _imported_names(tree):
            if name == "socket" or name.startswith("socket."):
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, "socket imported outside the L9 interface: " + ", ".join(offenders)


def test_the_interface_layer_uses_only_unix_sockets() -> None:
    """The allowed modules must use AF_UNIX — never AF_INET/AF_INET6, and never
    asyncio's TCP entry points."""
    offenders: list[str] = []
    for rel in sorted(_SOCKET_ALLOWED):
        path = _SRC / rel
        if not path.exists():
            continue
        source = path.read_text("utf-8")
        for family in ("AF_INET6", "AF_INET"):
            if family in source:
                offenders.append(f"{rel} references {family}")
        for tcp in ("open_connection(", "start_server(", "create_connection("):
            if f"asyncio.{tcp}" in source:
                offenders.append(f"{rel} calls asyncio.{tcp}")
    assert not offenders, "non-Unix socket use in L9:\n  " + "\n  ".join(offenders)
