"""Pydantic records, one per SQLite table, and the four protocols attachments plug into.

Table record field names match column names.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProcessName = Literal["EVT", "DVT", "PVT"]
TargetKind = Literal["task", "requirement"]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class User(Record):
    id: str
    name: str
    role: str
    team: str


class Product(Record):
    id: str
    name: str


class Process(Record):
    id: str
    product_id: str
    name: ProcessName
    gate_date: dt.date | None = None
    status: str


class Stage(Record):
    id: str
    process_id: str
    name: str
    position: int
    owner_id: str | None = None


class Requirement(Record):
    id: str
    product_id: str
    title: str
    value: str
    owner_id: str | None = None


class RequirementLink(Record):
    requirement_id: str
    affected_requirement_id: str
    note: str | None = None


class Task(Record):
    id: str
    title: str
    owner_id: str | None = None
    assigner_id: str | None = None
    product_id: str
    stage_id: str | None = None
    requirement_id: str | None = None
    deadline: dt.date | None = None
    status: str


class Handoff(Record):
    upstream_task_id: str
    downstream_task_id: str


class Signal(Record):
    id: str
    source: str
    channel: str
    product_id: str | None = None
    content_hash: str
    last_ts: str
    participants: list[str] = Field(default_factory=list)


class Delta(Record):
    id: str
    signal_id: str
    type: str
    target_kind: TargetKind
    target_id: str
    old_value: str | None = None
    new_value: str | None = None
    confidence: Probability
    ts: str


def normalize_surface(surface: str) -> str:
    """How alias surfaces compare: casefolded, runs of whitespace collapsed."""
    return " ".join(surface.split()).casefold()


class Alias(Record):
    """One surface name for a task or requirement (A4). `surface` is stored normalized,
    and lookups normalize the same way, so 'MDB  Rev A' finds 'mdb rev a'."""

    surface: str
    target_kind: TargetKind
    target_id: str

    @field_validator("surface")
    @classmethod
    def _normalize(cls, surface: str) -> str:
        return normalize_surface(surface)


class UnresolvedName(Record):
    """A name `apply` could not resolve (A4), kept as the extractor wrote it."""

    surface: str
    target_kind: TargetKind
    signal_id: str


class InboxRow(Record):
    """`score` is the delta's confidence at fan-out time; rankers weight it."""

    user_id: str
    delta_id: str
    reason: str
    score: float


class Digest(Record):
    user_id: str
    date: dt.date
    body: str


class GraphSlice(Record):
    """What an extractor sees besides the signal: open tasks and requirements for its product.

    `product_id` is None when the signal could not be tagged; the slice then spans every product.
    """

    product_id: str | None
    tasks: list[Task]
    requirements: list[Requirement]


class Decision(Record):
    """A decider's answers for one signal."""

    changes_state: Probability
    change_type: dict[str, Probability]
    contradicts: Probability
    risk: Probability


class InboxItem(Record):
    """One inbox row with everything a ranker or renderer needs, so neither reads the database.

    `conflicts` are read from the graph as it stands when the item is loaded.
    """

    row: InboxRow
    delta: Delta
    signal: Signal
    product_id: str
    product_name: str
    target_title: str
    source_url: str
    conflicts: list[str]


class Scored(Record):
    item: InboxItem
    score: float


class Decider(Protocol):
    def decide(self, signal: Signal, ctx: GraphSlice) -> Decision: ...


class Extractor(Protocol):
    def extract(self, signal: Signal, ctx: GraphSlice) -> list[Delta]: ...


class Ranker(Protocol):
    def rank(self, user: User, items: list[InboxItem]) -> list[Scored]: ...


class Renderer(Protocol):
    def render(self, user: User, items: list[Scored]) -> str: ...


class GraphSeed(Record):
    """Contents of data/graph_seed.json. Keys are table names, in insert order."""

    users: list[User]
    products: list[Product]
    processes: list[Process]
    stages: list[Stage]
    requirements: list[Requirement]
    requirement_links: list[RequirementLink]
    tasks: list[Task]
    handoffs: list[Handoff]
    aliases: list[Alias] = Field(default_factory=list)


TABLE_MODELS: dict[str, type[Record]] = {
    "users": User,
    "products": Product,
    "processes": Process,
    "stages": Stage,
    "requirements": Requirement,
    "requirement_links": RequirementLink,
    "tasks": Task,
    "handoffs": Handoff,
    "aliases": Alias,
    "signals": Signal,
    "deltas": Delta,
    "unresolved": UnresolvedName,
    "inbox": InboxRow,
    "digests": Digest,
}
