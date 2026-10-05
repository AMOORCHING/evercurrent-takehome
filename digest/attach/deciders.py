"""A1 classifier cascade: three `Decider` backends and the routing that gates the extractor.

Backends, selected by `--decider` and built by `build_decider`:

- `passthrough` (default): `PassThroughDecider`, every thread goes to the extractor.
- `jev`: TypeSafe's judgment API (`POST /v1/systemone`), one call for all four questions,
  native probabilities. Key from JEV_API_KEY.
- `tev`: Together's hosted Tev1, a letter-only decision model, one call per question.
  Probabilities come from option log-probabilities when the endpoint exposes them, else
  hard 0 or 1. Key from TOGETHER_API_KEY.
- `llm`: structured output from the model named by DIGEST_DECIDER_MODEL through the OpenAI
  Responses API. Key from OPENAI_API_KEY.

Routing: `changes_state` at or above 0.8 goes to the extractor; at or below 0.2 is dropped;
the middle goes to the extractor and is logged as escalated. `Cascade` applies the routing
around any `Extractor` and records decisions, routes and latencies for `digest eval`.

Prices are USD per million tokens. Neither TypeSafe nor Together published Tev1 or jev
pricing when this was written (Oct 2026); those constants are placeholder estimates, and the
llm price assumes a mid-tier model. Revisit before quoting dollars in the writeup.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
import urllib.request
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from digest.core.assemble import SlackExport, SlackMessage, threads
from digest.core.extract import API_KEY_ENV
from digest.models import Decider, Decision, Delta, Extractor, GraphSlice, Signal, SignalDecision

log = logging.getLogger(__name__)

JEV_KEY_ENV = "JEV_API_KEY"
TOGETHER_KEY_ENV = "TOGETHER_API_KEY"
DECIDER_MODEL_ENV = "DIGEST_DECIDER_MODEL"

EXTRACT_AT = 0.8
DROP_AT = 0.2

Route = Literal["extract", "escalate", "drop"]

CHANGE_TYPES = ("status_change", "deadline_change", "owner_change", "value_change")

CHANGES_STATE_Q = (
    "Does this thread agree or announce a change to a field of one of the listed tasks or "
    "requirements? Proposals, questions, banter, and status updates that change nothing do "
    "not count."
)
CHANGE_TYPE_Q = "Which kind of change does the thread make, if any?"
CHANGE_TYPE_CRITERIA = {
    "status_change": "a task's status changes",
    "deadline_change": "a task's deadline changes",
    "owner_change": "a task's owner changes",
    "value_change": "a requirement's value changes",
    "none": "no state change",
}
CONTRADICTS_Q = (
    "Does the thread assert something that contradicts the recorded state shown: a status, "
    "owner, deadline or requirement value that disagrees with the graph?"
)
RISK_Q = "Would missing this thread risk a slipped gate, rework, or a cross-team surprise?"

# A transport posts one JSON body and returns the parsed JSON response. Tests pass a
# function that returns recorded responses; `_http_post` is the real one.
Transport = Callable[[dict[str, Any]], dict[str, Any]]


def route(decision: Decision) -> Route:
    if decision.changes_state >= EXTRACT_AT:
        return "extract"
    if decision.changes_state <= DROP_AT:
        return "drop"
    return "escalate"


class DeciderUnavailable(RuntimeError):
    """The decider cannot be built, for example because its API key is unset."""


def _require(*names: str) -> dict[str, str]:
    env = {name: os.environ.get(name) for name in names}
    missing = [name for name, value in env.items() if not value]
    if missing:
        raise DeciderUnavailable(f"set {' and '.join(missing)}")
    return env


def _http_post(url: str, api_key: str) -> Transport:
    def post(body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    return post


class PassThroughDecider:
    """Every thread goes to the extractor; no questions are asked and nothing is spent."""

    spent_usd = 0.0

    def decide(self, signal: Signal, ctx: GraphSlice) -> Decision:
        return Decision(changes_state=1.0, change_type={}, contradicts=0.0, risk=0.0)


class _ModelDecider:
    """Shared plumbing: the thread-plus-graph state text, and token cost accounting."""

    PRICE_IN: float
    PRICE_OUT: float

    def __init__(self, export: SlackExport, transport: Transport) -> None:
        self._transport = transport
        self._threads = threads(export)
        self._names = {u.id: u.name for u in export.users}
        self.spent_usd = 0.0

    def _state(self, signal: Signal, ctx: GraphSlice) -> str:
        lines = [
            f"Channel: #{signal.channel}",
            f"Product: {ctx.product_id or 'untagged'}",
            "",
            "Open tasks:",
            *(
                f"{t.id} [{t.status}] {t.title} (owner {t.owner_id or '-'}, deadline {t.deadline or '-'})"
                for t in ctx.tasks
            ),
            "",
            "Requirements:",
            *(f"{r.id} {r.title} = {r.value}" for r in ctx.requirements),
            "",
            "Thread:",
        ]
        for m in self._threads.get(signal.id, []):
            line = f"{self._names.get(m.user, m.user)}: {m.text}"
            for r in m.reactions:
                line += f" (:{r.name}: from {', '.join(self._names.get(u, u) for u in r.users)})"
            lines.append(line)
        return "\n".join(lines)

    def _charge(self, usage: dict[str, Any] | None) -> None:
        usage = usage or {}
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        self.spent_usd += (input_tokens * self.PRICE_IN + output_tokens * self.PRICE_OUT) / 1e6


class _JevAnswer(BaseModel):
    type: str
    noul: float | None = None
    choice: str | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None


class _JevResponse(BaseModel):
    answers: dict[str, _JevAnswer]
    usage: dict[str, int] | None = None


class JevDecider(_ModelDecider):
    """All four questions in one TypeSafe systemone call; noul and choice probabilities are native."""

    URL = "https://api.typesafe.ai/v1/systemone"
    MODEL = "jev-latest"
    PRICE_IN, PRICE_OUT = 0.30, 1.20  # placeholder estimate, see module docstring

    @classmethod
    def from_env(cls, export: SlackExport) -> JevDecider:
        key = _require(JEV_KEY_ENV)[JEV_KEY_ENV]
        return cls(export, _http_post(cls.URL, key))

    def decide(self, signal: Signal, ctx: GraphSlice) -> Decision:
        body = {
            "state": self._state(signal, ctx),
            "model": self.MODEL,
            "questions": {
                "changes_state": {"type": "noul", "instructions": CHANGES_STATE_Q},
                "change_type": {
                    "type": "choice",
                    "instructions": CHANGE_TYPE_Q,
                    "criteria": CHANGE_TYPE_CRITERIA,
                },
                "contradicts": {"type": "noul", "instructions": CONTRADICTS_Q},
                "risk": {"type": "noul", "instructions": RISK_Q},
            },
        }
        response = _JevResponse.model_validate(self._transport(body))
        self._charge(response.usage)
        answers = response.answers
        choice = answers["change_type"]
        change_type = choice.probabilities or {choice.choice or "none": choice.confidence or 1.0}
        return Decision(
            changes_state=answers["changes_state"].noul or 0.0,
            change_type=change_type,
            contradicts=answers["contradicts"].noul or 0.0,
            risk=answers["risk"].noul or 0.0,
        )


class TevDecider(_ModelDecider):
    """One letter-only chat call per question. Tev1 returns a letter; probabilities come from
    option log-probabilities when the endpoint exposes them, and are hard 0 or 1 otherwise."""

    URL = "https://api.together.xyz/v1/chat/completions"
    MODEL = "together/Tev1-4B-experimental"
    PRICE_IN, PRICE_OUT = 0.10, 0.10  # placeholder estimate, see module docstring
    SYSTEM = (
        "Evaluate the supplied decision task. Select exactly one listed option. "
        "Return only its letter, with no explanation."
    )
    YES_NO = {"A": "yes", "B": "no"}

    @classmethod
    def from_env(cls, export: SlackExport) -> TevDecider:
        key = _require(TOGETHER_KEY_ENV)[TOGETHER_KEY_ENV]
        return cls(export, _http_post(cls.URL, key))

    def decide(self, signal: Signal, ctx: GraphSlice) -> Decision:
        state = self._state(signal, ctx)
        changes_state = self._yes(state, CHANGES_STATE_Q)
        letters = dict(zip("ABCDE", CHANGE_TYPE_CRITERIA))
        by_letter = self._ask(state, CHANGE_TYPE_Q, {k: CHANGE_TYPE_CRITERIA[v] for k, v in letters.items()})
        return Decision(
            changes_state=changes_state,
            change_type={letters[k]: p for k, p in by_letter.items()},
            contradicts=self._yes(state, CONTRADICTS_Q),
            risk=self._yes(state, RISK_Q),
        )

    def _yes(self, state: str, question: str) -> float:
        return self._ask(state, question, self.YES_NO)["A"]

    def _ask(self, state: str, question: str, options: dict[str, str]) -> dict[str, float]:
        """Option probabilities for one question, keyed by letter."""
        body = {
            "model": self.MODEL,
            "messages": [
                {"role": "system", "content": self.SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps({"state": state, "question": question, "options": options}),
                },
            ],
            "temperature": 0,
            "max_tokens": 8,
            "logprobs": True,
            "top_logprobs": 5,
        }
        data = self._transport(body)
        self._charge(data.get("usage"))
        chosen = data["choices"][0]["message"]["content"].strip()[:1].upper()
        if chosen not in options:
            raise ValueError(f"Tev1 returned {data['choices'][0]['message']['content']!r}, not a listed letter")
        found = {
            letter: math.exp(top["logprob"])
            for item in ((data["choices"][0].get("logprobs") or {}).get("content") or [])[:1]
            for top in item.get("top_logprobs") or []
            if (letter := top["token"].strip().upper()) in options
        }
        total = sum(found.values())
        if total <= 0:
            return {letter: 1.0 if letter == chosen else 0.0 for letter in options}
        return {letter: found.get(letter, 0.0) / total for letter in options}


class _LLMTypeProbs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_change: float
    deadline_change: float
    owner_change: float
    value_change: float
    none: float


class _LLMAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes_state: float
    change_type: _LLMTypeProbs
    contradicts: float
    risk: float


LLM_INSTRUCTIONS = f"""\
You read one Slack thread from a hardware engineering team, with the open tasks and
requirements of its product. Answer four questions, each as a probability from 0 to 1:

- changes_state: {CHANGES_STATE_Q}
- change_type: the probability that the thread's change is each kind, with "none" for no
  state change. The five probabilities should sum to about 1.
- contradicts: {CONTRADICTS_Q}
- risk: {RISK_Q}
"""


class LLMDecider(_ModelDecider):
    """Structured output through the OpenAI Responses API; the model self-reports probabilities."""

    URL = "https://api.openai.com/v1/responses"
    PRICE_IN, PRICE_OUT = 2.50, 10.00  # placeholder for a mid-tier model, see module docstring

    def __init__(self, model: str, export: SlackExport, transport: Transport) -> None:
        super().__init__(export, transport)
        self.model = model

    @classmethod
    def from_env(cls, export: SlackExport) -> LLMDecider:
        env = _require(DECIDER_MODEL_ENV, API_KEY_ENV)
        return cls(env[DECIDER_MODEL_ENV], export, _http_post(cls.URL, env[API_KEY_ENV]))

    def decide(self, signal: Signal, ctx: GraphSlice) -> Decision:
        body = {
            "model": self.model,
            "instructions": LLM_INSTRUCTIONS,
            "input": self._state(signal, ctx),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "decision",
                    "schema": _LLMAnswers.model_json_schema(),
                    "strict": True,
                }
            },
        }
        data = self._transport(body)
        self._charge(data.get("usage"))
        answers = _LLMAnswers.model_validate_json(_output_text(data))
        return Decision(
            changes_state=answers.changes_state,
            change_type=answers.change_type.model_dump(),
            contradicts=answers.contradicts,
            risk=answers.risk,
        )


def _output_text(data: dict[str, Any]) -> str:
    return "".join(
        content["text"]
        for item in data.get("output") or []
        if item.get("type") == "message"
        for content in item.get("content") or []
        if content.get("type") == "output_text"
    )


class Cascade(Extractor):
    """The routing around an extractor: decide each thread, drop, extract, or escalate.

    Without an `escalation` decider, the middle band goes to the extractor and is logged,
    as the spec first wrote it. With one (chosen after the first eval round, see
    EXPERIMENTS.md), the middle band is re-decided by the stronger backend and its answer
    is final: drop, or extract - a second middle answer extracts. Confident first-stage
    answers never pay for the second call.

    A decider error on one thread is logged and the thread extracts, so the run continues.
    Decisions, routes, escalation outcomes and per-thread latencies are kept for
    `digest eval`; `decisions` holds the answer that settled each thread, so for an
    escalated thread it is the escalation decider's.
    """

    def __init__(self, decider: Decider, extractor: Extractor, escalation: Decider | None = None) -> None:
        self.decider = decider
        self.inner = extractor
        self.escalation = escalation
        self.decisions: dict[str, Decision] = {}
        self.routes: dict[str, str] = {}
        self.escalations: dict[str, str] = {}
        self.latencies: list[float] = []

    @property
    def cost_usd(self) -> float:
        return getattr(self.decider, "spent_usd", 0.0) + getattr(self.escalation, "spent_usd", 0.0)

    def extract(self, signal: Signal, ctx: GraphSlice) -> list[Delta]:
        start = time.perf_counter()
        try:
            decision = self.decider.decide(signal, ctx)
        except Exception as e:
            self.latencies.append(time.perf_counter() - start)
            self.routes[signal.id] = "escalate"
            log.warning("decider failed on thread %s, escalating: %s", signal.id, e)
            return self.inner.extract(signal, ctx)
        where = route(decision)
        final = where
        if where == "escalate":
            decision, final = self._escalate(signal, ctx, decision)
        self.latencies.append(time.perf_counter() - start)
        self.decisions[signal.id] = decision
        self.routes[signal.id] = where
        if final == "drop":
            return []
        return self.inner.extract(signal, ctx)

    def _escalate(self, signal: Signal, ctx: GraphSlice, first: Decision) -> tuple[Decision, Route]:
        """The settled (decision, action) for one middle-band thread."""
        if self.escalation is None:
            log.info("escalated thread %s: changes_state=%.2f", signal.id, first.changes_state)
            return first, "extract"
        try:
            second = self.escalation.decide(signal, ctx)
        except Exception as e:
            log.warning("escalation decider failed on thread %s, extracting: %s", signal.id, e)
            self.escalations[signal.id] = "extract"
            return first, "extract"
        final: Route = "drop" if route(second) == "drop" else "extract"
        self.escalations[signal.id] = final
        log.info(
            "escalated thread %s: changes_state %.2f, second opinion %.2f, %s",
            signal.id, first.changes_state, second.changes_state, final,
        )
        return second, final


def save_decisions(conn: sqlite3.Connection, cascade: Cascade, backend: str) -> None:
    """Persist what the cascade decided this run, one row per decided signal, for
    `digest explain` to read back.

    A changed signal is re-decided, so its row is overwritten; a skipped thread's signal
    is not in `signals` and is retried next ingest, so its decision is not stored. When
    an escalation decider failed on a thread, the stored probabilities are the first
    stage's (the answer that stood), though `settled` still records the forced extract.
    """
    rows = [
        SignalDecision(
            signal_id=signal_id,
            backend=backend,
            changes_state=decision.changes_state,
            change_type=decision.change_type,
            contradicts=decision.contradicts,
            risk=decision.risk,
            route=cascade.routes[signal_id],
            settled=cascade.escalations.get(signal_id),
        )
        for signal_id, decision in sorted(cascade.decisions.items())
        if conn.execute("SELECT 1 FROM signals WHERE id = ?", (signal_id,)).fetchone()
    ]
    with conn:
        for record in rows:
            row = record.model_dump() | {"change_type": json.dumps(record.change_type)}
            columns = ", ".join(row)
            placeholders = ", ".join(f":{key}" for key in row)
            updates = ", ".join(f"{key} = excluded.{key}" for key in row if key != "signal_id")
            conn.execute(
                f"INSERT INTO decisions ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT (signal_id) DO UPDATE SET {updates}",
                row,
            )


DECIDERS: dict[str, Callable[[SlackExport], Decider]] = {
    "passthrough": lambda export: PassThroughDecider(),
    "jev": JevDecider.from_env,
    "tev": TevDecider.from_env,
    "llm": LLMDecider.from_env,
}


def build_decider(name: str, export: SlackExport) -> Decider:
    """Raises `DeciderUnavailable` for an unknown name or a missing key."""
    if name not in DECIDERS:
        raise DeciderUnavailable(f"unknown decider {name!r}; one of: {', '.join(DECIDERS)}")
    return DECIDERS[name](export)
