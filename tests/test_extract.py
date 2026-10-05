import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from digest.cli import app
from digest.core import extract
from digest.core.assemble import SlackExport, assemble
from digest.core.extract import API_KEY_ENV, MODEL_ENV, Extraction, LLMExtractor, graph_slice
from digest.db import connect, create_schema, load_graph_seed
from digest.pipeline import ingest
from tests.test_ingest import _snapshot

DATA = Path(__file__).parent.parent / "data"
EMPTY = '{"deltas": []}'
GOOD = "gripper:1772535600.000068"
BAD = "drive-unit:1772445900.000012"
GOOD_DELTA = {
    "type": "deadline_change", "target_kind": "task", "target_id": "T01",
    "old_value": "2026-03-03", "new_value": "2026-03-04", "ts": "1772535900.000070",
}


class FakeClient:
    """Answers by signal ID, read back from the prompt's channel line and first message ts."""

    def __init__(self, responses: dict[str, str | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def complete(self, instructions: str, prompt: str, schema: dict[str, Any]) -> str:
        self.calls.append((instructions, prompt, schema))
        channel = re.search(r"^Channel: #(\S+)$", prompt, re.M).group(1)
        root_ts = re.search(r"^\[ts ([\d.]+),", prompt, re.M).group(1)
        response = self.responses.get(f"{channel}:{root_ts}", EMPTY)
        if isinstance(response, Exception):
            raise response
        return response


def gold_responses() -> dict[str, str]:
    """The response a perfect model would give for every thread in gold."""
    keys = ("type", "target_kind", "target_id", "old_value", "new_value", "ts")
    gold = json.loads((DATA / "gold.json").read_text())
    return {
        t["thread"]: json.dumps({"deltas": [{k: d[k] for k in keys} for d in t["deltas"]]})
        for t in gold["threads"]
    }


def fake_openai(monkeypatch, client: FakeClient) -> list[tuple[str, str]]:
    """Point the env-built extractor at `client`. Returns the (model, api key) it was built with."""
    built = []

    def factory(model: str, api_key: str, timeout: float | None = None) -> FakeClient:
        built.append((model, api_key))
        return client

    monkeypatch.setenv(MODEL_ENV, "fake-model")
    monkeypatch.setenv(API_KEY_ENV, "fake-key")
    monkeypatch.setattr(extract, "OpenAIClient", factory)
    return built


def _export() -> SlackExport:
    return SlackExport.model_validate_json((DATA / "slack.json").read_text())


def _seeded():
    conn = connect(":memory:")
    create_schema(conn)
    load_graph_seed(conn, DATA / "graph_seed.json")
    return conn


def test_one_call_per_signal_given_thread_and_product_slice():
    export = _export()
    conn = _seeded()
    signal = next(s for s in assemble(conn, export) if s.id == GOOD)
    ctx = graph_slice(conn, signal.product_id)
    client = FakeClient({GOOD: json.dumps({"deltas": [GOOD_DELTA]})})

    deltas = LLMExtractor(client, export).extract(signal, ctx)
    assert len(client.calls) == 1
    [delta] = deltas
    assert delta.model_dump(exclude={"id"}) == GOOD_DELTA | {"signal_id": GOOD, "confidence": 1.0}
    assert delta.id.startswith("llm-")
    assert LLMExtractor(client, export).extract(signal, ctx) == deltas

    _, prompt, schema = client.calls[0]
    assert schema == Extraction.model_json_schema()
    assert "maya: Finger CAD needs one more day." in prompt
    assert "[ts 1772535900.000070, Tue 2026-03-03 11:05 UTC] maya: tomorrow EOD, yes" in prompt
    assert all(t.model_dump_json() in prompt for t in ctx.tasks + ctx.requirements)
    assert ctx.product_id == "gripper" and '"T17"' not in prompt
    conn.close()


@pytest.mark.parametrize("response", [
    "not json",
    '{"deltas": [{"type": "status_change"}]}',
    json.dumps({"deltas": [GOOD_DELTA | {"type": "value_change"}]}),
    json.dumps({"deltas": [GOOD_DELTA | {"ts": "1.000000"}]}),
    json.dumps({"deltas": [GOOD_DELTA | {"confidence": 0.9}]}),
])
def test_malformed_response_is_logged_and_skips_only_its_thread(response, caplog):
    conn = _seeded()
    client = FakeClient({BAD: response, GOOD: json.dumps({"deltas": [GOOD_DELTA]})})
    with caplog.at_level(logging.ERROR):
        result = ingest(conn, _export(), LLMExtractor(client, _export()))

    assert result.skipped == 1 and result.signals == len(client.calls) - 1
    assert BAD in caplog.text
    assert conn.execute("SELECT 1 FROM signals WHERE id = ?", (BAD,)).fetchone() is None
    applied = [tuple(r) for r in conn.execute("SELECT signal_id, target_id, new_value FROM deltas")]
    assert applied == [(GOOD, "T01", "2026-03-04")]
    conn.close()


def test_ingest_without_replay_uses_llm_extractor_from_env(tmp_path, monkeypatch):
    client = FakeClient(gold_responses())
    built = fake_openai(monkeypatch, client)
    db_path = tmp_path / "digest.db"
    args = ["ingest", str(DATA / "slack.json"), "--db", str(db_path)]

    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert built == [("fake-model", "fake-key")]
    gold = json.loads((DATA / "gold.json").read_text())
    assert len(client.calls) == len(gold["threads"])
    state = _snapshot(db_path)
    assert state["deltas"][0] == sum(len(t["deltas"]) for t in gold["threads"])

    second = CliRunner().invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "0 new or changed signals" in second.output
    assert len(client.calls) == len(gold["threads"])
    assert _snapshot(db_path) == state


def test_ingest_without_replay_needs_model_and_key(tmp_path):
    db_path = tmp_path / "digest.db"
    result = CliRunner().invoke(app, ["ingest", str(DATA / "slack.json"), "--db", str(db_path)])
    assert result.exit_code == 1
    assert MODEL_ENV in result.output and API_KEY_ENV in result.output
    assert not db_path.exists()
