import pytest

from digest.attach.deciders import DECIDER_MODEL_ENV, JEV_KEY_ENV
from digest.attach.llm_render import RENDERER_MODEL_ENV
from digest.core.extract import API_KEY_ENV, MODEL_ENV


@pytest.fixture(autouse=True)
def _no_model_credentials(monkeypatch):
    """No test reaches a real model: the LLM extractor, renderer and decider backends are
    unavailable unless a test fakes their clients."""
    for name in (MODEL_ENV, API_KEY_ENV, JEV_KEY_ENV, DECIDER_MODEL_ENV, RENDERER_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
