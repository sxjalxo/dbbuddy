"""Orchestration: context → prompt → provider chain → validate → format.

The chain call goes through :func:`dbbuddy_core.ai_providers.generate_with_chain`,
the same resilience path column labeling uses — per-provider retries, circuit
breaker, and metrics come for free, and there is no second provider abstraction
to keep in sync.

Nothing here raises for a provider failure. An unreachable chain yields an honest
bundle saying so, because a panel that explains why it is empty is more useful
than a 500.
"""

from __future__ import annotations

import logging

from dbbuddy_core.ai_providers import ProviderRuntimeConfig, generate_with_chain

from .context import grounded_numbers
from .formatter import build_answer, build_bundle, parse_json_object
from .models import InsightBundle, ResultContext, settings
from .prompts import (
    CANNOT_DETERMINE,
    PROMPT_VERSION,
    build_followup_prompt,
    build_insight_prompt,
)

logger = logging.getLogger(__name__)

_UNAVAILABLE = (
    "No AI provider could be reached, so this result has not been analyzed. "
    "The query results themselves are unaffected."
)


class InsightsDisabled(RuntimeError):
    """Raised when the feature is switched off by configuration (Phase 8)."""


def _check_enabled(config) -> None:
    if not config.enabled:
        raise InsightsDisabled(
            "AI Insights is disabled by configuration (INSIGHTS_ENABLED=0)."
        )


def generate_insights(
    context: ResultContext,
    chain: list[ProviderRuntimeConfig] | None,
    *,
    config=None,
) -> InsightBundle:
    """Phase 3 — analyze a shaped result context.

    An empty result is analyzed like any other: the prompt tells the model to
    report it plainly. Short-circuiting it here would skip the guardrails and
    hand back an unvalidated string.
    """
    cfg = config or settings
    _check_enabled(cfg)

    if not chain:
        return InsightBundle(
            summary=_UNAVAILABLE,
            limitations=["Configure an AI provider for this organization to enable insights."],
            prompt_version=PROMPT_VERSION,
        )

    outcome = generate_with_chain(
        build_insight_prompt(context), chain,
        parse=parse_json_object, temperature=cfg.temperature, json_mode=True,
    )
    if outcome is None:
        logger.info("Insights generation exhausted the provider chain for %s", context.database)
        return InsightBundle(
            summary=_UNAVAILABLE,
            limitations=["Every configured AI provider failed or was unreachable."],
            prompt_version=PROMPT_VERSION,
        )

    raw, provider_label = outcome
    return build_bundle(raw, provider=provider_label,
                        columns=[c.name for c in context.columns],
                        numbers=grounded_numbers(context))


def answer_followup(
    context: ResultContext,
    question: str,
    chain: list[ProviderRuntimeConfig] | None,
    *,
    history: list[dict] | None = None,
    config=None,
) -> dict:
    """Phase 4 — answer a follow-up bound to the same evidence packet."""
    cfg = config or settings
    _check_enabled(cfg)

    if not question or not question.strip():
        return {
            "answer": CANNOT_DETERMINE, "evidence": "",
            "limitations": ["No question was asked."],
            "provider": None, "prompt_version": PROMPT_VERSION,
        }

    if not chain:
        return {
            "answer": _UNAVAILABLE, "evidence": "",
            "limitations": ["Configure an AI provider for this organization to enable insights."],
            "provider": None, "prompt_version": PROMPT_VERSION,
        }

    outcome = generate_with_chain(
        build_followup_prompt(context, question.strip(), history, config=cfg), chain,
        parse=parse_json_object, temperature=cfg.temperature, json_mode=True,
    )
    if outcome is None:
        return {
            "answer": _UNAVAILABLE, "evidence": "",
            "limitations": ["Every configured AI provider failed or was unreachable."],
            "provider": None, "prompt_version": PROMPT_VERSION,
        }

    raw, provider_label = outcome
    return build_answer(raw, provider=provider_label,
                        columns=[c.name for c in context.columns],
                        numbers=grounded_numbers(context))
