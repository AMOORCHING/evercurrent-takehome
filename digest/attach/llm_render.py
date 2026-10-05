"""A5 phrased digest with fallback: model output is an enhancement; the template is the contract.

`LLMRenderer` wraps `TemplateRenderer`. One structured-output call rewrites the ranked
cards into two-line entries - a headline and a why-plus-source line - keeping the
template's product grouping and item order. The template body renders first and is the
answer whenever the model does not strictly improve on it: on timeout, on any API or
validation error, and on a response whose entries do not match the input items one for
one (anything dropped, added, swapped or reordered). A digest is therefore never lost
or distorted by the model.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from digest.core.extract import API_KEY_ENV, ModelClient, OpenAIClient
from digest.core.render import TemplateRenderer
from digest.models import InboxItem, Renderer, Scored, User

log = logging.getLogger(__name__)

RENDERER_MODEL_ENV = "DIGEST_RENDERER_MODEL"

# One digest is a handful of cards; a render slower than this is worth less than the
# template that is already in hand.
RENDER_TIMEOUT_S = 30.0


class PhrasedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    headline: str
    why: str


class PhrasedDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[PhrasedEntry]


INSTRUCTIONS = """\
You rewrite one person's engineering digest cards into two-line entries. For every card
in the prompt, in the same order, return one entry:

- target_id: the task or requirement ID, copied exactly from the card heading.
- headline: one sentence under 15 words: what changed and the consequence that matters
  to this reader. Lead with the change, not the ID.
- why: one sentence: why this reaches them and what, if anything, to do about it.

Use only facts from the card. Never merge, drop, add or reorder cards.
"""


class RendererUnavailable(RuntimeError):
    """The renderer cannot be built, for example because its environment variables are unset."""


class LLMRenderer(Renderer):
    """Rewrites the top items into two-line entries, falling back to the wrapped renderer."""

    def __init__(self, client: ModelClient, fallback: Renderer | None = None) -> None:
        self._client = client
        self._fallback = fallback if fallback is not None else TemplateRenderer()

    @classmethod
    def from_env(cls) -> LLMRenderer:
        """Model name from DIGEST_RENDERER_MODEL, API key from OPENAI_API_KEY."""
        env = {name: os.environ.get(name) for name in (RENDERER_MODEL_ENV, API_KEY_ENV)}
        missing = [name for name, value in env.items() if not value]
        if missing:
            raise RendererUnavailable(f"set {' and '.join(missing)}")
        return cls(OpenAIClient(model=env[RENDERER_MODEL_ENV], api_key=env[API_KEY_ENV],
                                timeout=RENDER_TIMEOUT_S))

    def render(self, user: User, items: list[Scored]) -> str:
        template = self._fallback.render(user, items)
        if not items:
            return template
        try:
            text = self._client.complete(
                INSTRUCTIONS, self._prompt(user, template), PhrasedDigest.model_json_schema()
            )
            phrased = PhrasedDigest.model_validate_json(text)
        except Exception as e:  # timeout, API error, malformed or invalid response
            log.warning("LLM renderer for %s fell back: %s", user.id, e)
            return template

        expected = [s.item.delta.target_id for s in items]
        returned = [entry.target_id for entry in phrased.entries]
        if returned != expected:
            log.warning(
                "LLM renderer for %s fell back: items are %s, response carried %s",
                user.id, expected, returned,
            )
            return template
        return _body(user, items, phrased.entries)

    def _prompt(self, user: User, template: str) -> str:
        return f"Reader: {user.name} ({user.role.replace('_', ' ')})\n\nCards, in order:\n\n{template}"


def _body(user: User, items: list[Scored], entries: list[PhrasedEntry]) -> str:
    """The template's shape - heading, products in first-appearance order, numbered
    items - with each card replaced by its two-line entry."""
    lines = [f"# Digest for {user.name}", ""]
    by_product: dict[str, list[tuple[InboxItem, PhrasedEntry]]] = {}
    for scored, entry in zip(items, entries):
        by_product.setdefault(scored.item.product_id, []).append((scored.item, entry))

    number = 0
    for pairs in by_product.values():
        lines += [f"## {pairs[0][0].product_name}", ""]
        for item, entry in pairs:
            number += 1
            lines += [
                f"{number}. **{entry.headline}**",
                f"   {entry.why} (#{item.signal.channel} {item.source_url})",
                "",
            ]
    return "\n".join(lines).rstrip("\n") + "\n"


RENDERERS: dict[str, Callable[[], Renderer]] = {
    "template": TemplateRenderer,
    "llm": LLMRenderer.from_env,
}


def build_renderer(name: str) -> Renderer:
    """Raises `RendererUnavailable` for an unknown name or missing environment."""
    if name not in RENDERERS:
        raise RendererUnavailable(f"unknown renderer {name!r}; one of: {', '.join(RENDERERS)}")
    return RENDERERS[name]()
