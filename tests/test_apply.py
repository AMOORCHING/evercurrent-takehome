from pathlib import Path

import pytest

from digest.core.apply import apply
from digest.core.assemble import save_signal
from digest.db import connect, create_schema, load_graph_seed
from digest.models import Delta, Signal

SEED = Path(__file__).parent.parent / "data" / "graph_seed.json"
SIGNAL = Signal(
    id="gripper:1.000001", source="slack", channel="gripper", product_id="gripper",
    content_hash="x", last_ts="1.000001", participants=["maya"],
)


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "digest.db")
    create_schema(conn)
    load_graph_seed(conn, SEED)
    with conn:
        save_signal(conn, SIGNAL)
    yield conn
    conn.close()


def _delta(type_, target_kind, target_id, new_value, id_="d1"):
    return Delta(
        id=id_, signal_id=SIGNAL.id, type=type_, target_kind=target_kind, target_id=target_id,
        old_value=None, new_value=new_value, confidence=1.0, ts="1.000001",
    )


@pytest.mark.parametrize(
    ("type_", "target_kind", "target_id", "table", "column", "new_value"),
    [
        ("status_change", "task", "T01", "tasks", "status", "blocked"),
        ("deadline_change", "task", "T01", "tasks", "deadline", "2026-03-20"),
        ("owner_change", "task", "T01", "tasks", "owner_id", "diego"),
        ("value_change", "requirement", "REQ-G03", "requirements", "value", "Ti-6Al-4V"),
    ],
)
def test_each_delta_type_updates_the_right_row(conn, type_, target_kind, target_id, table, column, new_value):
    before = {row["id"]: dict(row) for row in conn.execute(f"SELECT * FROM {table}")}
    old_value = before[target_id][column]
    assert old_value != new_value

    with conn:
        assert apply(conn, _delta(type_, target_kind, target_id, new_value)) is True

    after = {row["id"]: dict(row) for row in conn.execute(f"SELECT * FROM {table}")}
    assert after[target_id] == before[target_id] | {column: new_value}
    assert {k: v for k, v in after.items() if k != target_id} == {
        k: v for k, v in before.items() if k != target_id
    }

    stored = conn.execute("SELECT * FROM deltas").fetchall()
    assert len(stored) == 1
    assert (stored[0]["target_id"], stored[0]["old_value"], stored[0]["new_value"]) == (
        target_id, old_value, new_value,
    )


def test_duplicate_delta_id_is_a_no_op(conn):
    with conn:
        assert apply(conn, _delta("status_change", "task", "T01", "blocked")) is True
    tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    deltas = conn.execute("SELECT * FROM deltas").fetchall()

    with conn:
        assert apply(conn, _delta("status_change", "task", "T01", "blocked")) is False
        assert apply(conn, _delta("status_change", "task", "T01", "done")) is False

    assert conn.execute("SELECT * FROM tasks ORDER BY id").fetchall() == tasks
    assert conn.execute("SELECT * FROM deltas").fetchall() == deltas
