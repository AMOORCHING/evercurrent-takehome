"""`rank`: inbox to scored items, the first half of `rank_and_render`."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from digest.models import Delta, InboxItem, InboxRow, Ranker, Scored, Signal, User

SLACK_ARCHIVE_URL = "https://slack.com/archives"
DEFAULT_LIMIT = 5
DEFAULT_WEIGHT = 0.5

# Role to delta type to weight. Engineers weigh requirement values most, since their designs
# answer to them; supply chain and the manager weigh dates most, since orders and gates hang on them.
WEIGHTS: dict[str, dict[str, float]] = {
    "mechanical_engineer": {"value_change": 1.0, "status_change": 0.8, "deadline_change": 0.7, "owner_change": 0.6},
    "electrical_engineer": {"value_change": 1.0, "status_change": 0.8, "deadline_change": 0.7, "owner_change": 0.6},
    "firmware_engineer":   {"value_change": 1.0, "status_change": 0.9, "deadline_change": 0.7, "owner_change": 0.6},
    "supply_chain_lead":   {"value_change": 0.7, "status_change": 0.9, "deadline_change": 1.0, "owner_change": 0.6},
    "engineering_manager": {"value_change": 0.7, "status_change": 0.9, "deadline_change": 1.0, "owner_change": 0.8},
    "product_manager":     {"value_change": 1.0, "status_change": 0.7, "deadline_change": 0.9, "owner_change": 0.5},
}


def load_inbox(conn: sqlite3.Connection, user_id: str, date: dt.date) -> list[InboxItem]:
    """The user's inbox items whose delta happened on `date` (UTC), oldest first."""
    rows = conn.execute(
        "SELECT i.reason, i.score, d.* FROM inbox i JOIN deltas d ON d.id = i.delta_id "
        "WHERE i.user_id = ?",
        (user_id,),
    ).fetchall()
    items = []
    for row in rows:
        delta = Delta.model_validate({k: row[k] for k in Delta.model_fields})
        if _utc_date(delta.ts) != date:
            continue
        inbox_row = InboxRow(user_id=user_id, delta_id=delta.id, reason=row["reason"], score=row["score"])
        items.append(_item(conn, inbox_row, delta))
    return sorted(items, key=lambda i: (i.delta.ts, i.delta.id))


class FixedWeightRanker(Ranker):
    """Scores each item by a fixed weight for the user's role and the delta type, times the
    fan-out score, and keeps the top `limit`. Ties go to the earlier change."""

    def __init__(self, limit: int = DEFAULT_LIMIT, weights: dict[str, dict[str, float]] = WEIGHTS) -> None:
        self.limit = limit
        self.weights = weights

    def rank(self, user: User, items: list[InboxItem]) -> list[Scored]:
        role = self.weights.get(user.role, {})
        scored = [Scored(item=i, score=role.get(i.delta.type, DEFAULT_WEIGHT) * i.row.score) for i in items]
        scored.sort(key=lambda s: (-s.score, s.item.delta.ts, s.item.delta.id))
        return scored[: self.limit]


def _utc_date(ts: str) -> dt.date:
    return dt.datetime.fromtimestamp(float(ts), dt.UTC).date()


def _item(conn: sqlite3.Connection, inbox_row: InboxRow, delta: Delta) -> InboxItem:
    table = "tasks" if delta.target_kind == "task" else "requirements"
    target = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (delta.target_id,)).fetchone()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (target["product_id"],)).fetchone()
    signal_row = conn.execute("SELECT * FROM signals WHERE id = ?", (delta.signal_id,)).fetchone()
    signal = Signal.model_validate(dict(signal_row) | {"participants": json.loads(signal_row["participants"])})
    root_ts = signal.id.split(":", 1)[1]
    return InboxItem(
        row=inbox_row,
        delta=delta,
        signal=signal,
        product_id=product["id"],
        product_name=product["name"],
        target_title=target["title"],
        source_url=f"{SLACK_ARCHIVE_URL}/{signal.channel}/p{root_ts.replace('.', '')}",
        conflicts=_conflicts(conn, delta),
    )


def _conflicts(conn: sqlite3.Connection, delta: Delta) -> list[str]:
    """Graph facts the change may collide with: linked requirements for a value change, open
    downstream tasks due earlier or a passed gate for a later deadline, and open downstream tasks
    for a block."""
    if delta.type == "value_change":
        links = conn.execute(
            "SELECT r.id, r.title, r.value, r.owner_id, l.note FROM requirement_links l "
            "JOIN requirements r ON r.id = l.affected_requirement_id "
            "WHERE l.requirement_id = ? ORDER BY r.id",
            (delta.target_id,),
        )
        return [
            f"{r['id']} {r['title']} ({r['value']}, owned by {r['owner_id']})"
            + (f": {r['note']}" if r["note"] else "")
            for r in links
        ]

    downstream = conn.execute(
        "SELECT t.id, t.title, t.owner_id, t.deadline FROM handoffs h "
        "JOIN tasks t ON t.id = h.downstream_task_id "
        "WHERE h.upstream_task_id = ? AND t.status != 'done' ORDER BY t.id",
        (delta.target_id,),
    ).fetchall()

    if delta.type == "deadline_change" and delta.new_value:
        conflicts = [
            f"{t['id']} {t['title']} ({t['owner_id']}) is due {t['deadline']}, before this lands."
            for t in downstream
            if t["deadline"] and t["deadline"] < delta.new_value
        ]
        gate = conn.execute(
            "SELECT p.product_id, p.name, p.gate_date FROM tasks t "
            "JOIN stages s ON s.id = t.stage_id JOIN processes p ON p.id = s.process_id "
            "WHERE t.id = ?",
            (delta.target_id,),
        ).fetchone()
        if gate and gate["gate_date"] and gate["gate_date"] < delta.new_value:
            conflicts.append(
                f"Lands after the {gate['product_id']} {gate['name']} gate on {gate['gate_date']}."
            )
        return conflicts

    if delta.type == "status_change" and delta.new_value == "blocked":
        return [f"{t['id']} {t['title']} ({t['owner_id']}) waits on this." for t in downstream]

    return []
