from pathlib import Path

from typer.testing import CliRunner

from digest.cli import app

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"


def _demo(*args: str):
    return CliRunner().invoke(app, ["demo", "--data", str(DATA), *args])


def test_demo_runs_offline_and_shows_every_section(tmp_path):
    """The conftest fixture has removed every API key, so this is the fresh-clone run."""
    result = _demo("--results", str(ROOT / "results.md"))
    assert result.exit_code == 0, result.output
    out = result.output

    # The silo case: the thread, and the two affected owners' digests for its day.
    assert "## 1. A silo case" in out
    assert "gripper:1772535600.000068" in out
    assert "# Digest for Priya Raman" in out
    assert "# Digest for Tom Becker" in out

    # Day 5 and day 7 either side of the EVT gate, with different content.
    assert "gate passes on 2026-03-09" in out
    assert "### Day 5: 2026-03-06" in out
    assert "### Day 7: 2026-03-10" in out
    day5 = out.split("### Day 5")[1].split("### Day 7")[0]
    day7 = out.split("### Day 7")[1].split("## 4.")[0]
    assert day5 != day7

    # The honest A3 framing: the live reorder count over every real digest, and one
    # day rendered under both rankers, with the order actually differing.
    assert "reorders 2 of 33 digests" in out
    assert "### One day, both rankers: tom on 2026-03-05" in out
    both = out.split("### One day, both rankers")[1].split("## 4.")[0]
    core_half = both.split("The same day with --phase:")[0]
    phase_half = both.split("The same day with --phase:")[1]
    assert "### 1. T20" in core_half and "### 1. T17" in phase_half

    # The explain trace and the results table.
    assert "# Explain: priya, 2026-03-03, item 1" in out
    assert "Backend: passthrough" in out
    assert "# Results" in out


def test_demo_is_repeatable_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = _demo("--results", str(ROOT / "results.md"))
    assert first.exit_code == 0, first.output
    assert _demo("--results", str(ROOT / "results.md")).output == first.output
    assert list(tmp_path.iterdir()) == []


def test_demo_without_results_file_says_how_to_make_one(tmp_path):
    result = _demo("--results", str(tmp_path / "nope.md"))
    assert result.exit_code == 0, result.output
    assert "run `digest eval`" in result.output
