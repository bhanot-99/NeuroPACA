# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""L9 · the deterministic project guide behind ``neuropaca tell`` / ``overview`` (B12).

The terminal client is a **read-only project guide**: you ask it about the codebase
with predefined commands and it answers from facts already in the tree, not from a
model. This module is the engine.

- ``tell <path>`` — resolve a file or folder, read its **module docstring** and its
  top-level classes / functions (via :mod:`ast`, no import, no execution), name the
  layer it belongs to, and print it. Fully offline — like ``doctor``, it works with
  no daemon.
- ``overview`` — a curated map: what NeuroPACA is, what it watches, the L1-L10 layer
  table, and where to look next.
- ``tell <path> --explain`` adds one optional step on top: the daemon's interactive
  model paraphrases the deterministic summary this module produced (see
  ``InterfaceLayer._explain``). The paraphrase is flagged and never replaces the
  facts above it.

Nothing here touches the socket, ``rich``, or a model. ``interface/cli.py`` owns the
rendering and the ``--explain`` round-trip; everything in this file is plain data over
stdlib types so it stays trivially testable (``tests/test_describe.py``).
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import neuropaca


class DescribeError(Exception):
    """A path could not be resolved, or is not something ``tell`` can describe.

    Carried to the CLI verbatim as the stderr line — the message is written to be
    read by a human, and includes candidate paths when a basename was ambiguous.
    """


def repo_root() -> Path:
    """The installed package's repo root — ``src/neuropaca/__init__.py`` -> repo.

    Matches the daemon's systemd ``WorkingDirectory`` without depending on the
    process cwd, so ``tell`` resolves the same paths whether run from the repo or
    from ``~``. (Lifted from the retired ``interface/knowledge.py``.)
    """
    return Path(neuropaca.__file__).resolve().parents[2]


# ---------------------------------------------------------------- the layer map

_PKG = "src/neuropaca"


@dataclass(frozen=True, slots=True)
class Layer:
    """One architectural layer. ``num`` is the L-number used throughout the docs
    and the module docstrings; ``blurb`` is one plain sentence."""

    num: int
    name: str
    blurb: str

    @property
    def tag(self) -> str:
        return f"L{self.num} · {self.name}"


# Keyed by the top-level package directory under src/neuropaca/. Source of truth:
# Architecture.md §2 and the "Ln ·" prefix each module docstring already carries.
# Static on purpose — parsing the doc at runtime would make `tell` depend on a
# Markdown file that may not ship.
LAYER_MAP: dict[str, Layer] = {
    "core": Layer(
        1,
        "Core",
        "the behavioural graph, event bus, config, models and model runtime — held by every layer",
    ),
    "sensing": Layer(
        2, "Sensing", "collectors that turn raw system and activity readings into MetricSnapshots"
    ),
    "diagnosis": Layer(
        3,
        "Diagnosis",
        "SignalCorrelator + rule-based patterns: load / idle / focus / distraction, no inference",
    ),
    "learning": Layer(
        4, "Learning", "BitNetPlasticity — extractive insight classification behind a GBNF grammar"
    ),
    "drive": Layer(
        5,
        "Drive",
        "PressureAccumulator — builds 'something should happen' pressure toward a threshold",
    ),
    "idle": Layer(
        6,
        "Idle Cognition",
        "DefaultModeNetwork — low-priority reflection while you are away from the machine",
    ),
    "action": Layer(
        7,
        "Action",
        "BaseAction and the safety gate — sandbox, backup, terminal confirmation, audit log",
    ),
    "agents": Layer(
        8, "Agents", "AgentSupervisor — bounded ephemeral agents and structural graph plasticity"
    ),
    "interface": Layer(
        9,
        "Interface",
        "the only layer that talks to you — the Unix socket, this CLI, notifications",
    ),
    "orchestration": Layer(
        10,
        "Orchestration",
        "NeuroPACAOrchestrator and the Scheduler — construct the daemon and own its lifecycle",
    ),
}

_INTRO = (
    "NeuroPACA is a local, offline daemon that watches how you use this machine, "
    "builds a behavioural graph from it, and — behind a hard safety gate — can act "
    "on what it learns. Nothing leaves the box: no telemetry, no screen capture, no "
    "keystroke logging, cold system numbers only."
)

_MONITORS = (
    "the active window and application switches",
    "idle <-> active transitions (Wayland idle-notify)",
    "system CPU / memory / disk pressure",
    "filesystem churn in watched directories",
)


# ---------------------------------------------------------------- resolution


def _candidates(name: str, root: Path) -> list[Path]:
    """Every file/dir under src/neuropaca/ (and the repo-root docs) whose name or
    stem matches ``name``. Used for the bare-basename form (``tell layer.py``)."""
    hits: list[Path] = []
    pkg = root / _PKG
    for p in pkg.rglob("*"):
        if "__pycache__" in p.parts:
            continue
        if p.name == name or p.stem == name:
            hits.append(p)
    for p in sorted(root.glob("*.md")):
        if p.name == name or p.stem == name:
            hits.append(p)
    return hits


def resolve(path_arg: str) -> Path:
    """Turn a user-typed path into an absolute path inside the repo.

    Accepts, in order of preference:
      * ``root/foo/bar.py``            — the ``root/`` prefix is stripped
      * ``src/neuropaca/foo/bar.py``   — repo-root-relative, as-is
      * ``foo/bar.py``                 — tried as-is, then under ``src/neuropaca/``
      * ``bar.py`` / ``layer``         — unique basename match under the package

    Raises :class:`DescribeError` — with the candidate list — on a miss or an
    ambiguous basename. The result is always inside the repo (``..`` that escapes
    is rejected).
    """
    root = repo_root()
    raw = path_arg.strip().strip("/")
    if not raw:
        raise DescribeError(
            "tell needs a path, e.g. `neuropaca tell src/neuropaca/interface/cli.py`"
        )
    if raw in ("root", "."):
        return root
    if raw.startswith("root/"):
        raw = raw[len("root/") :]

    tries = [root / raw]
    if not raw.startswith(_PKG):
        tries.append(root / _PKG / raw)
    for cand in tries:
        try:
            resolved = cand.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.exists():
            return resolved

    if "/" not in raw:
        hits = _candidates(raw, root)
        uniq = sorted({h.resolve() for h in hits})
        if len(uniq) == 1:
            return uniq[0]
        if len(uniq) > 1:
            listing = "\n".join(f"  {h.relative_to(root)}" for h in uniq)
            raise DescribeError(f"'{path_arg}' matches several files — say which:\n{listing}")

    raise DescribeError(f"nothing at '{path_arg}' — path is relative to the repo root")


def _layer_for(path: Path) -> Layer | None:
    root = repo_root()
    try:
        parts = path.resolve().relative_to(root / _PKG).parts
    except ValueError:
        return None
    return LAYER_MAP.get(parts[0]) if parts else None


# ---------------------------------------------------------------- file / dir


@dataclass(frozen=True, slots=True)
class Symbol:
    kind: str  # "class" | "def" | "async def"
    name: str
    summary: str  # first line of the symbol's docstring, or ""


@dataclass(frozen=True, slots=True)
class FileDescription:
    rel: str
    layer: Layer | None
    lines: int
    kib: float
    what: str  # first paragraph of the module docstring, de-indented + unwrapped
    symbols: list[Symbol] = field(default_factory=list)
    see_also: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DirDescription:
    rel: str
    layer: Layer | None
    what: str
    children: list[tuple[str, str]] = field(default_factory=list)  # (name, one-line summary)


def _first_paragraph(doc: str | None) -> str:
    """The lead of a module docstring, flattened. Takes the first paragraph, and
    the second too when the first is just a one-line title (these docstrings open
    with ``Ln · title.`` then a blank line then the real explanation)."""
    if not doc:
        return "(no module docstring)"
    paras = [p for p in doc.strip().split("\n\n") if p.strip()]
    lead = " ".join(paras[0].split())
    if len(lead) < 90 and len(paras) > 1:
        lead = lead + "  " + " ".join(paras[1].split())
    return lead


def _first_line(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _module_doc(path: Path) -> str | None:
    try:
        tree = ast.parse(path.read_text("utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    return ast.get_docstring(tree)


def describe_file(path: Path) -> FileDescription:
    """Read one ``.py`` file's module docstring and its top-level defs via ``ast``.

    Non-Python files (a ``.md`` doc, say) still get a description — their whole
    text is the "what", capped — but no symbol list.
    """
    root = repo_root()
    rel = str(path.resolve().relative_to(root))
    text = path.read_text("utf-8", errors="replace")
    n_lines = text.count("\n") + 1
    kib = path.stat().st_size / 1024

    if path.suffix != ".py":
        para = text.strip().split("\n\n", 1)[0]
        return FileDescription(
            rel=rel,
            layer=_layer_for(path),
            lines=n_lines,
            kib=kib,
            what=" ".join(para.split())[:600],
        )

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise DescribeError(f"{rel} does not parse: {exc}") from exc

    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(Symbol("class", node.name, _first_line(ast.get_docstring(node))))
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append(Symbol("async def", node.name, _first_line(ast.get_docstring(node))))
        elif isinstance(node, ast.FunctionDef):
            symbols.append(Symbol("def", node.name, _first_line(ast.get_docstring(node))))

    doc = ast.get_docstring(tree)
    see_also: list[str] = []
    if doc:
        for sib in sorted(path.parent.glob("*.py")):
            if sib.name != path.name and sib.stem in doc:
                see_also.append(sib.name)

    return FileDescription(
        rel=rel,
        layer=_layer_for(path),
        lines=n_lines,
        kib=kib,
        what=_first_paragraph(doc),
        symbols=symbols,
        see_also=see_also,
    )


def describe_dir(path: Path) -> DirDescription:
    """Summarise a folder: its own purpose (``__init__.py`` docstring if present,
    else the layer blurb) and every child ``.py`` file's one-line docstring."""
    root = repo_root()
    rel = str(path.resolve().relative_to(root))
    layer = _layer_for(path)

    init_doc = _module_doc(path / "__init__.py") if (path / "__init__.py").exists() else None
    if init_doc:
        what = _first_paragraph(init_doc)
    elif layer is not None:
        what = layer.blurb[0].upper() + layer.blurb[1:] + "."
    else:
        what = "(no package docstring)"

    children: list[tuple[str, str]] = []
    for child in sorted(path.iterdir()):
        if child.name in ("__pycache__", "__init__.py") or child.name.startswith("."):
            continue
        if child.is_dir():
            sub = LAYER_MAP.get(child.name)
            children.append((child.name + "/", sub.blurb if sub else "(subpackage)"))
        elif child.suffix == ".py":
            children.append((child.name, _first_line(_module_doc(child)) or "(no docstring)"))
    return DirDescription(rel=rel, layer=layer, what=what, children=children)


# ---------------------------------------------------------------- rendering


def _wrap(text: str, indent: str = "  ", width: int = 88) -> str:
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _describe(path: Path) -> FileDescription | DirDescription:
    if path.is_dir():
        return describe_dir(path)
    if path.is_file():
        return describe_file(path)
    raise DescribeError(f"{path} is neither a file nor a directory")


def render_tell(path: Path) -> str:
    """The deterministic ``neuropaca tell`` block, as plain text."""
    desc = _describe(path)
    out: list[str] = []

    if isinstance(desc, DirDescription):
        head = f"{Path(desc.rel).name}/   ·   {desc.rel}"
        out.append(head)
        if desc.layer:
            out.append(f"layer   {desc.layer.tag}")
        out.append("")
        out.append("WHAT IT IS")
        out.append(_wrap(desc.what))
        if desc.children:
            out.append("")
            out.append("CONTAINS")
            pad = max(len(n) for n, _ in desc.children)
            for name, summary in desc.children:
                out.append(f"  {name.ljust(pad)}  {summary}")
        out.append("")
        out.append("NEXT")
        out.append(f"  neuropaca tell {desc.rel}/<file>")
        return "\n".join(out)

    head = f"{Path(desc.rel).name}   ·   {desc.rel}"
    out.append(head)
    if desc.layer:
        out.append(f"layer   {desc.layer.tag}")
    out.append(f"size    {desc.lines} lines · {desc.kib:.1f} KiB")
    out.append("")
    out.append("WHAT IT IS")
    out.append(_wrap(desc.what))
    if desc.symbols:
        out.append("")
        out.append("DEFINES")
        pad = max(len(f"{s.kind} {s.name}") for s in desc.symbols)
        for s in desc.symbols:
            label = f"{s.kind} {s.name}".ljust(pad)
            out.append(f"  {label}  {s.summary}".rstrip())
    if desc.see_also:
        out.append("")
        out.append("SEE ALSO")
        out.append("  " + " · ".join(desc.see_also))
    return "\n".join(out)


def deterministic_summary(path: Path) -> tuple[str, str]:
    """``(label, summary)`` handed to the daemon for ``tell --explain``.

    ``label`` is the repo-relative path; ``summary`` is the WHAT IT IS + DEFINES
    text only — first-party facts, nothing model-chosen. Kept small so the
    paraphrase prompt stays well inside the interactive model's context window.
    """
    desc = _describe(path)
    lines = [f"File: {desc.rel}", "", desc.what]
    if isinstance(desc, FileDescription) and desc.symbols:
        lines.append("")
        lines.append("Defines:")
        for s in desc.symbols:
            lines.append(f"- {s.kind} {s.name}: {s.summary}".rstrip())
    elif isinstance(desc, DirDescription) and desc.children:
        lines.append("")
        lines.append("Contains:")
        for name, summary in desc.children:
            lines.append(f"- {name}: {summary}")
    return desc.rel, "\n".join(lines)[:3000]


def render_overview() -> str:
    """The curated ``neuropaca overview`` — what NeuroPACA is, what it watches, the
    layer table, and where to look next."""
    out: list[str] = ["NeuroPACA — a local behavioural-graph daemon.", ""]
    out.append("WHAT IT IS")
    out.append(_wrap(_INTRO))
    out.append("")
    out.append("WHAT IT MONITORS")
    for item in _MONITORS:
        out.append(f"  · {item}")
    out.append("")
    out.append("LAYERS")
    rows = sorted(LAYER_MAP.items(), key=lambda kv: kv[1].num)
    pad = max(len(name) for name, _ in rows)
    for name, layer in rows:
        out.append(f"  L{layer.num:<2} {name.ljust(pad)}  {layer.blurb}")
    out.append("")
    out.append("NEXT")
    out.append("  neuropaca tell src/neuropaca/<layer>      explain one layer")
    out.append("  neuropaca tell src/neuropaca/<layer>/<file>.py --explain")
    out.append("  neuropaca health                          is the daemon healthy")
    return "\n".join(out)


# gen-ref: 700fd28c
