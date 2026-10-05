"""A5 phrased digest: the model rewrites cards, and every failure mode returns the template."""

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from digest.attach import llm_render
from digest.attach.llm_render import (
    RENDER_TIMEOUT_S,
    RENDERER_MODEL_ENV,
    LLMRenderer,
    RendererUnavailable,
    build_renderer,
)
from digest.cli import app
from digest.core.extract import API_KEY_ENV
from digest.core.render import TemplateRenderer
from digest.models import Scored
from tests.test_run import MAYA, _item


class FakeRenderClient:
    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def complete(self, instructions: str, prompt: str, schema: dict[str, Any]) -> str:
        self.calls.append((instructions, prompt, schema))
        if self.error is not None:
            raise self.error
        return self.response


def _items() -> list[Scored]:
    return [
        Scored(item=_item(1, "drive", "deadline_change"), score=1.0),
        Scored(item=_item(2, "gripper", "value_change"), score=0.9),
    ]


def _entries(target_ids: list[str]) -> str:
    return json.dumps({
        "entries": [
            {"target_id": t, "headline": f"Headline for {t}.", "why": f"Why {t} matters."}
            for t in target_ids
        ]
    })


def test_phrased_digest_keeps_shape_and_order():
    items = _items()
    client = FakeRenderClient(response=_entries(["T01", "T02"]))
    body = LLMRenderer(client).render(MAYA, items)

    assert body != TemplateRenderer().render(MAYA, items)
    assert body.startswith("# Digest for Maya Chen")
    assert body.index("## Drive") < body.index("## Gripper")
    assert "1. **Headline for T01.**" in body
    assert "   Why T01 matters. (#drive https://example/1)" in body
    assert "2. **Headline for T02.**" in body
    assert len(client.calls) == 1
    instructions, prompt, schema = client.calls[0]
    assert "T01" in prompt and "mechanical engineer" in prompt
    assert schema == llm_render.PhrasedDigest.model_json_schema()


@pytest.mark.parametrize("client", [
    FakeRenderClient(error=TimeoutError("read timed out")),          # timeout
    FakeRenderClient(error=RuntimeError("api exploded")),            # error
    FakeRenderClient(response="not json"),                           # malformed response
    FakeRenderClient(response=_entries(["T01"])),                    # dropped an item
    FakeRenderClient(response=_entries(["T01", "T02", "T03"])),      # added an item
    FakeRenderClient(response=_entries(["T01", "T99"])),             # swapped an item
    FakeRenderClient(response=_entries(["T02", "T01"])),             # reordered items
], ids=["timeout", "error", "malformed", "dropped", "added", "swapped", "reordered"])
def test_every_failure_mode_returns_the_template(client):
    items = _items()
    assert LLMRenderer(client).render(MAYA, items) == TemplateRenderer().render(MAYA, items)


def test_empty_digest_skips_the_model():
    client = FakeRenderClient(error=AssertionError("must not be called"))
    assert LLMRenderer(client).render(MAYA, []) == TemplateRenderer().render(MAYA, [])
    assert client.calls == []


def test_build_renderer_registry(monkeypatch):
    assert isinstance(build_renderer("template"), TemplateRenderer)
    with pytest.raises(RendererUnavailable, match="unknown renderer 'nope'"):
        build_renderer("nope")
    with pytest.raises(RendererUnavailable, match=RENDERER_MODEL_ENV):
        build_renderer("llm")  # conftest cleared the env

    built = []
    monkeypatch.setenv(RENDERER_MODEL_ENV, "fake-model")
    monkeypatch.setenv(API_KEY_ENV, "fake-key")
    monkeypatch.setattr(
        llm_render, "OpenAIClient",
        lambda model, api_key, timeout: built.append((model, api_key, timeout)) or FakeRenderClient(),
    )
    assert isinstance(build_renderer("llm"), LLMRenderer)
    assert built == [("fake-model", "fake-key", RENDER_TIMEOUT_S)]


def test_run_with_llm_renderer_and_no_env_exits_clearly(tmp_path):
    result = CliRunner().invoke(
        app, ["run", "--user", "maya", "--date", "2026-03-12", "--db", str(tmp_path / "x.db"),
              "--render", "llm"],
    )
    assert result.exit_code == 1
    assert RENDERER_MODEL_ENV in result.output
