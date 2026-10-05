"""A1 cascade: backends parse recorded responses; routing gates the extractor; eval compares backends.

No test makes a network call: every backend takes a transport, and these tests pass one that
returns responses recorded from the TypeSafe, Together and OpenAI response shapes.
"""

import json
import logging
import math
from pathlib import Path

import pytest
from typer.testing import CliRunner

from digest.attach.deciders import (
    Cascade,
    DeciderUnavailable,
    JevDecider,
    LLMDecider,
    PassThroughDecider,
    TevDecider,
    build_decider,
    route,
)
from digest.cli import app
from digest.core.assemble import SlackExport, SlackMessage, SlackUser
from digest.core.rank import FixedWeightRanker
from digest.eval import (
    CONFIGURATIONS,
    METRICS,
    Configuration,
    Money,
    NotRun,
    Ratio,
    Seconds,
    evaluate,
)
from digest.models import Decision, GraphSlice, Requirement, Signal, Task

DATA = Path(__file__).parent.parent / "data"
CORE = CONFIGURATIONS[0]

EXPORT = SlackExport(
    users=[SlackUser(id="u1", name="Priya")],
    messages={"gripper": [SlackMessage(user="u1", ts="1.0", text="ok, go with 22 Nm")]},
)
SIGNAL = Signal(id="gripper:1.0", source="slack", channel="gripper", content_hash="h", last_ts="1.0")
CTX = GraphSlice(
    product_id="grip",
    tasks=[Task(id="T1", title="Torque test", product_id="grip", status="open")],
    requirements=[Requirement(id="R1", product_id="grip", title="Motor torque", value="15 Nm")],
)

# Recorded from the TypeSafe systemone response shape: noul answers carry a probability,
# the choice answer carries a distribution over the criteria.
JEV_RESPONSE = {
    "model": "jev-latest",
    "answers": {
        "changes_state": {"type": "noul", "noul": 0.93},
        "change_type": {
            "type": "choice",
            "choice": "value_change",
            "probabilities": {
                "status_change": 0.05, "deadline_change": 0.02, "owner_change": 0.01,
                "value_change": 0.84, "none": 0.08,
            },
            "confidence": 0.84,
        },
        "contradicts": {"type": "noul", "noul": 0.12},
        "risk": {"type": "noul", "noul": 0.41},
    },
    "usage": {"input_tokens": 1000, "output_tokens": 20},
}


def _tev_response(letter: str, top_logprobs: dict[str, float] | None) -> dict:
    """Recorded from Together's chat.completions shape; Tev1 answers with one letter."""
    logprobs = None
    if top_logprobs is not None:
        logprobs = {"content": [{
            "token": letter,
            "logprob": top_logprobs[letter],
            "top_logprobs": [{"token": t, "logprob": lp} for t, lp in top_logprobs.items()],
        }]}
    return {
        "choices": [{"message": {"content": letter}, "logprobs": logprobs}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 1},
    }


LLM_ANSWERS = {
    "changes_state": 0.9,
    "change_type": {"status_change": 0.05, "deadline_change": 0.05, "owner_change": 0.05,
                    "value_change": 0.8, "none": 0.05},
    "contradicts": 0.1,
    "risk": 0.3,
}
# Recorded from the OpenAI Responses API shape.
LLM_RESPONSE = {
    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(LLM_ANSWERS)}]}],
    "usage": {"input_tokens": 500, "output_tokens": 40},
}


def test_routing_thresholds_at_boundaries():
    def decision(p):
        return Decision(changes_state=p, change_type={}, contradicts=0.0, risk=0.0)

    assert route(decision(1.0)) == "extract"
    assert route(decision(0.8)) == "extract"
    assert route(decision(0.79)) == "escalate"
    assert route(decision(0.5)) == "escalate"
    assert route(decision(0.21)) == "escalate"
    assert route(decision(0.2)) == "drop"
    assert route(decision(0.0)) == "drop"


def test_passthrough_is_the_default_and_needs_no_key():
    decider = build_decider("passthrough", EXPORT)
    assert isinstance(decider, PassThroughDecider)
    assert decider.decide(SIGNAL, CTX) == Decision(
        changes_state=1.0, change_type={}, contradicts=0.0, risk=0.0
    )
    assert decider.spent_usd == 0.0


def test_unknown_backend_is_a_clear_error():
    with pytest.raises(DeciderUnavailable, match="unknown decider 'nope'"):
        build_decider("nope", EXPORT)


@pytest.mark.parametrize("name, message", [
    ("jev", "set JEV_API_KEY"),
    ("tev", "set TOGETHER_API_KEY"),
    ("llm", "set DIGEST_DECIDER_MODEL and OPENAI_API_KEY"),
])
def test_missing_key_is_a_clear_error(name, message):
    with pytest.raises(DeciderUnavailable, match=message):
        build_decider(name, EXPORT)


def test_jev_asks_all_four_questions_in_one_call():
    bodies = []

    def transport(body):
        bodies.append(body)
        return JEV_RESPONSE

    decider = JevDecider(EXPORT, transport)
    decision = decider.decide(SIGNAL, CTX)

    [body] = bodies
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"changes_state", "change_type", "contradicts", "risk"}
    assert body["questions"]["changes_state"]["type"] == "noul"
    assert body["questions"]["change_type"]["type"] == "choice"
    assert "R1 Motor torque = 15 Nm" in body["state"]
    assert "Priya: ok, go with 22 Nm" in body["state"]

    assert decision.changes_state == 0.93
    assert decision.change_type == JEV_RESPONSE["answers"]["change_type"]["probabilities"]
    assert decision.contradicts == 0.12
    assert decision.risk == 0.41
    assert decider.spent_usd == pytest.approx((1000 * 0.30 + 20 * 1.20) / 1e6)


def test_tev_reads_logprobs_when_exposed_and_is_hard_otherwise():
    responses = [
        _tev_response("A", {"A": -0.105360516, "B": -2.302585093}),  # changes_state: yes at ~0.9
        _tev_response("D", None),                                    # change_type: no logprobs
        _tev_response("B", {"A": -3.0, "B": -0.05}),                 # contradicts: no
        _tev_response("A", None),                                    # risk: no logprobs
    ]
    decider = TevDecider(EXPORT, lambda body: responses.pop(0))
    decision = decider.decide(SIGNAL, CTX)

    assert decision.changes_state == pytest.approx(0.9, abs=1e-6)
    assert decision.change_type == {
        "status_change": 0.0, "deadline_change": 0.0, "owner_change": 0.0,
        "value_change": 1.0, "none": 0.0,
    }
    yes, no = math.exp(-3.0), math.exp(-0.05)
    assert decision.contradicts == pytest.approx(yes / (yes + no))
    assert decision.risk == 1.0
    assert decider.spent_usd == pytest.approx(4 * (800 * 0.10 + 1 * 0.10) / 1e6)


def test_tev_prose_answer_raises():
    prose = {"choices": [{"message": {"content": "Sure, happy to help!"}, "logprobs": None}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 8}}
    decider = TevDecider(EXPORT, lambda body: prose)
    with pytest.raises(ValueError, match="not a listed letter"):
        decider.decide(SIGNAL, CTX)


def test_llm_decider_parses_structured_output():
    bodies = []

    def transport(body):
        bodies.append(body)
        return LLM_RESPONSE

    decider = LLMDecider("gpt-test", EXPORT, transport)
    decision = decider.decide(SIGNAL, CTX)

    [body] = bodies
    assert body["model"] == "gpt-test"
    assert body["text"]["format"]["type"] == "json_schema"
    assert decision.changes_state == 0.9
    assert decision.change_type == LLM_ANSWERS["change_type"]
    assert decision.contradicts == 0.1
    assert decision.risk == 0.3
    assert decider.spent_usd == pytest.approx((500 * 2.50 + 40 * 10.00) / 1e6)


class StubDecider:
    """changes_state per signal ID; a missing ID makes decide raise."""

    def __init__(self, by_signal):
        self.by_signal = by_signal
        self.spent_usd = 0.0

    def decide(self, signal, ctx):
        return Decision(
            changes_state=self.by_signal[signal.id], change_type={"none": 1.0},
            contradicts=0.0, risk=0.0,
        )


class StubExtractor:
    def __init__(self):
        self.calls = []

    def extract(self, signal, ctx):
        self.calls.append(signal.id)
        return []


def _signal(id_):
    return Signal(id=id_, source="slack", channel="c", content_hash="h", last_ts="1.0")


def test_cascade_routes_drop_escalate_extract_and_records(caplog):
    inner = StubExtractor()
    cascade = Cascade(StubDecider({"c:hi": 0.9, "c:mid": 0.5, "c:lo": 0.1}), inner)

    with caplog.at_level(logging.INFO, logger="digest.attach.deciders"):
        for id_ in ("c:hi", "c:mid", "c:lo", "c:boom"):
            cascade.extract(_signal(id_), CTX)

    assert inner.calls == ["c:hi", "c:mid", "c:boom"]  # dropped c:lo; escalated on decider error
    assert cascade.routes == {"c:hi": "extract", "c:mid": "escalate", "c:lo": "drop", "c:boom": "escalate"}
    assert set(cascade.decisions) == {"c:hi", "c:mid", "c:lo"}
    assert len(cascade.latencies) == 4
    assert "escalated thread c:mid" in caplog.text
    assert "decider failed on thread c:boom" in caplog.text


def test_eval_backends_not_run_without_keys():
    backends = [c for c in CONFIGURATIONS if c.name in ("a1-jev", "a1-tev", "a1-llm", "a1-jev-llm")]
    rows = {(r.metric, r.configuration): r.value for r in evaluate(DATA, backends, METRICS[:1])}
    assert rows[("Silo recall", "a1-jev")] == NotRun("set JEV_API_KEY")
    assert rows[("Silo recall", "a1-tev")] == NotRun("set TOGETHER_API_KEY")
    assert rows[("Silo recall", "a1-llm")] == NotRun("set DIGEST_DECIDER_MODEL and OPENAI_API_KEY")
    assert rows[("Silo recall", "a1-jev-llm")] == NotRun("set JEV_API_KEY")


def test_cascade_escalation_second_opinion_is_final():
    inner = StubExtractor()
    first = StubDecider({"c:band1": 0.5, "c:band2": 0.4, "c:band3": 0.6, "c:sure": 0.9})
    second = StubDecider({"c:band1": 0.95, "c:band2": 0.05, "c:band3": 0.5})
    first.spent_usd, second.spent_usd = 0.002, 0.010
    cascade = Cascade(first, inner, escalation=second)

    for id_ in ("c:band1", "c:band2", "c:band3", "c:sure"):
        cascade.extract(_signal(id_), CTX)

    # band2 is dropped by the second opinion; a second middle answer (band3) extracts.
    assert inner.calls == ["c:band1", "c:band3", "c:sure"]
    assert cascade.escalations == {"c:band1": "extract", "c:band2": "drop", "c:band3": "extract"}
    # Routes still mark the band, so the escalation rate reads as the share paying twice.
    assert cascade.routes == {
        "c:band1": "escalate", "c:band2": "escalate", "c:band3": "escalate", "c:sure": "extract",
    }
    # The settling answer is recorded: the second opinion for the band, the first elsewhere.
    assert cascade.decisions["c:band1"].changes_state == 0.95
    assert cascade.decisions["c:band2"].changes_state == 0.05
    assert cascade.decisions["c:sure"].changes_state == 0.9
    assert cascade.cost_usd == pytest.approx(0.012)
    assert len(cascade.latencies) == 4


def test_cascade_escalation_failure_extracts_and_keeps_first_decision(caplog):
    class Boom:
        spent_usd = 0.0

        def decide(self, signal, ctx):
            raise RuntimeError("down")

    inner = StubExtractor()
    cascade = Cascade(StubDecider({"c:mid": 0.5}), inner, escalation=Boom())
    with caplog.at_level(logging.WARNING, logger="digest.attach.deciders"):
        cascade.extract(_signal("c:mid"), CTX)

    assert inner.calls == ["c:mid"]
    assert cascade.escalations == {"c:mid": "extract"}
    assert cascade.decisions["c:mid"].changes_state == 0.5
    assert "escalation decider failed on thread c:mid" in caplog.text


def test_ingest_with_escalation_missing_key_exits_clearly(tmp_path):
    args = ["ingest", str(DATA / "slack.json"), "--replay", "--db", str(tmp_path / "d.db")]
    result = CliRunner().invoke(app, [*args, "--escalate-to", "jev"])
    assert result.exit_code == 1
    assert "set JEV_API_KEY" in result.output

    result = CliRunner().invoke(app, [*args, "--escalate-to", "nope"])
    assert result.exit_code == 1
    assert "unknown decider 'nope'" in result.output


def test_eval_scores_the_passthrough_baseline():
    config = next(c for c in CONFIGURATIONS if c.name == "a1-passthrough")
    rows = {r.metric: r.value for r in evaluate(DATA, [config], METRICS)}
    gold = json.loads((DATA / "gold.json").read_text())["threads"]
    total = len(gold)
    with_deltas = sum(1 for t in gold if t["deltas"])
    contradicting = sum(1 for t in gold if any(d.get("contradicts") for d in t["deltas"]))

    assert rows["Escalation rate"] == Ratio(0, total)
    assert rows["Decider accuracy: changes_state"] == Ratio(with_deltas, total)
    assert rows["Decider accuracy: change_type"] == Ratio(0, 0)  # passthrough answers no questions
    assert rows["Decider accuracy: contradicts"] == Ratio(total - contradicting, total)
    assert rows["Decider accuracy: risk"] == NotRun("gold has no risk labels")
    assert isinstance(rows["Decider latency (median)"], Seconds)
    assert rows["Cost per digest"] == Money(0.0)
    assert str(Money(0.0)) == "$0.0000"
    # The gate passed everything, so the digests match the core configuration exactly.
    assert rows["Silo recall"].hits == rows["Silo recall"].total > 0


def test_eval_counts_dropped_threads_against_recall_not_extractor_accuracy():
    drop_all = Configuration(
        name="drop-all", description="Cascade that drops every thread.",
        extractor=CORE.extractor, ranker=FixedWeightRanker,
        decider=lambda _: LowDecider(),
    )
    rows = {r.metric: r.value for r in evaluate(DATA, [drop_all], METRICS)}
    gold = json.loads((DATA / "gold.json").read_text())["threads"]
    empty = sum(1 for t in gold if not t["deltas"])

    assert rows["Silo recall"].hits == 0
    # A dropped no-change thread records an empty extraction, so correct drops still score.
    assert rows["Extractor accuracy"] == Ratio(empty, len(gold))
    assert rows["Escalation rate"] == Ratio(0, len(gold))
    assert rows["Cost per digest"] == NotRun("no digests produced")


class LowDecider:
    spent_usd = 0.0

    def decide(self, signal, ctx):
        return Decision(changes_state=0.1, change_type={"none": 1.0}, contradicts=0.0, risk=0.0)


def test_ingest_with_decider_missing_key_exits_clearly(tmp_path):
    args = ["ingest", str(DATA / "slack.json"), "--replay", "--db", str(tmp_path / "d.db")]
    result = CliRunner().invoke(app, [*args, "--decider", "jev"])
    assert result.exit_code == 1
    assert "set JEV_API_KEY" in result.output

    result = CliRunner().invoke(app, [*args, "--decider", "nope"])
    assert result.exit_code == 1
    assert "unknown decider 'nope'" in result.output

    result = CliRunner().invoke(app, args)  # passthrough default still ingests
    assert result.exit_code == 0, result.output
