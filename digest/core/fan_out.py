"""`fan_out`: delta to inbox rows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

from digest.db import insert_or_ignore
from digest.models import Delta, InboxRow


def fan_out(conn: sqlite3.Connection, delta: Delta) -> list[InboxRow]:
    """Write and return one inbox row per person the delta reaches, skipping signal participants.

    Rules, in order: the target's owner; for a task, the owner of the next stage in its process
    and the owners of tasks one handoff downstream; for a requirement, the owners of the
    requirements it links to. Someone reached by several rules gets one row listing each reason.

    Reads the graph as it stands, so call it right after `apply` on the same delta.
    Does not commit; the caller owns the transaction.
    """
    signal = conn.execute("SELECT participants FROM signals WHERE id = ?", (delta.signal_id,)).fetchone()
    participants = set(json.loads(signal["participants"]))

    reasons: dict[str, list[str]] = {}
    for user_id, reason in _reach(conn, delta):
        if user_id is not None and user_id not in participants:
            reasons.setdefault(user_id, []).append(reason)

    rows = [
        InboxRow(user_id=user_id, delta_id=delta.id, reason=" ".join(why), score=delta.confidence)
        for user_id, why in reasons.items()
    ]
    for row in rows:
        insert_or_ignore(conn, "inbox", row)
    return rows


def _reach(conn: sqlite3.Connection, delta: Delta) -> Iterator[tuple[str | None, str]]:
    if delta.target_kind == "requirement":
        req = conn.execute("SELECT * FROM requirements WHERE id = ?", (delta.target_id,)).fetchone()
        yield req["owner_id"], f"You own requirement {req['id']} ({req['title']})."
        links = conn.execute(
            "SELECT r.id, r.title, r.owner_id, l.note FROM requirement_links l "
            "JOIN requirements r ON r.id = l.affected_requirement_id "
            "WHERE l.requirement_id = ? ORDER BY r.id",
            (req["id"],),
        )
        for link in links:
            note = f" {link['note']}" if link["note"] else ""
            yield link["owner_id"], f"Your requirement {link['id']} ({link['title']}) depends on it.{note}"
        return

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (delta.target_id,)).fetchone()
    yield task["owner_id"], f"You own task {task['id']} ({task['title']})."

    next_stage = conn.execute(
        "SELECT n.name, n.owner_id, p.product_id, p.name AS process FROM stages s "
        "JOIN stages n ON n.process_id = s.process_id AND n.position = s.position + 1 "
        "JOIN processes p ON p.id = s.process_id WHERE s.id = ?",
        (task["stage_id"],),
    ).fetchone()
    if next_stage:
        yield next_stage["owner_id"], (
            f"You own {next_stage['name']}, the next stage of "
            f"{next_stage['product_id']} {next_stage['process']}."
        )

    downstream = conn.execute(
        "SELECT t.id, t.title, t.owner_id FROM handoffs h "
        "JOIN tasks t ON t.id = h.downstream_task_id WHERE h.upstream_task_id = ? ORDER BY t.id",
        (task["id"],),
    )
    for t in downstream:
        yield t["owner_id"], f"Your task {t['id']} ({t['title']}) is one handoff downstream."
