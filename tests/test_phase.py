"""A3 stage-aware focus: stage distance, gate proximity, and the YAML tiebreaker.

The demo pair sits either side of EVT exit: day 5 is 2026-03-06, the EVT gate passes
on day 6 (2026-03-09), day 7 is 2026-03-10.
"""

import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from digest.attach.phase import (
    PhaseGraph,
    PhaseProcess,
    PhaseRanker,
    load_phase_graph,
    load_phase_weights,
)
from digest.cli import app
from digest.core.rank import load_inbox
from digest.db import connect
from digest.models import Delta, InboxItem, InboxRow, Signal, User
from tests.test_ingest import _ingest, _snapshot

DATA = Path(__file__).parent.parent / "data"

DAY_5 = dt.date(2026, 3, 6)
DAY_7 = dt.date(2026, 3, 10)

RACHEL = User(id="rachel", name="Rachel Kim", role="engineering_manager", team="management")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("phase") / "digest.db"
    _ingest(DATA, path)
    return path


def _ts(date: dt.date, hour: int = 12) -> str:
    return f"{dt.datetime.combine(date, dt.time(hour), dt.UTC).timestamp():.6f}"


def _item(n: int, target_id: str, product: str, date: dt.date, hour: int = 12) -> InboxItem:
    ts = _ts(date, hour)
    signal = Signal(
        id=f"{product}:{ts}", source="slack", channel=product, content_hash="x",
        last_ts=ts, participants=[],
    )
    delta = Delta(
        id=f"d{n}", signal_id=signal.id, type="status_change", target_kind="task",
        target_id=target_id, old_value="open", new_value="blocked", confidence=1.0, ts=ts,
    )
    return InboxItem(
        row=InboxRow(user_id="rachel", delta_id=delta.id, reason=f"reason {n}", score=1.0),
        delta=delta, signal=signal, product_id=product, product_name=product.title(),
        target_title=f"Task {target_id}", source_url=f"https://example/{n}", conflicts=[],
    )


def test_evt_gate_passing_flips_the_ranking(db_path):
    """The same two items for the same user, dated either side of the EVT gate: before
    it, the EVT-stage item wins; after it, the DVT-stage item does. Nothing else moves."""
    conn = connect(db_path)
    graph = load_phase_graph(conn)
    conn.close()
    ranker = PhaseRanker(graph, load_phase_weights(DATA / "phase_weights.yaml"))

    def ranked_on(date: dt.date) -> list:
        # T09 sits in gripper-evt-4 and T16 in gripper-dvt-5, both stages rachel owns.
        return ranker.rank(RACHEL, [_item(1, "T09", "gripper", date), _item(2, "T16", "gripper", date)])

    before = ranked_on(DAY_5)
    assert [s.item.delta.target_id for s in before] == ["T09", "T16"]
    # EVT is active and its gate is three days out: distance 0 times the 1.25 boost.
    assert before[0].score == pytest.approx(1.25)
    assert before[1].score < before[0].score

    after = ranked_on(DAY_7)
    assert [s.item.delta.target_id for s in after] == ["T16", "T09"]
    # DVT became active when the gate passed, so T16 is now the distance-0 item.
    assert after[0].score == pytest.approx(1.0 + 1.0 / 46.0)
    assert after[1].score < after[0].score


def test_day5_and_day7_digests_differ_via_cli(db_path, monkeypatch):
    """`digest run --phase` either side of EVT exit: both digests print, differ, and
    rerunning gives the identical body and database state."""
    monkeypatch.chdir(DATA.parent)  # --phase reads data/phase_weights.yaml from the repo root

    def run(date: dt.date) -> str:
        result = CliRunner().invoke(
            app, ["run", "--user", "tom", "--date", date.isoformat(), "--db", str(db_path), "--phase"]
        )
        assert result.exit_code == 0, result.output
        return result.output

    day5 = run(DAY_5)
    day7 = run(DAY_7)
    assert day5 != day7
    assert "T03" in day5  # the day-5 change sits in gripper EVT, the active phase
    assert "T10" in day7  # the day-7 changes sit in gripper DVT, active once the gate passed

    state = _snapshot(db_path)
    assert run(DAY_5) == day5
    assert run(DAY_7) == day7
    assert _snapshot(db_path) == state


def test_nearer_gate_ranks_gripper_over_drive(db_path):
    """Lena's real day-5 inbox: REQ-G04 (gripper, feeds her EVT stage through its tasks)
    and T21 (drive, feeds her DVT stage) are both distance zero, so the gripper item wins
    only because its gate is three days out against drive's three weeks."""
    conn = connect(db_path)
    graph = load_phase_graph(conn)
    lena = User.model_validate(dict(conn.execute("SELECT * FROM users WHERE id = 'lena'").fetchone()))
    items = load_inbox(conn, "lena", DAY_5)
    conn.close()

    ranked = PhaseRanker(graph, load_phase_weights(DATA / "phase_weights.yaml")).rank(lena, items)
    assert [s.item.delta.target_id for s in ranked] == ["REQ-G04", "T21"]
    assert ranked[0].score == pytest.approx(1.25)
    assert ranked[1].score == pytest.approx(1.0 + 1.0 / 22.0)


def test_role_by_phase_weights_break_ties():
    """Two products, gates equally far, the user owning no stage in either: scores tie,
    and the YAML table decides by role - even against the earlier-change tiebreak."""
    gate = dt.date(2026, 3, 11)
    graph = PhaseGraph(
        processes={
            "gripper": (PhaseProcess("g-evt", "EVT", gate),),
            "drive": (PhaseProcess("d-dvt", "DVT", gate),),
        },
        stages={}, stage_count={"g-evt": 4, "d-dvt": 4}, owned={},
        task_stage={}, requirement_stages={},
    )
    ranker = PhaseRanker(graph, load_phase_weights(DATA / "phase_weights.yaml"))
    # The gripper item is the earlier change, so plain recency would put it first.
    items = [_item(1, "TX", "gripper", DAY_5, hour=9), _item(2, "TY", "drive", DAY_5, hour=12)]

    supply = User(id="tom", name="Tom Becker", role="supply_chain_lead", team="operations")
    assert [s.item.product_id for s in ranker.rank(supply, items)] == ["drive", "gripper"]

    mech = User(id="maya", name="Maya Chen", role="mechanical_engineer", team="mechanical")
    assert [s.item.product_id for s in ranker.rank(mech, items)] == ["gripper", "drive"]


def test_load_phase_weights_reads_the_checked_in_table():
    weights = load_phase_weights(DATA / "phase_weights.yaml")
    assert set(weights) == {
        "mechanical_engineer", "electrical_engineer", "firmware_engineer",
        "supply_chain_lead", "engineering_manager", "product_manager",
    }
    for table in weights.values():
        assert set(table) == {"EVT", "DVT", "PVT"}
        assert all(0.0 <= w <= 1.0 for w in table.values())


@pytest.mark.parametrize("body", [
    "EVT: 1.0\n",                 # weight line before any role
    "role: 3\n",                  # role line carrying a value
    "role:\n  EVT: fast\n",       # weight that is not a number
    "just words\n",               # no key: value shape at all
])
def test_load_phase_weights_rejects_malformed_lines(tmp_path, body):
    path = tmp_path / "weights.yaml"
    path.write_text(body)
    with pytest.raises(ValueError):
        load_phase_weights(path)


def test_phase_flag_conflicts_with_ranker_flag(db_path):
    result = CliRunner().invoke(
        app,
        ["run", "--user", "tom", "--date", DAY_5.isoformat(), "--db", str(db_path),
         "--phase", "--ranker", "calibrated"],
    )
    assert result.exit_code == 1
