"""B12 · the deterministic project guide — `interface/describe.py`.

`tell` / `overview` answer from the source tree with no daemon and no model, so
these tests just call the functions and check the shape of what comes back.
"""

from __future__ import annotations

import pytest

from neuropaca.interface import cli, describe

# ---------------------------------------------------------------- resolve()


@pytest.mark.parametrize(
    "arg",
    [
        "src/neuropaca/interface/cli.py",
        "root/src/neuropaca/interface/cli.py",
        "interface/cli.py",
    ],
)
def test_resolve_accepts_several_path_forms(arg: str) -> None:
    assert describe.resolve(arg).name == "cli.py"


def test_resolve_accepts_a_bare_basename() -> None:
    assert describe.resolve("describe.py").parent.name == "interface"


def test_resolve_a_directory() -> None:
    p = describe.resolve("src/neuropaca/idle")
    assert p.is_dir() and p.name == "idle"


def test_resolve_missing_path_raises() -> None:
    with pytest.raises(describe.DescribeError, match="nothing at"):
        describe.resolve("src/neuropaca/does_not_exist.py")


def test_resolve_ambiguous_basename_lists_candidates() -> None:
    with pytest.raises(describe.DescribeError, match="matches several"):
        describe.resolve("__init__")


def test_resolve_cannot_escape_the_repo() -> None:
    with pytest.raises(describe.DescribeError):
        describe.resolve("../../../etc/passwd")


# ---------------------------------------------------------------- describe / render


def test_describe_file_reads_the_docstring_and_defs() -> None:
    desc = describe.describe_file(describe.resolve("src/neuropaca/interface/cli.py"))
    assert desc.rel == "src/neuropaca/interface/cli.py"
    assert desc.layer is not None and desc.layer.num == 9
    assert desc.lines > 100
    names = {s.name for s in desc.symbols}
    assert {"main", "_parse"} <= names


def test_render_tell_file_has_the_expected_sections() -> None:
    out = describe.render_tell(describe.resolve("interface/layer.py"))
    assert "WHAT IT IS" in out
    assert "DEFINES" in out
    assert "L9 · Interface" in out


def test_render_tell_dir_lists_children() -> None:
    out = describe.render_tell(describe.resolve("src/neuropaca/interface"))
    assert "CONTAINS" in out
    assert "cli.py" in out and "layer.py" in out and "describe.py" in out


def test_render_overview_names_every_layer() -> None:
    out = describe.render_overview()
    for n in range(1, 11):
        assert f"L{n} " in out or f"L{n}\n" in out.replace("  ", " ")
    assert "WHAT IT MONITORS" in out
    assert "behavioural-graph daemon" in out


def test_deterministic_summary_is_bounded_and_labelled() -> None:
    label, summary = describe.deterministic_summary(describe.resolve("interface/cli.py"))
    assert label == "src/neuropaca/interface/cli.py"
    assert "cli.py" in summary
    assert len(summary) <= 3000


# ---------------------------------------------------------------- through the CLI


def test_cli_overview_runs_offline(capsys) -> None:
    assert cli.main(["overview"]) == 0
    assert "LAYERS" in capsys.readouterr().out


def test_cli_tell_runs_offline(capsys) -> None:
    assert cli.main(["tell", "src/neuropaca/interface/describe.py"]) == 0
    assert "WHAT IT IS" in capsys.readouterr().out


def test_cli_tell_bad_path_exits_2(capsys) -> None:
    assert cli.main(["tell", "nonsense.py"]) == 2
    assert "nothing at" in capsys.readouterr().err


def test_cli_tell_explain_without_a_daemon_still_shows_the_block(capsys) -> None:
    code = cli.main(
        ["tell", "interface/describe.py", "--explain", "--socket", "/nonexistent/np.sock"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "WHAT IT IS" in out
    assert "--explain needs the daemon" in out
