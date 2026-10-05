"""`digest demo`: a replay-mode walkthrough of the whole pipeline, needing no API keys.

Everything runs in an in-memory database, so the demo writes nothing and leaves no state.
It shows, in order: one planted cross-team (silo) thread; the digests of the two affected
owners for that day; the --phase section (one user's digests on day 5 and day 7 either
side of the EVT gate, the live count of digests --phase reorders, and one day rendered
under both rankers side by side); an explain trace for one silo digest item; and
results.md as committed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from digest.attach.deciders import Cascade, PassThroughDecider, save_decisions
from digest.attach.phase import PhaseRanker, load_phase_graph, load_phase_weights
from digest.core.assemble import SlackExport, threads
from digest.core.extract import ReplayExtractor
from digest.core.rank import FixedWeightRanker, load_inbox
from digest.core.render import TemplateRenderer, display_order
from digest.db import connect, create_schema, load_graph_seed
from digest.eval import Gold, GoldThread
from digest.explain import explain
from digest.models import User
from digest.pipeline import ingest

# The user the --phase section follows. Tom owns sourcing stages in both products'
# processes, so his are the only real digests --phase reorders on this dataset, and he
# is also an affected owner in the first silo case, keeping one thread of narrative
# through the demo.
PHASE_USER = "tom"
DAY_5, DAY_7 = 5, 7


def demo(data_dir: Path, results_path: Path) -> str:
    export = SlackExport.model_validate_json((data_dir / "slack.json").read_text())
    gold = Gold.model_validate_json((data_dir / "gold.json").read_text())
    case = _silo_case(gold)

    conn = connect(":memory:")
    try:
        create_schema(conn)
        load_graph_seed(conn, data_dir / "graph_seed.json")
        cascade = Cascade(PassThroughDecider(), ReplayExtractor(data_dir / "gold.json"))
        result = ingest(conn, export, cascade)
        save_decisions(conn, cascade, "passthrough")

        lines = [
            "# Digest demo",
            "",
            "Replay mode: gold deltas, PassThroughDecider, no API keys, an in-memory",
            "database. Nothing on disk changes.",
            "",
            f"Ingested {result.signals} signals, {result.deltas} deltas applied, "
            f"{result.skipped} threads skipped.",
            "",
        ]
        lines += _silo_section(conn, export, case)
        lines += _phase_section(conn, data_dir)
        lines += _explain_section(conn, case)
        lines += _results_section(results_path)
    finally:
        conn.close()
    return "\n".join(lines).rstrip("\n") + "\n"


def _silo_case(gold: Gold) -> GoldThread:
    """The first planted cross-team case that reaches at least two people, so the demo
    can show two digests; any cross-team case failing that, the first one."""
    cases = [t for t in gold.threads if t.kind == "cross_team"]
    if not cases:
        raise ValueError("gold.json has no cross_team case to demo")
    return next((t for t in cases if len(_affected(t)) >= 2), cases[0])


def _affected(case: GoldThread) -> list[str]:
    return list(dict.fromkeys(user for delta in case.deltas for user in delta.affected))


def _case_date(conn: sqlite3.Connection, case: GoldThread) -> dt.date:
    ts = conn.execute("SELECT MIN(ts) FROM deltas WHERE signal_id = ?", (case.thread,)).fetchone()[0]
    return dt.datetime.fromtimestamp(float(ts), dt.UTC).date()


def _user(conn: sqlite3.Connection, user_id: str) -> User:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User.model_validate(dict(row))


def _digest(conn: sqlite3.Connection, user: User, date: dt.date, ranker) -> str:
    return TemplateRenderer().render(user, ranker.rank(user, load_inbox(conn, user.id, date)))


def _silo_section(conn: sqlite3.Connection, export: SlackExport, case: GoldThread) -> list[str]:
    date = _case_date(conn, case)
    affected = _affected(case)
    names = {u.id: u.name for u in export.users}
    lines = [
        f"## 1. A silo case: planted case {case.case or '?'}, thread {case.thread}",
        "",
        *(f"> {names.get(m.user, m.user)}: {m.text}" for m in threads(export)[case.thread]),
        "",
        f"Gold plants {len(case.deltas)} state change(s) here. The affected owners -",
        f"{', '.join(affected)} - never appear in the thread, which is the silo the",
        "digest exists to cross.",
        "",
        f"## 2. Who the change reached on {date.isoformat()}",
        "",
    ]
    for user_id in affected[:2]:
        lines += [_digest(conn, _user(conn, user_id), date, FixedWeightRanker())]
    return lines


def _phase_section(conn: sqlite3.Connection, data_dir: Path) -> list[str]:
    dates = sorted(
        {
            dt.datetime.fromtimestamp(float(r["ts"]), dt.UTC).date()
            for r in conn.execute("SELECT ts FROM deltas")
        }
    )
    if len(dates) < DAY_7:
        return [f"## 3. Stage-aware focus: skipped, the dataset has only {len(dates)} days", ""]
    day5, day7 = dates[DAY_5 - 1], dates[DAY_7 - 1]
    gates = conn.execute(
        "SELECT product_id, name, gate_date FROM processes "
        "WHERE gate_date > ? AND gate_date < ? ORDER BY gate_date",
        (day5.isoformat(), day7.isoformat()),
    ).fetchall()
    user = _user(conn, PHASE_USER)
    core = FixedWeightRanker()
    ranker = PhaseRanker(load_phase_graph(conn), load_phase_weights(data_dir / "phase_weights.yaml"))
    lines = [f"## 3. Stage-aware focus (--phase): {user.id} on day {DAY_5} and day {DAY_7}", ""]
    for gate in gates:
        lines.append(
            f"The {gate['product_id']} {gate['name']} gate passes on {gate['gate_date']}, "
            f"between day {DAY_5} ({day5.isoformat()}) and day {DAY_7} ({day7.isoformat()})."
        )
    if gates:
        lines.append("")
    for label, day in ((f"Day {DAY_5}", day5), (f"Day {DAY_7}", day7)):
        lines += [f"### {label}: {day.isoformat()}", "", _digest(conn, user, day, ranker)]

    total, changed = _order_changes(conn, dates, core, ranker)
    lines += [
        "To be clear about what A3 does and does not do here: a digest covers one day's",
        "changes, so the day 5 and day 7 digests above hold different items under any",
        "ranker - that difference is the data. What --phase adds is ordering within a",
        f"day, and on this dataset it reorders {len(changed)} of {total} digests. The",
        "gate flip itself - a passing gate demoting last phase's item below the new",
        "phase's - needs one inbox holding items from both phases, which no real day",
        "here produces; tests/test_phase.py pins that mechanism with constructed items.",
        "",
    ]
    comparison = next((c for c in changed if c[0] == user.id), changed[0] if changed else None)
    if comparison is not None:
        shown_id, shown_day = comparison
        shown_user = _user(conn, shown_id)
        lines += [
            f"### One day, both rankers: {shown_id} on {shown_day.isoformat()}",
            "",
            "Core ranker (fixed weights):",
            "",
            _digest(conn, shown_user, shown_day, core),
            "The same day with --phase:",
            "",
            _digest(conn, shown_user, shown_day, ranker),
        ]
    return lines


def _order_changes(
    conn: sqlite3.Connection, dates: list[dt.date], core: FixedWeightRanker, phase: PhaseRanker
) -> tuple[int, list[tuple[str, dt.date]]]:
    """Every (user, day) pair with a non-empty inbox, and the pairs where --phase orders
    the digest differently from the core ranker."""
    total, changed = 0, []
    for row in conn.execute("SELECT * FROM users ORDER BY id").fetchall():
        user = User.model_validate(dict(row))
        for day in dates:
            items = load_inbox(conn, user.id, day)
            if not items:
                continue
            total += 1
            if [s.item.delta.id for s in core.rank(user, items)] != [
                s.item.delta.id for s in phase.rank(user, items)
            ]:
                changed.append((user.id, day))
    return total, changed


def _explain_section(conn: sqlite3.Connection, case: GoldThread) -> list[str]:
    """The explain trace for the silo item in the first affected owner's digest."""
    date = _case_date(conn, case)
    user = _user(conn, _affected(case)[0])
    ranker = FixedWeightRanker()
    items = display_order(ranker.rank(user, load_inbox(conn, user.id, date)))
    number = next(
        (n for n, s in enumerate(items, start=1) if s.item.delta.signal_id == case.thread), 1
    )
    return [
        f"## 4. The explain trace: digest explain --user {user.id} "
        f"--date {date.isoformat()} --item {number}",
        "",
        explain(conn, user, date, number, ranker),
    ]


def _results_section(results_path: Path) -> list[str]:
    lines = [f"## 5. The results table: {results_path.as_posix()}", ""]
    if results_path.exists():
        lines.append(results_path.read_text())
    else:
        lines.append(f"{results_path.as_posix()} not found; run `digest eval` to write it.")
    return lines
