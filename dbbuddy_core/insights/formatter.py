"""Normalize raw model output into an :class:`InsightBundle`, and render it.

Models are inconsistent about the *shape* of a correct answer: a string where a
list was asked for, a bare list of strings where objects were asked for, a key
named ``key_findings``. None of that is a quality failure worth failing the
provider over, so it is normalized here. Genuinely unusable output (not JSON at
all) raises, and the caller's chain fails over to the next provider.
"""

from __future__ import annotations

import json

from .models import Finding, InsightBundle
from .prompts import CANNOT_DETERMINE, PROMPT_VERSION
from .validators import strip_sql, validate_answer, validate_findings


def parse_json_object(text: str) -> dict:
    """Extract the first JSON object from a model response.

    Raises ``ValueError`` if there is none — deliberately, so
    ``generate_with_chain`` treats it as a failed provider and fails over.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    stripped = text.strip()
    # Unwrap a ```json fence before looking for the object.
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.lstrip().lower().startswith("json"):
            stripped = stripped.lstrip()[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    parsed = json.loads(stripped[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON was not an object")
    return parsed


def _as_str_list(value) -> list[str]:
    """Coerce whatever arrived into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return [str(value)]
    out = []
    for item in value:
        text = (item if isinstance(item, str) else json.dumps(item, default=str)).strip()
        if text:
            out.append(text)
    return out


def _first_key(data: dict, *names):
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return None


def _as_findings(value) -> list[Finding]:
    """Build findings from the model's ``findings`` value.

    A bare list of strings is accepted but yields findings with empty evidence —
    which the validator then drops. That is the intended outcome: an uncited
    observation does not become citable by being reformatted.
    """
    findings: list[Finding] = []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return findings

    for item in value:
        if isinstance(item, str):
            findings.append(Finding(title="Observation", detail=item.strip(), evidence=""))
            continue
        if not isinstance(item, dict):
            continue
        detail = _first_key(item, "detail", "description", "text", "finding") or ""
        evidence = _first_key(item, "evidence", "support", "basis") or ""
        title = _first_key(item, "title", "label", "name") or "Observation"
        findings.append(Finding(
            title=strip_sql(str(title))[:200],
            detail=strip_sql(str(detail)),
            evidence=strip_sql(str(evidence)),
        ))
    return findings


def build_bundle(raw: dict, *, provider: str | None,
                 columns: list[str] | None = None,
                 numbers: set[str] | None = None) -> InsightBundle:
    """Normalize + guardrail raw model JSON into the bundle the API returns.

    ``columns`` are the result's column names and ``numbers`` its actual values
    (integer-part keys), used together to check that any asserted cause is
    grounded in the data rather than imported from outside it.
    """
    summary = strip_sql(str(_first_key(raw, "summary", "executive_summary", "overview") or ""))

    findings, limitation_notes = validate_findings(
        _as_findings(_first_key(raw, "findings", "key_findings", "observations", "insights")),
        columns, numbers,
    )
    recommendations = _as_str_list(_first_key(raw, "recommendations", "actions", "next_steps"))
    limitations = _as_str_list(_first_key(raw, "limitations", "caveats"))

    # A summary that invented a cause is not salvageable by trimming — replace it
    # and let the limitation explain the gap.
    from .validators import banned_topics_in, ungrounded_causal_claim

    topics = banned_topics_in(summary)
    if topics:
        limitations.append(
            "The generated summary referenced factors outside this data ("
            + ", ".join(sorted(topics)) + ") and was withheld."
        )
        summary = CANNOT_DETERMINE
    elif ungrounded_causal_claim(summary, columns, numbers):
        limitations.append(
            "The generated summary explained the result using factors it does not "
            "contain and was withheld. This data shows what happened, not why."
        )
        summary = CANNOT_DETERMINE
    limitations.extend(limitation_notes)

    # Recommendations premised on invented causes go too — an action resting on
    # something the data never showed is worse than no recommendation.
    recommendations = [
        r for r in recommendations
        if not banned_topics_in(r) and not ungrounded_causal_claim(r, columns, numbers)
    ]

    if not summary:
        summary = CANNOT_DETERMINE
    if not limitations:
        limitations = ["Based only on the rows returned by this query."]

    # De-duplicate while preserving order: the summary, findings, and recommendation
    # paths can each contribute the same note, and three identical bullets read as
    # a rendering bug rather than as emphasis.
    limitations = list(dict.fromkeys(limitations))

    return InsightBundle(
        summary=summary, findings=findings, recommendations=recommendations,
        limitations=limitations, provider=provider, prompt_version=PROMPT_VERSION,
    )


def build_answer(raw: dict, *, provider: str | None,
                 columns: list[str] | None = None,
                 numbers: set[str] | None = None) -> dict:
    """Normalize + guardrail a follow-up response."""
    answer = str(_first_key(raw, "answer", "response", "text") or "")
    evidence = str(_first_key(raw, "evidence", "support", "basis") or "")
    limitations = _as_str_list(_first_key(raw, "limitations", "caveats"))

    answer, notes = validate_answer(answer, evidence, columns, numbers)
    limitations.extend(notes)
    if not answer:
        answer = CANNOT_DETERMINE
    return {
        "answer": answer,
        "evidence": strip_sql(evidence),
        "limitations": limitations,
        "provider": provider,
        "prompt_version": PROMPT_VERSION,
    }


def to_markdown(bundle: InsightBundle) -> str:
    """Render a bundle as markdown — what the UI's Copy button yields."""
    lines = ["## Summary", "", bundle.summary, ""]
    if bundle.findings:
        lines.append("## Key findings")
        lines.append("")
        for finding in bundle.findings:
            lines.append(f"**{finding.title}** — {finding.detail}")
            lines.append(f"> Evidence: {finding.evidence}")
            lines.append("")
    if bundle.recommendations:
        lines.append("## Recommendations")
        lines.append("")
        lines.extend(f"- {r}" for r in bundle.recommendations)
        lines.append("")
    if bundle.limitations:
        lines.append("## Limitations")
        lines.append("")
        lines.extend(f"- {limitation}" for limitation in bundle.limitations)
        lines.append("")
    return "\n".join(lines).strip() + "\n"
