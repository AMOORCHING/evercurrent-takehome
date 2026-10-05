import json
from pathlib import Path

import pytest

from digest.core.apply import apply
from digest.core.assemble import save_signal
from digest.core.fan_out import fan_out
from digest.db import connect, create_schema, load_graph_seed
from digest.models import Delta, Signal
from tests.test_ingest import _ingest

DATA = Path(__file__).parent.parent / "data"


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "digest.db")
    create_schema(conn)
    load_graph_seed(conn, DATA / "graph_seed.json")
    yield conn
    conn.close()


def _reach(conn, participants, type_, target_kind, target_id, new_value):
    """Apply one delta from a thread with these participants; return {user: reason} from fan-out."""
    signal = Signal(
        id="general:1.000001", source="slack", channel="general", content_hash="x",
        last_ts="1.000001", participants=participants,
    )
    delta = Delta(
        id="d1", signal_id=signal.id, type=type_, target_kind=target_kind, target_id=target_id,
        new_value=new_value, confidence=1.0, ts="1.000001",
    )
    with conn:
        save_signal(conn, signal)
        assert apply(conn, delta)
        rows = fan_out(conn, delta)
    stored = {r["user_id"]: r["reason"] for r in conn.execute("SELECT * FROM inbox")}
    assert stored == {r.user_id: r.reason for r in rows}
    assert all(stored.values())
    return stored


def test_task_owner_is_reached(conn):
    reached = _reach(conn, ["maya", "priya"], "status_change", "task", "T03", "done")
    assert reached == {"tom": "You own task T03 (Machine EVT jaw fingers)."}


def test_task_owner_is_read_after_the_change(conn):
    reached = _reach(conn, ["rachel", "priya"], "owner_change", "task", "T15", "diego")
    assert "diego" in reached and "maya" not in reached


def test_requirement_owner_is_reached(conn):
    reached = _reach(conn, ["omar", "rachel"], "value_change", "requirement", "REQ-G06", "IP65")
    assert reached == {"maya": "You own requirement REQ-G06 (Ingress protection)."}


def test_next_stage_owner_is_reached(conn):
    reached = _reach(conn, ["sam", "diego"], "deadline_change", "task", "T18", "2026-03-10")
    assert reached == {"tom": "You own sourcing, the next stage of drive DVT."}


def test_one_handoff_downstream_owner_is_reached(conn):
    # T02 hands off to T08 (priya), which hands off to T09 (rachel). Priya also owns the next stage.
    reached = _reach(conn, ["maya"], "deadline_change", "task", "T02", "2026-03-05")
    assert reached == {
        "priya": "You own electrical bring-up, the next stage of gripper EVT. "
        "Your task T08 (Validate ten EVT gripper samples) is one handoff downstream."
    }


def test_linked_requirement_owners_are_reached(conn):
    # REQ-G04 links to REQ-G05 (lena). REQ-G01 (maya) links to REQ-G04, the other way, so is not reached.
    reached = _reach(conn, ["priya", "sam"], "value_change", "requirement", "REQ-G04", "<= 2.8 A")
    assert reached == {
        "lena": "Your requirement REQ-G05 (Full close time) depends on it. "
        "A lower current limit slows closing."
    }


def test_signal_participants_are_skipped(conn):
    # T17 reaches diego (owner), tom (next stage, T21), and sam (T20). All three are in the thread.
    assert _reach(conn, ["diego", "sam", "tom"], "status_change", "task", "T17", "done") == {}
    assert conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_replay_fan_out_matches_gold_affected(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    conn = connect(db_path)
    reached: dict[str, set[str]] = {}
    for row in conn.execute("SELECT user_id, delta_id FROM inbox"):
        reached.setdefault(row["delta_id"], set()).add(row["user_id"])
    conn.close()

    gold = json.loads((DATA / "gold.json").read_text())
    for thread in gold["threads"]:
        for d in thread["deltas"]:
            assert reached.get(d["id"], set()) == set(d["affected"]), d["id"]
