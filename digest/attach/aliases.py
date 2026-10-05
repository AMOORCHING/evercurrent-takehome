"""A4 entity aliases: cross-tool names are a data problem to solve once, at write time.

`resolving_apply` wraps the core `apply` through the pipeline's ApplyFn seam, behind
`--aliases`. A delta whose target_id is no task or requirement ID is looked up in the
`aliases` table - surfaces compare normalized, see `normalize_surface` - and applied
under its canonical ID; fan-out then sees the canonical delta.

A name that resolves nowhere is recorded in the `unresolved` table and only that delta
is dropped - the rest of the thread still applies and the signal is saved. The thread
is deliberately not skipped the way a malformed response is: skipping rolls back the
transaction that holds the unresolved row, and an unknown name is not a broken
response but exactly the data gap this attachment exists to surface. `digest eval`
lists the recorded names so each one can be fixed once with a new alias row.
"""

from __future__ import annotations

import logging
import sqlite3

from digest.core.apply import apply
from digest.db import insert_or_ignore
from digest.models import Delta, UnresolvedName, normalize_surface

log = logging.getLogger(__name__)

_TABLES = {"task": "tasks", "requirement": "requirements"}


def resolve(conn: sqlite3.Connection, delta: Delta) -> Delta | None:
    """The delta ready for `apply`, its target_id canonical, or None once the unknown
    name is recorded in `unresolved`. Does not commit; the caller owns the transaction."""
    if conn.execute(
        f"SELECT 1 FROM {_TABLES[delta.target_kind]} WHERE id = ?", (delta.target_id,)
    ).fetchone():
        return delta

    row = conn.execute(
        "SELECT target_id FROM aliases WHERE surface = ? AND target_kind = ?",
        (normalize_surface(delta.target_id), delta.target_kind),
    ).fetchone()
    if row:
        log.info(
            "delta %s: resolved %r to %s %s",
            delta.id, delta.target_id, delta.target_kind, row["target_id"],
        )
        return delta.model_copy(update={"target_id": row["target_id"]})

    insert_or_ignore(
        conn,
        "unresolved",
        UnresolvedName(surface=delta.target_id, target_kind=delta.target_kind, signal_id=delta.signal_id),
    )
    log.warning(
        "delta %s: no %s named %r; recorded as unresolved",
        delta.id, delta.target_kind, delta.target_id,
    )
    return None


def resolving_apply(conn: sqlite3.Connection, delta: Delta) -> Delta | None:
    """An ApplyFn: resolve the target name, then apply. Returns the delta as applied,
    or None when the name is unresolved or the delta ID was already applied."""
    resolved = resolve(conn, delta)
    if resolved is None:
        return None
    return resolved if apply(conn, resolved) else None
