"""`digest explain`: the chain from one digest item back through its delta and signal to
the decider's probabilities and routing.

The item, delta and signal come from the database as `digest run` reads it; the decider
section reads the `decisions` table that `digest ingest` fills, so it shows what was
actually answered at ingest time rather than re-asking a backend.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from digest.attach.deciders import DROP_AT, EXTRACT_AT
from digest.core.rank import load_inbox
from digest.core.render import display_order
from digest.models import Ranker, Scored, User


class ExplainError(ValueError):
    """The requested item does not exist: an empty digest or an item number out of range."""


def explain(conn: sqlite3.Connection, user: User, date: dt.date, number: int, ranker: Ranker) -> str:
    """The trace for item `number` (1-based, as the printed digest counts cards) of the
    user's digest for `date` under `ranker`."""
    items = display_order(ranker.rank(user, load_inbox(conn, user.id, date)))
    if not items:
        raise ExplainError(f"{user.id} has no digest items on {date.isoformat()}")
    if not 1 <= number <= len(items):
        raise ExplainError(f"the digest has {len(items)} item(s); --item must be 1 to {len(items)}")
    scored = items[number - 1]
    lines = [f"# Explain: {user.id}, {date.isoformat()}, item {number} of {len(items)}", ""]
    lines += _item_section(scored)
    lines += _delta_section(conn, scored, user)
    lines += _signal_section(scored)
    lines += _decider_section(conn, scored)
    return "\n".join(lines).rstrip("\n") + "\n"


def _item_section(scored: Scored) -> list[str]:
    item = scored.item
    delta = item.delta
    return [
        "## Digest item",
        f"{delta.target_id} {item.target_title}: {delta.type.replace('_', ' ')} ({item.product_name})",
        f"- Rank score: {scored.score:.2f}",
        f"- Why it reaches you: {item.row.reason}",
        "",
    ]


def _delta_section(conn: sqlite3.Connection, scored: Scored, user: User) -> list[str]:
    delta = scored.item.delta
    reached = conn.execute(
        "SELECT user_id, reason FROM inbox WHERE delta_id = ? ORDER BY user_id", (delta.id,)
    ).fetchall()
    lines = [
        f"## Delta {delta.id}",
        f"- {delta.type.replace('_', ' ')} on {delta.target_kind} {delta.target_id}: "
        f"{delta.old_value or '(none)'} -> {delta.new_value or '(none)'}",
        f"- Confidence {delta.confidence:.2f}, at {_utc(delta.ts)}",
        "- Fanned out to: "
        + ", ".join(r["user_id"] + (" (you)" if r["user_id"] == user.id else "") for r in reached),
        "",
    ]
    return lines


def _signal_section(scored: Scored) -> list[str]:
    item = scored.item
    signal = item.signal
    return [
        f"## Signal {signal.id}",
        f"- {signal.source} #{signal.channel}, product {item.product_id}, "
        f"last activity {_utc(signal.last_ts)}",
        f"- Participants (skipped by fan-out): {', '.join(signal.participants) or '(none)'}",
        f"- Link: {item.source_url}",
        "",
    ]


def _decider_section(conn: sqlite3.Connection, scored: Scored) -> list[str]:
    row = conn.execute(
        "SELECT * FROM decisions WHERE signal_id = ?", (scored.item.signal.id,)
    ).fetchone()
    if row is None:
        return [
            "## Decider",
            "No decision recorded for this signal; it was ingested before decisions were stored.",
            "Re-ingest to record the decider's answers.",
        ]
    change_type = json.loads(row["change_type"])
    lines = [
        "## Decider",
        f"- Backend: {row['backend']}",
        f"- changes_state {row['changes_state']:.2f}, contradicts {row['contradicts']:.2f}, "
        f"risk {row['risk']:.2f}",
        "- change_type: "
        + (
            ", ".join(f"{k} {v:.2f}" for k, v in sorted(change_type.items(), key=lambda kv: -kv[1]))
            or "(none asked)"
        ),
    ]
    if row["settled"]:
        lines += [
            f"- Routing: first stage answered in the middle band ({DROP_AT} to {EXTRACT_AT}) -> escalated",
            f"- Second-stage decider settled it: {row['settled']}; the probabilities above are its answer",
        ]
    else:
        lines.append(f"- Routing: {_routing(row['route'], row['changes_state'])}")
    return lines


def _routing(route: str, changes_state: float) -> str:
    if route == "extract":
        return f"changes_state {changes_state:.2f} >= {EXTRACT_AT} -> extracted"
    if route == "drop":
        return f"changes_state {changes_state:.2f} <= {DROP_AT} -> dropped"
    return (
        f"{DROP_AT} < changes_state {changes_state:.2f} < {EXTRACT_AT} -> escalated; "
        "with no second-stage decider it went to the extractor"
    )


def _utc(ts: str) -> str:
    return dt.datetime.fromtimestamp(float(ts), dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
