"""`digest eval`: score each configuration of the pipeline against the gold labels.

Configurations and metrics are both registries. The results table has one row per metric and
configuration, so adding either one adds rows and leaves the table's shape alone.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from digest.attach import calibrate
from digest.attach.aliases import resolving_apply
from digest.attach.deciders import Cascade, DeciderUnavailable, build_decider
from digest.attach.phase import PhaseRanker, load_phase_graph, load_phase_weights
from digest.core.assemble import SlackExport
from digest.core.extract import MODEL_ENV, ExtractorUnavailable, LLMExtractor, ReplayExtractor
from digest.core.rank import FixedWeightRanker, load_inbox
from digest.db import connect, create_schema, load_graph_seed
from digest.models import Decider, Decision, Delta, Extractor, GraphSlice, Ranker, Scored, Signal, User
from digest.pipeline import core_apply, ingest

# A delta is identified by what it changes, not by its ID, so extractors that mint their own
# IDs still match gold: (signal ID, target kind, target ID, delta type).
MatchKey = tuple[str, str, str, str]

# What one extracted delta changes and to what: (target kind, target ID, delta type, new value).
Change = tuple[str, str, str, str]


class GoldDelta(BaseModel):
    type: str
    target_kind: str
    target_id: str
    new_value: str
    affected: list[str]
    contradicts: bool = False


class GoldThread(BaseModel):
    thread: str
    kind: str
    deltas: list[GoldDelta]
    case: str | None = None


class Gold(BaseModel):
    threads: list[GoldThread]

    def affected(self) -> dict[MatchKey, set[str]]:
        return {
            (t.thread, d.target_kind, d.target_id, d.type): set(d.affected)
            for t in self.threads
            for d in t.deltas
        }


@dataclass(frozen=True)
class Ratio:
    hits: int
    total: int

    def __str__(self) -> str:
        if self.total == 0:
            return "n/a (0 of 0)"
        return f"{100 * self.hits / self.total:.1f}% ({self.hits} of {self.total})"


@dataclass(frozen=True)
class NotRun:
    """Stands in for every value of a configuration that could not run, such as one needing a key."""

    reason: str

    def __str__(self) -> str:
        return f"not run: {self.reason}"


@dataclass(frozen=True)
class Seconds:
    value: float

    def __str__(self) -> str:
        return f"{1000 * self.value:.0f} ms"


@dataclass(frozen=True)
class Money:
    value: float

    def __str__(self) -> str:
        return f"${self.value:.4f}"


Value = Ratio | Seconds | Money | calibrate.Calibration | NotRun


@dataclass(frozen=True)
class Configuration:
    """`extractor` raises `ExtractorUnavailable` when the configuration cannot run here;
    `decider` and `escalation`, when set, raise `DeciderUnavailable` the same way, and
    `ranker` raises `RankerUnavailable`. `escalation` is the second-stage decider that
    settles the middle band. `ranker` is built after ingest, on the dataset directory
    and the ingested database, since a ranker like A3's reads the graph once."""

    name: str
    description: str
    extractor: Callable[[Path], Extractor]
    ranker: Callable[[Path, sqlite3.Connection], Ranker]
    decider: Callable[[Path], Decider] | None = None
    escalation: Callable[[Path], Decider] | None = None
    resolve_aliases: bool = False


@dataclass(frozen=True)
class Run:
    """What one configuration produced: every user's digest items for every day with a change,
    what the extractor returned for each thread it extracted without raising, and what the
    decider answered, routed and spent when the configuration has one."""

    gold: Gold
    digests: dict[tuple[str, dt.date], list[Scored]]
    extracted: dict[str, list[Delta]] = field(default_factory=dict)
    decisions: dict[str, Decision] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    decider_latencies: list[float] = field(default_factory=list)
    decider_cost_usd: float = 0.0
    # (surface, target_kind, signal_id) rows from the unresolved table after ingest (A4).
    unresolved: list[tuple[str, str, str]] = field(default_factory=list)

    def delivered(self) -> list[tuple[str, MatchKey]]:
        """(user ID, match key) for each digest item, across every digest."""
        return [
            (user_id, (d.signal_id, d.target_kind, d.target_id, d.type))
            for (user_id, _), items in sorted(self.digests.items())
            for d in (s.item.delta for s in items)
        ]


@dataclass(frozen=True)
class Metric:
    name: str
    definition: str
    score: Callable[[Run], Value]


@dataclass(frozen=True)
class Row:
    metric: str
    configuration: str
    value: Value


def silo_recall(run: Run) -> Ratio:
    delivered = set(run.delivered())
    wanted = [
        (user_id, (t.thread, d.target_kind, d.target_id, d.type))
        for t in run.gold.threads
        if t.kind == "cross_team"
        for d in t.deltas
        for user_id in d.affected
    ]
    return Ratio(sum(pair in delivered for pair in wanted), len(wanted))


def digest_precision(run: Run) -> Ratio:
    affected = run.gold.affected()
    delivered = run.delivered()
    return Ratio(sum(user_id in affected.get(key, ()) for user_id, key in delivered), len(delivered))


def extractor_accuracy(run: Run) -> Ratio:
    gold = {t.thread: _changes(t.deltas) for t in run.gold.threads}
    hits = sum(
        thread in run.extracted and _changes(run.extracted[thread]) == expected
        for thread, expected in gold.items()
    )
    return Ratio(hits, len(gold))


def _changes(deltas: Sequence[GoldDelta | Delta]) -> set[Change]:
    """New values compare with case and runs of whitespace ignored."""
    return {
        (d.target_kind, d.target_id, d.type, " ".join((d.new_value or "").split()).casefold())
        for d in deltas
    }


def _decided(run: Run) -> list[tuple[Decision, GoldThread]]:
    gold = {t.thread: t for t in run.gold.threads}
    return [(d, gold[thread]) for thread, d in sorted(run.decisions.items()) if thread in gold]


def changes_state_accuracy(run: Run) -> Value:
    if not run.routes:
        return NotRun("no decider")
    pairs = _decided(run)
    hits = sum((d.changes_state >= 0.5) == bool(t.deltas) for d, t in pairs)
    return Ratio(hits, len(pairs))


def change_type_accuracy(run: Run) -> Value:
    if not run.routes:
        return NotRun("no decider")
    pairs = [(d, t) for d, t in _decided(run) if t.deltas and d.change_type]
    hits = sum(
        max(d.change_type, key=d.change_type.__getitem__) in {x.type for x in t.deltas}
        for d, t in pairs
    )
    return Ratio(hits, len(pairs))


def contradicts_accuracy(run: Run) -> Value:
    if not run.routes:
        return NotRun("no decider")
    pairs = _decided(run)
    hits = sum((d.contradicts >= 0.5) == any(x.contradicts for x in t.deltas) for d, t in pairs)
    return Ratio(hits, len(pairs))


def risk_accuracy(run: Run) -> Value:
    return NotRun("no decider") if not run.routes else NotRun("gold has no risk labels")


def _calibration_pairs(run: Run) -> dict[str, calibrate.Pair]:
    """Per decided gold thread: the backend's changes_state and whether gold has a delta."""
    gold = {t.thread: bool(t.deltas) for t in run.gold.threads}
    return {
        thread: (d.changes_state, gold[thread])
        for thread, d in run.decisions.items()
        if thread in gold
    }


def calibration_error(run: Run) -> Value:
    if not run.routes:
        return NotRun("no decider")
    pairs = _calibration_pairs(run)
    if not pairs:
        return NotRun("no decisions on gold threads")
    return calibrate.calibrate(pairs)


def escalation_rate(run: Run) -> Value:
    if not run.routes:
        return NotRun("no decider")
    return Ratio(sum(r == "escalate" for r in run.routes.values()), len(run.routes))


def decider_latency(run: Run) -> Value:
    if not run.decider_latencies:
        return NotRun("no decider")
    return Seconds(statistics.median(run.decider_latencies))


def cost_per_digest(run: Run) -> Value:
    if not run.routes:
        return NotRun("no decider")
    produced = sum(1 for items in run.digests.values() if items)
    if produced == 0:
        return NotRun("no digests produced")
    return Money(run.decider_cost_usd / produced)


def _llm_extractor(data_dir: Path) -> Extractor:
    return LLMExtractor.from_env(SlackExport.model_validate_json((data_dir / "slack.json").read_text()))


def _replay_extractor(data_dir: Path) -> Extractor:
    return ReplayExtractor(data_dir / "gold.json")


def _fixed_ranker(data_dir: Path, conn: sqlite3.Connection) -> Ranker:
    return FixedWeightRanker()


def _calibrated_ranker(data_dir: Path, conn: sqlite3.Connection) -> Ranker:
    return calibrate.CalibratedRanker()


def _phase_ranker(data_dir: Path, conn: sqlite3.Connection) -> Ranker:
    """A3's ranker over the ingested graph; a missing or malformed weights file makes
    the configuration `NotRun` rather than ending the whole eval."""
    weights_path = data_dir / "phase_weights.yaml"
    try:
        return PhaseRanker(load_phase_graph(conn), load_phase_weights(weights_path))
    except (OSError, ValueError) as e:
        raise calibrate.RankerUnavailable(f"cannot load {weights_path}: {e}")


def _a1_decider(backend: str) -> Callable[[Path], Decider]:
    def factory(data_dir: Path) -> Decider:
        export = SlackExport.model_validate_json((data_dir / "slack.json").read_text())
        return build_decider(backend, export)

    return factory


CONFIGURATIONS: list[Configuration] = [
    Configuration(
        name="core",
        description="Replay extractor (gold deltas), core fan-out, fixed-weight ranker, top five.",
        extractor=lambda data_dir: ReplayExtractor(data_dir / "gold.json"),
        ranker=_fixed_ranker,
    ),
    Configuration(
        name="llm",
        description=(
            f"LLMExtractor on {os.environ.get(MODEL_ENV) or f'the model named by {MODEL_ENV}'}, "
            "core fan-out, fixed-weight ranker, top five."
        ),
        extractor=_llm_extractor,
        ranker=_fixed_ranker,
    ),
    Configuration(
        name="a1-passthrough",
        description=(
            "A1 baseline: replay extractor behind the cascade with PassThroughDecider, so "
            "every thread goes to the extractor."
        ),
        extractor=_replay_extractor,
        ranker=_fixed_ranker,
        decider=_a1_decider("passthrough"),
    ),
    Configuration(
        name="a1-jev",
        description=(
            "A1 cascade with JevDecider (TypeSafe jev-latest, all four questions in one call), "
            "replay extractor past the gate. Needs JEV_API_KEY."
        ),
        extractor=_replay_extractor,
        ranker=_fixed_ranker,
        decider=_a1_decider("jev"),
    ),
    Configuration(
        name="a1-llm",
        description=(
            "A1 cascade with LLMDecider (structured output on the model named by "
            "DIGEST_DECIDER_MODEL), replay extractor past the gate. Needs "
            "DIGEST_DECIDER_MODEL and OPENAI_API_KEY."
        ),
        extractor=_replay_extractor,
        ranker=_fixed_ranker,
        decider=_a1_decider("llm"),
    ),
    Configuration(
        name="a1-jev-llm",
        description=(
            "A1 two-stage cascade, added after the first eval round (EXPERIMENTS.md): jev "
            "gates every thread, the 0.2-0.8 middle band is re-decided by the LLM decider, "
            "and that answer is final. Replay extractor past the gate. Needs JEV_API_KEY, "
            "DIGEST_DECIDER_MODEL and OPENAI_API_KEY."
        ),
        extractor=_replay_extractor,
        ranker=_fixed_ranker,
        decider=_a1_decider("jev"),
        escalation=_a1_decider("llm"),
    ),
    Configuration(
        name="a2-calibrated",
        description=(
            "A2: replay extractor, calibrated ranker. Each section keeps every item whose "
            "confidence clears its cost-ratio threshold (10:1 needs-you, 3:1 affects-you, "
            "1:1 FYI) instead of a top-five cut; no decider, so temperature stays 1.0."
        ),
        extractor=_replay_extractor,
        ranker=_calibrated_ranker,
    ),
    Configuration(
        name="a3-phase",
        description=(
            "A3: replay extractor, stage-aware ranker (--phase). Scores by stage distance "
            "to the stages the person owns in the product's active process, raised by "
            "proximity to its gate; phase_weights.yaml beside the dataset breaks ties. "
            "Top five, like core."
        ),
        extractor=_replay_extractor,
        ranker=_phase_ranker,
    ),
    Configuration(
        name="a4-aliases",
        description=(
            "A4: LLMExtractor plus alias resolution in apply (--aliases); compare against "
            "the llm row, since the replay extractor already emits canonical IDs and gives "
            "aliases nothing to do. Surface names resolve through the aliases table before "
            "a delta is matched; names resolving nowhere land in the unresolved table and "
            "are listed below. Needs DIGEST_EXTRACTOR_MODEL and OPENAI_API_KEY."
        ),
        extractor=_llm_extractor,
        ranker=_fixed_ranker,
        resolve_aliases=True,
    ),
]

METRICS: list[Metric] = [
    Metric(
        name="Silo recall",
        definition=(
            "Of the (cross-team case, affected person) pairs in gold, the share where the change "
            "appears in that person's digest."
        ),
        score=silo_recall,
    ),
    Metric(
        name="Digest precision",
        definition=(
            "Of all items in every person's digest on every day, the share that match a gold "
            "delta listing that person as affected."
        ),
        score=digest_precision,
    ),
    Metric(
        name="Extractor accuracy",
        definition=(
            "Of all threads, the share where the extractor returned exactly the gold changes: the "
            "same targets, delta types and new values, and nothing for a thread with none. A thread "
            "skipped for a malformed response counts as a miss."
        ),
        score=extractor_accuracy,
    ),
    Metric(
        name="Decider accuracy: changes_state",
        definition=(
            "Of decided threads, the share where the decider's changes_state at or above 0.5 "
            "matches whether gold has any delta for the thread."
        ),
        score=changes_state_accuracy,
    ),
    Metric(
        name="Decider accuracy: change_type",
        definition=(
            "Of decided threads with a gold delta and a change_type answer, the share where "
            "the highest-probability type matches a gold delta's type."
        ),
        score=change_type_accuracy,
    ),
    Metric(
        name="Decider accuracy: contradicts",
        definition=(
            "Of decided threads, the share where the decider's contradicts at or above 0.5 "
            "matches whether gold marks a delta as contradicting recorded state."
        ),
        score=contradicts_accuracy,
    ),
    Metric(
        name="Decider accuracy: risk",
        definition="Not scorable: gold carries no risk labels, so the risk answer is reported unscored.",
        score=risk_accuracy,
    ),
    Metric(
        name="Calibration error",
        definition=(
            "Expected calibration error (10 bins) of changes_state against whether gold has "
            "a delta for the thread, measured on the two thirds of threads not used for "
            "fitting; before and after temperature scaling fit on the fixed seeded third "
            "(digest/attach/calibrate.py). Reliability plots sit beside results.md under "
            "calibration/."
        ),
        score=calibration_error,
    ),
    Metric(
        name="Escalation rate",
        definition=(
            "Of decided threads, the share with changes_state between 0.2 and 0.8. Without "
            "an escalation decider they go to the extractor and are logged; with one, the "
            "second opinion settles them, so this is the share paying for the second call."
        ),
        score=escalation_rate,
    ),
    Metric(
        name="Decider latency (median)",
        definition="Median wall-clock time of one decide call across all decided threads.",
        score=decider_latency,
    ),
    Metric(
        name="Cost per digest",
        definition=(
            "Decider spend for the full run divided by digests with at least one item. The "
            "replay extractor is free; token prices are the placeholder estimates in "
            "digest/attach/deciders.py."
        ),
        score=cost_per_digest,
    ),
]


class _Recording(Extractor):
    """Keeps what the wrapped extractor returned, before `apply` rewrites old values."""

    def __init__(self, inner: Extractor) -> None:
        self.inner = inner
        self.extracted: dict[str, list[Delta]] = {}

    def extract(self, signal: Signal, ctx: GraphSlice) -> list[Delta]:
        deltas = self.inner.extract(signal, ctx)
        self.extracted[signal.id] = deltas
        return deltas


def run_configuration(data_dir: Path, config: Configuration) -> Run:
    """Ingest the dataset into a fresh in-memory database and rank every user's digest for every
    day that has a change. Raises `ExtractorUnavailable` or `DeciderUnavailable` before
    ingesting anything."""
    inner = config.extractor(data_dir)
    cascade = None
    if config.decider is not None:
        decider = config.decider(data_dir)
        escalation = config.escalation(data_dir) if config.escalation is not None else None
        cascade = Cascade(decider, inner, escalation=escalation)
        inner = cascade
    # Recording wraps the cascade, so a dropped thread records an empty extraction and a
    # correct drop still counts for extractor accuracy.
    extractor = _Recording(inner)
    conn = connect(":memory:")
    try:
        create_schema(conn)
        load_graph_seed(conn, data_dir / "graph_seed.json")
        export = SlackExport.model_validate_json((data_dir / "slack.json").read_text())
        ingest(conn, export, extractor,
               apply_fn=resolving_apply if config.resolve_aliases else core_apply)
        unresolved = [
            (r["surface"], r["target_kind"], r["signal_id"])
            for r in conn.execute("SELECT * FROM unresolved ORDER BY surface, target_kind, signal_id")
        ]

        users = [User.model_validate(dict(r)) for r in conn.execute("SELECT * FROM users ORDER BY id")]
        dates = sorted(
            {dt.datetime.fromtimestamp(float(r["ts"]), dt.UTC).date() for r in conn.execute("SELECT ts FROM deltas")}
        )
        ranker = config.ranker(data_dir, conn)
        digests = {
            (user.id, date): ranker.rank(user, load_inbox(conn, user.id, date))
            for user in users
            for date in dates
        }
    finally:
        conn.close()
    gold = Gold.model_validate_json((data_dir / "gold.json").read_text())
    return Run(
        gold=gold,
        digests=digests,
        extracted=extractor.extracted,
        decisions=cascade.decisions if cascade else {},
        routes=cascade.routes if cascade else {},
        decider_latencies=cascade.latencies if cascade else [],
        decider_cost_usd=cascade.cost_usd if cascade else 0.0,
        unresolved=unresolved,
    )


def collect_runs(data_dir: Path, configs: Sequence[Configuration]) -> dict[str, Run | NotRun]:
    """One `Run` per configuration, or `NotRun` for one that cannot run here."""
    runs: dict[str, Run | NotRun] = {}
    for config in configs:
        try:
            runs[config.name] = run_configuration(data_dir, config)
        except (ExtractorUnavailable, DeciderUnavailable, calibrate.RankerUnavailable) as e:
            runs[config.name] = NotRun(str(e))
    return runs


def score_rows(
    runs: dict[str, Run | NotRun], configs: Sequence[Configuration], metrics: Sequence[Metric]
) -> list[Row]:
    """One row per metric and configuration, grouped by metric so configurations sit side by side."""

    def value(metric: Metric, run: Run | NotRun) -> Value:
        return run if isinstance(run, NotRun) else metric.score(run)

    return [
        Row(metric=metric.name, configuration=config.name, value=value(metric, runs[config.name]))
        for metric in metrics
        for config in configs
    ]


def evaluate(data_dir: Path, configs: Sequence[Configuration], metrics: Sequence[Metric]) -> list[Row]:
    """`collect_runs` then `score_rows`: a configuration whose extractor or decider is
    unavailable gets a `NotRun` value in each of its rows."""
    return score_rows(collect_runs(data_dir, configs), configs, metrics)


def write_reliability_plots(runs: dict[str, Run | NotRun], directory: Path) -> list[Path]:
    """One reliability SVG per configuration whose decider decided gold threads, named
    reliability-<configuration>.svg. Overwrites in place, so rerunning changes nothing."""
    paths = []
    for name, run in runs.items():
        if isinstance(run, NotRun):
            continue
        pairs = _calibration_pairs(run)
        if not pairs:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"reliability-{name}.svg"
        path.write_text(calibrate.reliability_svg(calibrate.calibrate(pairs), title=name))
        paths.append(path)
    return paths


def unresolved_names(runs: dict[str, Run | NotRun]) -> dict[str, list[tuple[str, str, str]]]:
    """Per configuration that ran and recorded any: the unresolved-name rows (A4)."""
    return {
        name: run.unresolved
        for name, run in runs.items()
        if isinstance(run, Run) and run.unresolved
    }


def results_markdown(
    rows: Sequence[Row],
    configs: Sequence[Configuration],
    metrics: Sequence[Metric],
    gold_path: Path,
    plots: Sequence[str] = (),
    unresolved: dict[str, list[tuple[str, str, str]]] | None = None,
) -> str:
    lines = [
        "# Results",
        "",
        f"Written by `digest eval` against `{gold_path.as_posix()}`. Regenerate rather than edit.",
        "Each value is a percentage with the counts behind it. With about 120 threads, gaps of a",
        "few points are noise.",
        "",
        "| Metric | Configuration | Value |",
        "| --- | --- | --- |",
        *(f"| {r.metric} | {r.configuration} | {r.value} |" for r in rows),
        "",
        "## Metrics",
        "",
        *(f"- **{m.name}**: {m.definition}" for m in metrics),
        "",
        "## Configurations",
        "",
        *(f"- **{c.name}**: {c.description}" for c in configs),
        "",
        "## Unresolved names",
        "",
        "Names `apply` could not match to a task or requirement, even through the aliases",
        "table (A4). Each is fixed once by adding an alias row to the graph seed.",
        "",
    ]
    if unresolved:
        lines += [
            f"- **{name}**: " + "; ".join(f"{s!r} ({kind}) in {signal}" for s, kind, signal in names)
            for name, names in sorted(unresolved.items())
        ]
    else:
        lines.append("None recorded by any configuration that ran.")
    if plots:
        lines += [
            "",
            "## Reliability plots",
            "",
            "One per decider backend that ran, written beside this file:",
            "",
            *(f"- ![{Path(p).stem}]({p})" for p in plots),
        ]
    return "\n".join(lines) + "\n"
