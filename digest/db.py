"""SQLite schema and connection helper."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from digest.models import GraphSeed, Record

DEFAULT_DB_PATH = Path("digest.db")
DEFAULT_SEED_PATH = Path("data/graph_seed.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    role    TEXT NOT NULL,
    team    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processes (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL REFERENCES products(id),
    name        TEXT NOT NULL CHECK (name IN ('EVT', 'DVT', 'PVT')),
    gate_date   TEXT,
    status      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stages (
    id          TEXT PRIMARY KEY,
    process_id  TEXT NOT NULL REFERENCES processes(id),
    name        TEXT NOT NULL,
    position    INTEGER NOT NULL,
    owner_id    TEXT REFERENCES users(id),
    UNIQUE (process_id, position)
);

CREATE TABLE IF NOT EXISTS requirements (
    id          TEXT PRIMARY KEY,
    product_id  TEXT NOT NULL REFERENCES products(id),
    title       TEXT NOT NULL,
    value       TEXT NOT NULL,
    owner_id    TEXT REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS requirement_links (
    requirement_id           TEXT NOT NULL REFERENCES requirements(id),
    affected_requirement_id  TEXT NOT NULL REFERENCES requirements(id),
    note                     TEXT,
    PRIMARY KEY (requirement_id, affected_requirement_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    owner_id        TEXT REFERENCES users(id),
    assigner_id     TEXT REFERENCES users(id),
    product_id      TEXT NOT NULL REFERENCES products(id),
    stage_id        TEXT REFERENCES stages(id),
    requirement_id  TEXT REFERENCES requirements(id),
    deadline        TEXT,
    status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    upstream_task_id    TEXT NOT NULL REFERENCES tasks(id),
    downstream_task_id  TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (upstream_task_id, downstream_task_id)
);

CREATE TABLE IF NOT EXISTS aliases (
    surface      TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('task', 'requirement')),
    target_id    TEXT NOT NULL,
    PRIMARY KEY (surface, target_kind)
);

CREATE TABLE IF NOT EXISTS signals (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    channel       TEXT NOT NULL,
    product_id    TEXT REFERENCES products(id),
    content_hash  TEXT NOT NULL,
    last_ts       TEXT NOT NULL,
    participants  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS deltas (
    id           TEXT PRIMARY KEY,
    signal_id    TEXT NOT NULL REFERENCES signals(id),
    type         TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('task', 'requirement')),
    target_id    TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    confidence   REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    ts           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    signal_id      TEXT PRIMARY KEY REFERENCES signals(id),
    backend        TEXT NOT NULL,
    changes_state  REAL NOT NULL CHECK (changes_state BETWEEN 0.0 AND 1.0),
    change_type    TEXT NOT NULL,
    contradicts    REAL NOT NULL CHECK (contradicts BETWEEN 0.0 AND 1.0),
    risk           REAL NOT NULL CHECK (risk BETWEEN 0.0 AND 1.0),
    route          TEXT NOT NULL CHECK (route IN ('extract', 'escalate', 'drop')),
    settled        TEXT CHECK (settled IN ('extract', 'drop'))
);

CREATE TABLE IF NOT EXISTS unresolved (
    surface      TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK (target_kind IN ('task', 'requirement')),
    signal_id    TEXT NOT NULL REFERENCES signals(id),
    PRIMARY KEY (surface, target_kind, signal_id)
);

CREATE TABLE IF NOT EXISTS inbox (
    user_id   TEXT NOT NULL REFERENCES users(id),
    delta_id  TEXT NOT NULL REFERENCES deltas(id),
    reason    TEXT NOT NULL,
    score     REAL NOT NULL,
    PRIMARY KEY (user_id, delta_id)
);

CREATE TABLE IF NOT EXISTS digests (
    user_id  TEXT NOT NULL REFERENCES users(id),
    date     TEXT NOT NULL,
    body     TEXT NOT NULL,
    PRIMARY KEY (user_id, date)
);
"""


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


def insert_or_ignore(conn: sqlite3.Connection, table: str, record: Record) -> None:
    row = {
        key: json.dumps(value) if isinstance(value, list) else value
        for key, value in record.model_dump(mode="json").items()
    }
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    conn.execute(f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})", row)


def load_graph_seed(conn: sqlite3.Connection, path: str | Path = DEFAULT_SEED_PATH) -> GraphSeed:
    """Insert the seeded project graph.

    Existing rows are left alone, so reloading never reverts changes made by `apply`.
    """
    seed = GraphSeed.model_validate_json(Path(path).read_text())
    with conn:
        for table in GraphSeed.model_fields:
            for record in getattr(seed, table):
                insert_or_ignore(conn, table, record)
    return seed
