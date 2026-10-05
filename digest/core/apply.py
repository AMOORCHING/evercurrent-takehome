"""`apply`: deltas to graph."""

from __future__ import annotations

import logging
import sqlite3

from digest.db import insert_or_ignore
from digest.models import Delta, Record, Requirement, Task

log = logging.getLogger(__name__)

TASK_STATUSES = {"open", "in_progress", "blocked", "done"}

# (target_kind, type) to the table, its record, and the column the delta changes.
TARGETS: dict[tuple[str, str], tuple[str, type[Record], str]] = {
    ("task", "status_change"): ("tasks", Task, "status"),
    ("task", "deadline_change"): ("tasks", Task, "deadline"),
    ("task", "owner_change"): ("tasks", Task, "owner_id"),
    ("requirement", "value_change"): ("requirements", Requirement, "value"),
}


class InvalidDelta(ValueError):
    pass


def apply(conn: sqlite3.Connection, delta: Delta) -> bool:
    """Update the target row and append the delta. Returns False if the delta ID is already applied.

    The stored old value is the value recorded in the graph, which may differ from what the
    extractor claimed. Raises `InvalidDelta` or `pydantic.ValidationError` before writing anything.
    Does not commit; the caller owns the transaction.
    """
    if conn.execute("SELECT 1 FROM deltas WHERE id = ?", (delta.id,)).fetchone():
        return False

    target = TARGETS.get((delta.target_kind, delta.type))
    if target is None:
        raise InvalidDelta(f"{delta.id}: {delta.type} does not apply to a {delta.target_kind}")
    table, model, column = target

    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (delta.target_id,)).fetchone()
    if row is None:
        raise InvalidDelta(f"{delta.id}: no {delta.target_kind} {delta.target_id}")
    if delta.new_value is None:
        raise InvalidDelta(f"{delta.id}: {delta.type} needs a new value")
    if column == "status" and delta.new_value not in TASK_STATUSES:
        raise InvalidDelta(f"{delta.id}: unknown status {delta.new_value!r}")
    if column == "owner_id" and not conn.execute(
        "SELECT 1 FROM users WHERE id = ?", (delta.new_value,)
    ).fetchone():
        raise InvalidDelta(f"{delta.id}: unknown user {delta.new_value!r}")

    updated = model.model_validate(dict(row) | {column: delta.new_value})
    new_value = updated.model_dump(mode="json")[column]
    old_value = row[column]
    if delta.old_value != old_value:
        log.warning(
            "delta %s on %s %s: extractor said old value %r, graph has %r",
            delta.id, delta.target_kind, delta.target_id, delta.old_value, old_value,
        )

    conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (new_value, delta.target_id))
    insert_or_ignore(
        conn, "deltas", delta.model_copy(update={"old_value": old_value, "new_value": new_value})
    )
    return True
