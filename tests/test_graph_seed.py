import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from digest.db import connect, create_schema, load_graph_seed
from digest.models import GraphSeed

SEED_PATH = Path(__file__).parent.parent / "data" / "graph_seed.json"

WORKING_DAYS = [
    dt.date(2026, 3, 2) + dt.timedelta(days=offset)
    for offset in range(14)
    if (dt.date(2026, 3, 2) + dt.timedelta(days=offset)).weekday() < 5
]


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "digest.db")
    create_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def seed() -> GraphSeed:
    return GraphSeed.model_validate_json(SEED_PATH.read_text())


def _dump(conn):
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in GraphSeed.model_fields
    }


def test_every_foreign_key_resolves(conn):
    load_graph_seed(conn, SEED_PATH)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    nullable_fks = [
        ("stages", "owner_id", "users"),
        ("requirements", "owner_id", "users"),
        ("tasks", "owner_id", "users"),
        ("tasks", "assigner_id", "users"),
        ("tasks", "stage_id", "stages"),
        ("tasks", "requirement_id", "requirements"),
    ]
    for table, column, parent in nullable_fks:
        dangling = conn.execute(
            f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL "
            f"AND {column} NOT IN (SELECT id FROM {parent})"
        ).fetchall()
        assert dangling == [], (table, column)


def test_dangling_foreign_key_is_rejected(conn, tmp_path, seed):
    broken = seed.model_copy(
        update={"tasks": [seed.tasks[0].model_copy(update={"owner_id": "nobody"})]}
    )
    path = tmp_path / "broken_seed.json"
    path.write_text(broken.model_dump_json())
    with pytest.raises(sqlite3.IntegrityError):
        load_graph_seed(conn, path)


def test_load_twice_is_idempotent(conn):
    load_graph_seed(conn, SEED_PATH)
    first = _dump(conn)
    load_graph_seed(conn, SEED_PATH)
    assert _dump(conn) == first


def test_counts_match_spec(seed):
    assert len(seed.users) == 8
    assert len(seed.products) == 2
    assert 23 <= len(seed.tasks) <= 27
    assert len(seed.requirements) == 10
    assert len(seed.requirement_links) == 8
    assert len(seed.handoffs) == 12


def test_team_composition(seed):
    roles = sorted(user.role for user in seed.users)
    assert roles == sorted(
        ["mechanical_engineer"] * 2
        + ["electrical_engineer"] * 2
        + ["firmware_engineer", "supply_chain_lead", "engineering_manager", "product_manager"]
    )

    process_product = {p.id: p.product_id for p in seed.processes}
    products_by_user: dict[str, set[str]] = {}
    for stage in seed.stages:
        products_by_user.setdefault(stage.owner_id, set()).add(process_product[stage.process_id])
    for task in seed.tasks:
        products_by_user.setdefault(task.owner_id, set()).add(task.product_id)
    on_both = {user for user, products in products_by_user.items() if len(products) == 2}
    assert len(on_both) == 3


def test_stages_are_ordered_and_owned(seed):
    for process in seed.processes:
        stages = [s for s in seed.stages if s.process_id == process.id]
        assert 4 <= len(stages) <= 5
        assert sorted(s.position for s in stages) == list(range(1, len(stages) + 1))
        assert all(s.owner_id for s in stages)


def test_dates_span_ten_working_days(seed):
    assert len(WORKING_DAYS) == 10
    for task in seed.tasks:
        assert task.deadline in WORKING_DAYS, task.id

    gripper_evt = next(p for p in seed.processes if p.product_id == "gripper" and p.name == "EVT")
    assert gripper_evt.gate_date == WORKING_DAYS[5]
