"""`extract`: signal to deltas."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from digest.core.apply import TARGETS, TASK_STATUSES
from digest.core.assemble import SlackExport, SlackMessage, threads
from digest.models import Delta, Extractor, GraphSlice, Requirement, Signal, TargetKind, Task

GOLD_ONLY_FIELDS = ("contradicts", "affected")

MODEL_ENV = "DIGEST_EXTRACTOR_MODEL"
API_KEY_ENV = "OPENAI_API_KEY"

# Hard ceiling on one extraction call. Without it a dead connection hangs the whole
# ingest: a live run sat 70+ minutes on one request before this was added.
EXTRACT_TIMEOUT_S = 120.0


def graph_slice(conn: sqlite3.Connection, product_id: str | None) -> GraphSlice:
    """Open tasks and all requirements for one product, or for every product when None."""
    product = "product_id = ?" if product_id else "1"
    params = (product_id,) if product_id else ()
    tasks = conn.execute(
        f"SELECT * FROM tasks WHERE {product} AND status != 'done' ORDER BY id", params
    )
    requirements = conn.execute(f"SELECT * FROM requirements WHERE {product} ORDER BY id", params)
    return GraphSlice(
        product_id=product_id,
        tasks=[Task.model_validate(dict(row)) for row in tasks],
        requirements=[Requirement.model_validate(dict(row)) for row in requirements],
    )


class ReplayExtractor(Extractor):
    """Reads gold deltas from the dataset instead of calling a model. Confidence is always 1.0."""

    def __init__(self, gold_path: str | Path) -> None:
        gold = json.loads(Path(gold_path).read_text())
        self._deltas: dict[str, list[dict[str, Any]]] = {
            thread["thread"]: thread["deltas"] for thread in gold["threads"]
        }

    def extract(self, signal: Signal, ctx: GraphSlice) -> list[Delta]:
        """Raises `pydantic.ValidationError` if a gold delta for this thread is malformed."""
        deltas = []
        for raw in self._deltas.get(signal.id, []):
            fields = {k: v for k, v in raw.items() if k not in GOLD_ONLY_FIELDS}
            deltas.append(Delta.model_validate(fields | {"signal_id": signal.id, "confidence": 1.0}))
        return deltas


class ExtractorUnavailable(RuntimeError):
    """The extractor cannot be built, for example because its environment variables are unset."""


class ExtractionFailed(RuntimeError):
    """One model call failed (timeout, network, API error). The pipeline treats this like a
    malformed response: log the thread, skip it, keep going; the unsaved signal retries next
    ingest."""


class ModelClient(Protocol):
    def complete(self, instructions: str, prompt: str, schema: dict[str, Any]) -> str:
        """The model's raw response text, which should be JSON matching `schema`."""
        ...


class OpenAIClient(ModelClient):
    """Structured output through the OpenAI Responses API with a strict JSON schema."""

    def __init__(self, model: str, api_key: str, timeout: float | None = None) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(api_key=api_key, timeout=timeout)

    def complete(self, instructions: str, prompt: str, schema: dict[str, Any]) -> str:
        response = self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=prompt,
            text={"format": {"type": "json_schema", "name": "extraction", "schema": schema, "strict": True}},
        )
        return response.output_text


class ExtractedDelta(BaseModel):
    """One state change as the model reports it. Validation context: `ts`, the thread's message timestamps."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["status_change", "deadline_change", "owner_change", "value_change"]
    target_kind: TargetKind
    target_id: str
    old_value: str | None
    new_value: str
    ts: str = Field(description="ts of the message where the change was agreed or announced.")

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo) -> Self:
        if (self.target_kind, self.type) not in TARGETS:
            raise ValueError(f"{self.type} does not apply to a {self.target_kind}")
        if info.context is not None and self.ts not in info.context["ts"]:
            raise ValueError(f"ts {self.ts!r} is not a message in this thread")
        return self


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deltas: list[ExtractedDelta]


INSTRUCTIONS = f"""\
You read one Slack thread from a hardware engineering team and report the state changes it makes
to the project graph: decisions or updates that change a field of a task or requirement listed in
the prompt. The change types are:

- status_change on a task. new_value is one of: {", ".join(sorted(TASK_STATUSES))}.
- deadline_change on a task. new_value is the new deadline as YYYY-MM-DD.
- owner_change on a task. new_value is the new owner's user ID.
- value_change on a requirement. new_value is the new value, written in the style of the current one.

target_id is the task or requirement ID. old_value is the value before the change as the graph
records it, or null if unknown. ts is the ts of the message where the change was agreed or
announced; resolve relative dates such as "Friday" against that message's date.

Report a change only once it is agreed or announced, not while it is proposed or asked about.
Banter, questions, and status updates that change no field produce no deltas. Most threads change
nothing, so an empty list is a common answer.
"""


class LLMExtractor(Extractor):
    """One structured-output model call per signal, given the thread and its product's graph slice.

    A backend with no native confidence returns 1.0. Delta IDs derive from the thread content and
    the change, so re-extracting an unchanged thread yields the same IDs.
    """

    def __init__(self, client: ModelClient, export: SlackExport) -> None:
        self._client = client
        self._threads = threads(export)
        self._names = {u.id: u.name for u in export.users}

    @classmethod
    def from_env(cls, export: SlackExport) -> LLMExtractor:
        """Model name from DIGEST_EXTRACTOR_MODEL, API key from OPENAI_API_KEY."""
        env = {name: os.environ.get(name) for name in (MODEL_ENV, API_KEY_ENV)}
        missing = [name for name, value in env.items() if not value]
        if missing:
            raise ExtractorUnavailable(f"set {' and '.join(missing)}")
        return cls(
            OpenAIClient(model=env[MODEL_ENV], api_key=env[API_KEY_ENV], timeout=EXTRACT_TIMEOUT_S),
            export,
        )

    def extract(self, signal: Signal, ctx: GraphSlice) -> list[Delta]:
        """Raises `pydantic.ValidationError` if the response is not a valid extraction, and
        `ExtractionFailed` if the model call itself fails."""
        messages = self._threads.get(signal.id, [])
        try:
            text = self._client.complete(
                INSTRUCTIONS, self._prompt(signal, ctx, messages), Extraction.model_json_schema()
            )
        except Exception as e:
            raise ExtractionFailed(f"model call failed: {e}") from e
        extraction = Extraction.model_validate_json(text, context={"ts": {m.ts for m in messages}})
        return [
            Delta(
                id=_delta_id(signal, d),
                signal_id=signal.id,
                type=d.type,
                target_kind=d.target_kind,
                target_id=d.target_id,
                old_value=d.old_value,
                new_value=d.new_value,
                confidence=1.0,
                ts=d.ts,
            )
            for d in extraction.deltas
        ]

    def _prompt(self, signal: Signal, ctx: GraphSlice, messages: list[SlackMessage]) -> str:
        lines = [
            f"Channel: #{signal.channel}",
            f"Product: {ctx.product_id or 'untagged; the graph below spans every product'}",
            f"User IDs: {', '.join(sorted(self._names.values()))}",
            "",
            "Open tasks:",
            *(t.model_dump_json() for t in ctx.tasks),
            "",
            "Requirements:",
            *(r.model_dump_json() for r in ctx.requirements),
            "",
            "Thread:",
        ]
        for m in messages:
            when = dt.datetime.fromtimestamp(float(m.ts), dt.UTC).strftime("%a %Y-%m-%d %H:%M UTC")
            line = f"[ts {m.ts}, {when}] {self._names.get(m.user, m.user)}: {m.text}"
            for r in m.reactions:
                line += f" (:{r.name}: from {', '.join(self._names.get(u, u) for u in r.users)})"
            lines.append(line)
        return "\n".join(lines)


def _delta_id(signal: Signal, d: ExtractedDelta) -> str:
    key = "|".join([signal.id, signal.content_hash, d.target_kind, d.target_id, d.type, d.new_value])
    return f"llm-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
