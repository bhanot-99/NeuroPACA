#!/usr/bin/env python3
"""Regenerate the vendored cosmic-comp Wayland bindings (B2.5b, D-9).

    uv run --extra activity python -m neuropaca.sensing.activity._protocols.scan

`pywayland.scanner` resolves cross-protocol references only among the XML files
it is given, so the full dependency chain must be passed together:
`wayland.xml` -> `ext-foreign-toplevel-list-v1.xml` + `ext-workspace-v1.xml` ->
`cosmic-workspace-unstable-v1.xml` -> `cosmic-toplevel-info-unstable-v1.xml`
(otherwise: `KeyError: 'wl_output'` / `'zcosmic_workspace_handle_v1'`).

Only the two cosmic-specific modules are kept; their imports of the bundled
protocols are rewritten to `pywayland.protocol.*`. Everything else is deleted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_XML = _HERE / "xml"

# Bundled in pywayland — the generated cosmic modules import these from
# pywayland.protocol.* rather than local copies.
_BUNDLED = ("wayland", "ext_foreign_toplevel_list_v1", "ext_workspace_v1")
_KEEP = ("cosmic_toplevel_info_unstable_v1", "cosmic_workspace_unstable_v1")


def main() -> None:
    inputs = [
        _XML / "wayland.xml",
        _XML / "ext-foreign-toplevel-list-v1.xml",
        _XML / "ext-workspace-v1.xml",
        _XML / "cosmic-workspace-unstable-v1.xml",
        _XML / "cosmic-toplevel-info-unstable-v1.xml",
    ]
    missing = [p.name for p in inputs if not p.exists()]
    if missing:
        sys.exit(f"missing protocol XML in {_XML}/: {', '.join(missing)}")

    cmd = [sys.executable, "-m", "pywayland.scanner", "--input"]
    cmd += [str(p) for p in inputs]
    cmd += ["--output", str(_HERE)]
    subprocess.run(cmd, check=True)

    for module in _KEEP:
        path = _HERE / f"{module}.py"
        text = path.read_text(encoding="utf-8")
        for name in _BUNDLED:
            text = text.replace(f"from .{name} import", f"from pywayland.protocol.{name} import")
        path.write_text(text, encoding="utf-8")

    for generated in _HERE.glob("*_v1.py"):
        if generated.stem not in _KEEP:
            generated.unlink()

    print(f"regenerated {', '.join(_KEEP)} in {_HERE}")


if __name__ == "__main__":
    main()
