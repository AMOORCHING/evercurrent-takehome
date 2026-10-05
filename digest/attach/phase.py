"""A3 stage-aware focus: a ranker whose opinion is that focus comes from where a
person's work sits in the process, not from message text.

`PhaseRanker` scores each inbox item by stage distance - how many stages separate the
changed item from the stages the person owns in the product's active process - and by
proximity to that process's gate date:

    score = fan_out score * 1 / (1 + distance) * (1 + 1 / (1 + days to gate))

Items in the person's own stage or the one feeding it count as distance zero, so they
rank highest. Items whose stage sits outside the active process are the full process
length away: when a gate passes, last phase's items fall and the new phase's rise,
which is the day 5 / day 7 demo either side of EVT exit.

The active process for a product on the digest date is the one whose gate is next:
the process with the earliest gate date on or after the date, or the last process
once every gate has passed. The date itself comes from the items' delta timestamps,
so `rank` keeps the Ranker protocol.

Ties break on the hand-written role-by-phase table in data/phase_weights.yaml. The
file is plain YAML, but it is read here by a reader for just the two-level mapping
it uses, because the stack pins the dependency list and PyYAML is not on it.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from digest.core.rank import DEFAULT_LIMIT
from digest.models import InboxItem, Ranker, Scored, TargetKind, User

DEFAULT_WEIGHTS_PATH = Path("data/phase_weights.yaml")

# Stage distance when nothing locates the item or the person: a requirement no task
# implements, or a person owning no stage in the active process.
NEUTRAL_DISTANCE = 2

# Tiebreaker weight for a role or phase the table does not name.
NEUTRAL_TIE_WEIGHT = 0.5

# role -> phase name (EVT, DVT, PVT) -> tiebreaker weight in 0 to 1.
PhaseWeights = dict[str, dict[str, float]]


def load_phase_weights(path: Path = DEFAULT_WEIGHTS_PATH) -> PhaseWeights:
    """Read the role-by-phase table from its YAML file.

    Reads only the shape the file uses: an unindented `role:` line opens a role, each
    indented `PHASE: number` line sets one weight, and blank lines and `#` comments
    are skipped. Anything else raises ValueError with the line number.
    """
    weights: PhaseWeights = {}
    current: dict[str, float] | None = None
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        key, sep, value = line.strip().partition(":")
        if not sep or not key:
            raise ValueError(f"{path}:{lineno}: expected 'key:' or 'key: number', got {raw!r}")
        if line[0] not in " \t":
            if value.strip():
                raise ValueError(f"{path}:{lineno}: a role line takes no value, got {raw!r}")
            current = weights.setdefault(key, {})
            continue
        if current is None:
            raise ValueError(f"{path}:{lineno}: weight line before any role line")
        try:
            current[key] = float(value)
        except ValueError:
            raise ValueError(f"{path}:{lineno}: weight for {key!r} is not a number: {value.strip()!r}")
    return weights


@dataclass(frozen=True)
class PhaseProcess:
    id: str
    name: str
    gate_date: dt.date | None


@dataclass(frozen=True)
class PhaseGraph:
    """The slice of the graph the ranker needs, loaded once so `rank` stays off the database.

    `processes` lists each product's processes in gate order (no gate date sorts last).
    `stage_count` is per process; `owned` maps (process, user) to the stage positions
    that user owns; `stages` maps a stage to its (process, position).
    """

    processes: dict[str, tuple[PhaseProcess, ...]]
    stages: dict[str, tuple[str, int]]
    stage_count: dict[str, int]
    owned: dict[tuple[str, str], tuple[int, ...]]
    task_stage: dict[str, str]
    requirement_stages: dict[str, tuple[str, ...]]

    def active_process(self, product_id: str, date: dt.date) -> PhaseProcess | None:
        """The process whose gate is next on `date`: the earliest gate on or after it,
        or the last process once every gate has passed."""
        ordered = self.processes.get(product_id)
        if not ordered:
            return None
        for process in ordered:
            if process.gate_date is None or process.gate_date >= date:
                return process
        return ordered[-1]

    def target_stage_ids(self, kind: TargetKind, target_id: str) -> tuple[str, ...]:
        """The stages that locate a target: a task's own stage, or for a requirement
        the stages of the tasks implementing it."""
        if kind == "task":
            stage_id = self.task_stage.get(target_id)
            return (stage_id,) if stage_id else ()
        return self.requirement_stages.get(target_id, ())


def load_phase_graph(conn: sqlite3.Connection) -> PhaseGraph:
    processes: dict[str, list[PhaseProcess]] = {}
    rows = conn.execute("SELECT product_id, id, name, gate_date FROM processes").fetchall()
    for row in sorted(rows, key=lambda r: (r["product_id"], r["gate_date"] is None, r["gate_date"], r["id"])):
        gate = dt.date.fromisoformat(row["gate_date"]) if row["gate_date"] else None
        processes.setdefault(row["product_id"], []).append(PhaseProcess(row["id"], row["name"], gate))

    stages: dict[str, tuple[str, int]] = {}
    stage_count: dict[str, int] = {}
    owned: dict[tuple[str, str], list[int]] = {}
    for row in conn.execute("SELECT id, process_id, position, owner_id FROM stages ORDER BY process_id, position"):
        stages[row["id"]] = (row["process_id"], row["position"])
        stage_count[row["process_id"]] = stage_count.get(row["process_id"], 0) + 1
        if row["owner_id"]:
            owned.setdefault((row["process_id"], row["owner_id"]), []).append(row["position"])

    task_stage: dict[str, str] = {}
    requirement_stages: dict[str, list[str]] = {}
    for row in conn.execute("SELECT id, stage_id, requirement_id FROM tasks ORDER BY id"):
        if not row["stage_id"]:
            continue
        task_stage[row["id"]] = row["stage_id"]
        if row["requirement_id"]:
            requirement_stages.setdefault(row["requirement_id"], []).append(row["stage_id"])

    return PhaseGraph(
        processes={k: tuple(v) for k, v in processes.items()},
        stages=stages,
        stage_count=stage_count,
        owned={k: tuple(v) for k, v in owned.items()},
        task_stage=task_stage,
        requirement_stages={k: tuple(v) for k, v in requirement_stages.items()},
    )


class PhaseRanker(Ranker):
    """Scores by stage distance and gate proximity; the role-by-phase table breaks ties,
    then earlier change wins. Keeps the top `limit`, like the core ranker."""

    def __init__(self, graph: PhaseGraph, weights: PhaseWeights, limit: int = DEFAULT_LIMIT) -> None:
        self.graph = graph
        self.weights = weights
        self.limit = limit

    def rank(self, user: User, items: list[InboxItem]) -> list[Scored]:
        if not items:
            return []
        date = max(_utc_date(item.delta.ts) for item in items)
        keyed = []
        for item in items:
            active = self.graph.active_process(item.product_id, date)
            closeness = 1.0 / (1.0 + self._distance(user, item, active))
            score = item.row.score * closeness * self._gate_boost(active, date)
            phase = active.name if active else ""
            tie = self.weights.get(user.role, {}).get(phase, NEUTRAL_TIE_WEIGHT)
            keyed.append((score, tie, item))
        keyed.sort(key=lambda k: (-k[0], -k[1], k[2].delta.ts, k[2].delta.id))
        return [Scored(item=item, score=score) for score, _, item in keyed[: self.limit]]

    def _distance(self, user: User, item: InboxItem, active: PhaseProcess | None) -> int:
        """Stages between the changed item and the stages `user` owns in the active
        process. Own stage and the one feeding it are zero; a stage outside the active
        process is the whole process length away; nothing to locate is neutral."""
        if active is None:
            return NEUTRAL_DISTANCE
        stage_ids = self.graph.target_stage_ids(item.delta.target_kind, item.delta.target_id)
        owned = self.graph.owned.get((active.id, user.id), ())
        distances = []
        for stage_id in stage_ids:
            process_id, position = self.graph.stages[stage_id]
            if process_id != active.id:
                distances.append(self.graph.stage_count[active.id])
            elif not owned:
                distances.append(NEUTRAL_DISTANCE)
            else:
                distances.append(
                    min(0 if position in (p, p - 1) else abs(position - p) for p in owned)
                )
        return min(distances, default=NEUTRAL_DISTANCE)

    @staticmethod
    def _gate_boost(active: PhaseProcess | None, date: dt.date) -> float:
        """1 up to 2: the nearer the active process's gate, the higher every score."""
        if active is None or active.gate_date is None:
            return 1.0
        days = max(0, (active.gate_date - date).days)
        return 1.0 + 1.0 / (1.0 + days)


def _utc_date(ts: str) -> dt.date:
    return dt.datetime.fromtimestamp(float(ts), dt.UTC).date()
