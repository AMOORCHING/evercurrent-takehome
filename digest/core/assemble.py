"""`assemble`: Slack messages to signals, one per thread."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict

from pydantic import BaseModel, Field

from digest.models import Signal

SOURCE = "slack"


class SlackUser(BaseModel):
    id: str
    name: str


class SlackReaction(BaseModel):
    name: str
    users: list[str]


class SlackMessage(BaseModel):
    user: str
    ts: str
    text: str = ""
    thread_ts: str | None = None
    reactions: list[SlackReaction] = Field(default_factory=list)


class SlackExport(BaseModel):
    users: list[SlackUser]
    messages: dict[str, list[SlackMessage]]


def assemble(conn: sqlite3.Connection, export: SlackExport) -> list[Signal]:
    """Group messages by thread and return the signals that are new or changed, oldest first.

    A signal's ID is `<channel>:<root ts>`. Its hash covers every message and reaction, so an
    unchanged hash means an unchanged row. Nothing is written; see `save_signal`.
    """
    names = {u.id: u.name for u in export.users}
    keywords = _product_keywords(conn)
    stored = dict(conn.execute("SELECT id, content_hash FROM signals").fetchall())

    signals = []
    for signal_id, messages in threads(export).items():
        channel = signal_id.rsplit(":", 1)[0]
        signal = Signal(
            id=signal_id,
            source=SOURCE,
            channel=channel,
            product_id=_tag_product(channel, messages, keywords),
            content_hash=_content_hash(channel, messages),
            last_ts=messages[-1].ts,
            participants=sorted(_participants(messages, names)),
        )
        if stored.get(signal.id) != signal.content_hash:
            signals.append(signal)
    return sorted(signals, key=lambda s: (s.last_ts, s.id))


def threads(export: SlackExport) -> dict[str, list[SlackMessage]]:
    """Each thread's messages, oldest first, keyed by signal ID."""
    grouped: dict[str, list[SlackMessage]] = defaultdict(list)
    for channel, messages in export.messages.items():
        for m in messages:
            grouped[f"{channel}:{m.thread_ts or m.ts}"].append(m)
    return {signal_id: sorted(ms, key=lambda m: m.ts) for signal_id, ms in grouped.items()}


def save_signal(conn: sqlite3.Connection, signal: Signal) -> None:
    row = signal.model_dump() | {"participants": json.dumps(signal.participants)}
    columns = ", ".join(row)
    placeholders = ", ".join(f":{key}" for key in row)
    updates = ", ".join(f"{key} = excluded.{key}" for key in row if key != "id")
    conn.execute(
        f"INSERT INTO signals ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}",
        row,
    )


def _participants(messages: list[SlackMessage], names: dict[str, str]) -> set[str]:
    """Graph user IDs of everyone who posted or reacted. Graph IDs are Slack user names."""
    slack_ids = {m.user for m in messages} | {u for m in messages for r in m.reactions for u in r.users}
    return {names.get(slack_id, slack_id) for slack_id in slack_ids}


def _content_hash(channel: str, messages: list[SlackMessage]) -> str:
    content = [
        {
            "ts": m.ts,
            "user": m.user,
            "text": m.text,
            "reactions": sorted((r.name, sorted(r.users)) for r in m.reactions),
        }
        for m in messages
    ]
    payload = json.dumps({"channel": channel, "messages": content}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _product_keywords(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Each product's ID and the model code that leads its name, such as "gripper" and "rg-2"."""
    return {
        row["id"]: [row["id"].lower(), row["name"].split()[0].lower()]
        for row in conn.execute("SELECT id, name FROM products ORDER BY id")
    }


def _tag_product(
    channel: str, messages: list[SlackMessage], keywords: dict[str, list[str]]
) -> str | None:
    """The product named by the channel, else the one named most often in the thread, else None."""

    def count(text: str, words: list[str]) -> int:
        return sum(len(re.findall(rf"\b{re.escape(w)}\b", text)) for w in words)

    by_channel = [p for p, words in keywords.items() if count(channel.lower(), words)]
    if len(by_channel) == 1:
        return by_channel[0]

    text = " ".join(m.text for m in messages).lower()
    counts = {p: count(text, words) for p, words in keywords.items()}
    best = max(counts.values(), default=0)
    winners = [p for p, n in counts.items() if n == best]
    return winners[0] if best and len(winners) == 1 else None
