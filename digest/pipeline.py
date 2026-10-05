"""The ingest loop shared by `digest ingest` and `digest eval`."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from digest.core.apply import InvalidDelta, apply
from digest.core.assemble import SlackExport, assemble, save_signal
from digest.core.extract import ExtractionFailed, graph_slice
from digest.core.fan_out import fan_out
from digest.models import Delta, Extractor

log = logging.getLogger(__name__)

# The A4 seam: applies one delta and returns it as applied (its target possibly resolved
# to a canonical ID), or None when nothing was applied and nothing should fan out.
ApplyFn = Callable[[sqlite3.Connection, Delta], Delta | None]


def core_apply(conn: sqlite3.Connection, delta: Delta) -> Delta | None:
    """The core `apply` as an ApplyFn: the delta itself when newly applied, else None."""
    return delta if apply(conn, delta) else None


@dataclass
class IngestResult:
    signals: int = 0
    deltas: int = 0
    skipped: int = 0


def ingest(
    conn: sqlite3.Connection,
    export: SlackExport,
    extractor: Extractor,
    apply_fn: ApplyFn = core_apply,
) -> IngestResult:
    """Run assemble, extract, apply and fan_out. A thread that fails validation is logged and skipped.

    A skipped thread's signal is not saved, so the next ingest retries it. Each delta fans out
    as soon as it is applied, so its recipients reflect the graph at that moment. `apply_fn`
    is the A4 seam; fan-out sees the delta it returns, so aliased targets fan out canonically.
    """
    result = IngestResult()
    for signal in assemble(conn, export):
        try:
            deltas = extractor.extract(signal, graph_slice(conn, signal.product_id))
            applied = 0
            with conn:
                save_signal(conn, signal)
                for delta in sorted(deltas, key=lambda d: (d.ts, d.id)):
                    if (applied_delta := apply_fn(conn, delta)) is not None:
                        fan_out(conn, applied_delta)
                        applied += 1
        except (ValidationError, InvalidDelta, ExtractionFailed) as e:
            log.error("skipping thread %s: %s", signal.id, e)
            result.skipped += 1
            continue
        result.signals += 1
        result.deltas += applied
    return result
