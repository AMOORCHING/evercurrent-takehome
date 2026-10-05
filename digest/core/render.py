"""`render`: scored items to a digest body, the second half of `rank_and_render`."""

from __future__ import annotations

import sqlite3

from digest.models import Digest, InboxItem, Renderer, Scored, User


class TemplateRenderer(Renderer):
    """Markdown with one card per item, grouped by product.

    Products appear in the order of their best-ranked item, and cards keep rank order within a
    product. Cards are numbered in display order.
    """

    def render(self, user: User, items: list[Scored]) -> str:
        lines = [f"# Digest for {user.name}", ""]
        if not items:
            lines.append("No changes reached you.")
            return "\n".join(lines) + "\n"

        last_product = None
        for number, scored in enumerate(display_order(items), start=1):
            item = scored.item
            if item.product_id != last_product:
                lines += [f"## {item.product_name}", ""]
                last_product = item.product_id
            lines += _card(number, item) + [""]
        return "\n".join(lines).rstrip("\n") + "\n"


def display_order(items: list[Scored]) -> list[Scored]:
    """Scored items in the order the template numbers their cards: grouped by product in
    the order of each product's best-ranked item, rank order within a product. `digest
    explain` counts items the same way, so its numbers match the printed digest."""
    by_product: dict[str, list[Scored]] = {}
    for scored in items:
        by_product.setdefault(scored.item.product_id, []).append(scored)
    return [scored for group in by_product.values() for scored in group]


def save_digest(conn: sqlite3.Connection, digest: Digest) -> None:
    """Insert or overwrite the digest for its user and date. Does not commit."""
    conn.execute(
        "INSERT INTO digests (user_id, date, body) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, date) DO UPDATE SET body = excluded.body",
        (digest.user_id, digest.date.isoformat(), digest.body),
    )


def _card(number: int, item: InboxItem) -> list[str]:
    delta = item.delta
    lines = [
        f"### {number}. {delta.target_id} {item.target_title}: {delta.type.replace('_', ' ')}",
        f"- Before: {delta.old_value or '(none)'}",
        f"- After: {delta.new_value or '(none)'}",
        f"- Source: #{item.signal.channel} {item.source_url}",
        f"- Why it reaches you: {item.row.reason}",
    ]
    if not item.conflicts:
        return lines + ["- Potential conflicts: none found"]
    return lines + ["- Potential conflicts:"] + [f"  - {c}" for c in item.conflicts]
