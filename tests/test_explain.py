import datetime as dt
import json
from pathlib import Path

from typer.testing import CliRunner

from digest.attach.deciders import Cascade, PassThroughDecider, save_decisions
from digest.attach.phase import PhaseRanker, load_phase_graph, load_phase_weights
from digest.cli import app
from digest.core.rank import FixedWeightRanker
from digest.db import connect, create_schema
from digest.explain import explain
from digest.models import Decision, User
from tests.test_ingest import _ingest, _snapshot

DATA = Path(__file__).parent.parent / "data"

# The planted silo case X1: maya and rachel agree a deadline slip in the thread, and the
# change reaches priya and tom, who are not in it.
X1_THREAD = "gripper:1772535600.000068"
X1_DATE = "2026-03-03"


def _explain(db_path: Path, *args: str):
    return CliRunner().invoke(app, ["explain", "--db", str(db_path), *args])


def test_explain_traces_item_to_delta_signal_and_decision(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    result = _explain(db_path, "--user", "priya", "--date", X1_DATE, "--item", "1")
    assert result.exit_code == 0, result.output
    out = result.output

    assert "# Explain: priya, 2026-03-03, item 1 of 1" in out
    assert "T01 Finalize jaw finger CAD: deadline change" in out
    assert "## Delta gold-X1" in out
    assert "2026-03-03 -> 2026-03-04" in out
    assert "Fanned out to: priya (you), tom" in out
    assert f"## Signal {X1_THREAD}" in out
    assert "Participants (skipped by fan-out): maya, rachel" in out
    assert "Backend: passthrough" in out
    assert "changes_state 1.00 >= 0.8 -> extracted" in out


def test_explain_is_readonly_and_repeatable(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    state = _snapshot(db_path)
    first = _explain(db_path, "--user", "tom", "--date", X1_DATE, "--item", "1")
    assert first.exit_code == 0, first.output
    assert _explain(db_path, "--user", "tom", "--date", X1_DATE, "--item", "1").output == first.output
    assert _snapshot(db_path) == state


def test_explain_rejects_bad_requests(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    for args in (
        ["--user", "priya", "--date", X1_DATE, "--item", "9"],
        ["--user", "priya", "--date", "2026-01-01", "--item", "1"],  # no items that day
        ["--user", "nobody", "--date", X1_DATE, "--item", "1"],
        ["--user", "priya", "--date", "yesterday", "--item", "1"],
        ["--user", "priya", "--date", X1_DATE, "--item", "1", "--phase", "--ranker", "calibrated"],
    ):
        result = _explain(db_path, *args)
        assert result.exit_code == 1, args


def test_explain_numbers_items_like_the_rendered_digest(tmp_path):
    """Under the phase ranker, tom's 2026-03-10 digest has two items; item 2 is T15."""
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    conn = connect(db_path)
    ranker = PhaseRanker(load_phase_graph(conn), load_phase_weights(DATA / "phase_weights.yaml"))
    tom = User.model_validate(dict(conn.execute("SELECT * FROM users WHERE id = 'tom'").fetchone()))
    trace = explain(conn, tom, dt.date(2026, 3, 10), 2, ranker)
    conn.close()
    assert "item 2 of 2" in trace
    assert "T15" in trace


def test_ingest_records_one_decision_per_signal(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    conn = connect(db_path)
    signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    rows = conn.execute("SELECT backend, route, settled, change_type FROM decisions").fetchall()
    conn.close()
    assert len(rows) == signals
    assert {(r["backend"], r["route"], r["settled"]) for r in rows} == {("passthrough", "extract", None)}


def test_save_decisions_overwrites_and_skips_unsaved_signals(tmp_path):
    conn = connect(tmp_path / "d.db")
    create_schema(conn)
    with conn:
        conn.execute(
            "INSERT INTO signals VALUES ('ch:1.0', 'slack', 'ch', NULL, 'h', '1.0', '[]')"
        )
    cascade = Cascade(PassThroughDecider(), extractor=None)
    cascade.decisions = {
        "ch:1.0": Decision(changes_state=0.5, change_type={"none": 0.9}, contradicts=0.1, risk=0.2),
        "ch:2.0": Decision(changes_state=1.0, change_type={}, contradicts=0.0, risk=0.0),  # not saved
    }
    cascade.routes = {"ch:1.0": "escalate", "ch:2.0": "extract"}
    cascade.escalations = {"ch:1.0": "extract"}
    save_decisions(conn, cascade, "jev")

    rows = conn.execute("SELECT * FROM decisions").fetchall()
    assert [(r["signal_id"], r["backend"], r["route"], r["settled"]) for r in rows] == [
        ("ch:1.0", "jev", "escalate", "extract")
    ]
    assert json.loads(rows[0]["change_type"]) == {"none": 0.9}

    cascade.decisions["ch:1.0"] = Decision(
        changes_state=0.9, change_type={}, contradicts=0.0, risk=0.0
    )
    cascade.routes["ch:1.0"] = "extract"
    cascade.escalations = {}
    save_decisions(conn, cascade, "jev")
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    assert [(r["signal_id"], r["route"], r["settled"]) for r in rows] == [("ch:1.0", "extract", None)]
    conn.close()


def test_explain_shows_escalation_outcome(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    conn = connect(db_path)
    with conn:
        conn.execute(
            "UPDATE decisions SET backend = 'jev', changes_state = 0.95, route = 'escalate', "
            "settled = 'extract' WHERE signal_id = ?",
            (X1_THREAD,),
        )
    priya = User.model_validate(dict(conn.execute("SELECT * FROM users WHERE id = 'priya'").fetchone()))
    trace = explain(conn, priya, dt.date(2026, 3, 3), 1, FixedWeightRanker())
    conn.close()
    assert "Backend: jev" in trace
    assert "middle band" in trace and "escalated" in trace
    assert "Second-stage decider settled it: extract" in trace
