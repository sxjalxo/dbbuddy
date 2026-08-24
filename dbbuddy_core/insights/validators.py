"""Phase 5 — post-hoc guardrail enforcement.

Prompt rules are an instruction, not a control: a model can ignore them, and a
leading follow-up ("why did Electronics do badly?") invites exactly the invented
causality the engine promises never to produce. So every response is scanned
after the fact, and the scan — not the prompt — is what the guarantee rests on.

Two different failure modes, two different responses:

* **Invented external causality** (weather, competitors, the economy) is not a
  weak claim, it is a fabricated one. Those findings are *removed*, and the
  removal is surfaced as a limitation rather than silently swallowed.
* **Hedged or speculative language** ("this probably indicates…") is a real
  observation stated too loosely. Those findings are *tapered* — confidence is
  down-weighted, and they stay. Deleting them would read as breakage, and the
  underlying number is usually sound.
"""

from __future__ import annotations

import re

from .models import Finding
from .prompts import CANNOT_DETERMINE

# Externalities the engine has no evidence for and must never assert. Matched on
# word boundaries so "seasonal" does not trip on "season ticket sales" as a value.
BANNED_TOPICS = {
    "weather": r"\b(weather|rain|rainfall|snow|temperature outside|climate)\b",
    "competitors": r"\b(competitor|competitors|competition|rival|market share)\b",
    "economy": r"\b(recession|inflation|economic downturn|economy|market conditions)\b",
    "holidays": r"\b(holiday season|christmas|black friday|festive season)\b",
    "marketing_speculation": r"\b(brand perception|customer sentiment|word of mouth)\b",
}

# Connectives that introduce a *cause*. The engine's contract is that it reports
# what the data shows, never why — so an asserted cause has to be grounded in the
# result or it is invention, whatever vocabulary it is dressed in.
CAUSAL_CONNECTIVES = r"\b(because of|because|due to|caused by|driven by|attributable to|as a result of|owing to|stemming from|resulted from|resulting from|thanks to|on account of|led to|explained by)\b"

# Hedging that signals the model went past its evidence without inventing a cause.
SPECULATION_MARKERS = r"\b(probably|likely|presumably|may have|might have|could have|perhaps|suggests that|it seems|appears to be due to|possibly)\b"

# The engine never produces SQL. A response containing a statement is a contract
# violation regardless of whether the SQL is valid.
SQL_MARKERS = r"\b(SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE)\b"

# How far a hedged finding is knocked down. Enough to sort below clean findings,
# not so far that it reads as suppressed.
SPECULATION_PENALTY = 0.5


def banned_topics_in(text: str) -> list[str]:
    """Names of every banned topic asserted in ``text`` (empty when clean)."""
    if not text:
        return []
    return [topic for topic, pattern in BANNED_TOPICS.items()
            if re.search(pattern, text, re.IGNORECASE)]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", (text or "").lower()))


def ungrounded_causal_claim(text: str, columns: list[str] | None,
                            numbers: set[str] | None = None) -> bool:
    """True if ``text`` asserts a cause that the result cannot support.

    The banned-topic list is a fast hard-block for the classic offenders, but it is
    a *vocabulary*, and a vocabulary cannot enumerate the world: "seasonality",
    "supply chain issues", "macroeconomic headwinds" and "consumer confidence" all
    sail straight through it. This check is grounded in the evidence instead of in
    a word list — it asks whether the thing being blamed is something the result
    actually contains.

    The clause after a causal connective must reference a column name from the
    context (or a number that appears in the result). If it names neither, the
    model is explaining the data with something outside the data.

    ``numbers`` is the set of integer-part keys the result actually contains (from
    :func:`dbbuddy_core.insights.context.grounded_numbers`). When supplied, a cited
    figure only grounds the clause if it is one of those values — so a *fabricated*
    number ("because of 47 supply-chain disruptions") no longer launders an
    external cause. When ``numbers`` is None the check stays permissive and treats
    any digit as evidence, which is the safe default for callers that cannot see
    the result values (they still have the column-name net).

    ``columns`` unknown (None) disables the check rather than guessing: a false
    positive here silently deletes a correct finding, which is worse than leaving
    the denylist as the only net.
    """
    if not text or columns is None:
        return False

    vocabulary = set()
    for column in columns:
        # Match on name parts too, so "revenue" grounds a claim about "total_revenue".
        vocabulary.update(_tokens(column.replace("_", " ")))
        vocabulary.add(column.lower())

    for match in re.finditer(CAUSAL_CONNECTIVES, text, re.IGNORECASE):
        clause = text[match.end():]
        # Stop at the sentence boundary — the next sentence is a separate claim.
        # A period only ends a sentence when it is NOT between digits: splitting on
        # a bare "." truncates "15.3% decline in revenue" to "15", dropping the very
        # column name ("revenue") that would ground the clause — deleting a correct
        # finding, the costliest error here. ``\.(?!\d)`` keeps decimals intact.
        clause = re.split(r";|\.(?!\d)", clause, maxsplit=1)[0]
        if _clause_cites_a_result_number(clause, numbers):
            continue  # cites a figure the result actually contains
        if _tokens(clause) & vocabulary:
            continue  # names a column present in the result
        return True
    return False


def _clause_cites_a_result_number(clause: str, numbers: set[str] | None) -> bool:
    """Whether a causal clause cites a figure that grounds it.

    ``numbers`` None → any digit counts (permissive default, column net still
    applies). ``numbers`` given → only a digit-run whose integer part is an actual
    result value counts; a fabricated figure does not.
    """
    runs = re.findall(r"\d[\d,]*(?:\.\d+)?", clause)
    if not runs:
        return False
    if numbers is None:
        return True
    for run in runs:
        integer_part = run.replace(",", "").split(".")[0]
        if integer_part in numbers:
            return True
    return False


def contains_speculation(text: str) -> bool:
    return bool(text) and bool(re.search(SPECULATION_MARKERS, text, re.IGNORECASE))


def contains_sql(text: str) -> bool:
    return bool(text) and bool(re.search(SQL_MARKERS, text, re.IGNORECASE | re.DOTALL))


def strip_sql(text: str) -> str:
    """Remove fenced code blocks from free text.

    A model that ignores rule 4 usually does it inside a fence. Dropping the fence
    keeps the surrounding prose (which is often a perfectly good observation)
    rather than discarding the whole response over a formatting habit.
    """
    return re.sub(r"```.*?```", "", text or "", flags=re.DOTALL).strip()


def validate_findings(
    findings: list[Finding], columns: list[str] | None = None,
    numbers: set[str] | None = None,
) -> tuple[list[Finding], list[str]]:
    """Filter and taper a list of findings.

    Returns ``(kept, limitations)``. A finding is dropped when it asserts a banned
    external cause, blames something absent from the result, contains SQL, or
    cites no evidence; it is tapered when it hedges. Kept findings come back
    sorted by confidence, so a tapered one falls below the clean ones without
    disappearing.
    """
    kept: list[Finding] = []
    limitations: list[str] = []
    dropped_topics: set[str] = set()
    dropped_ungrounded = 0
    dropped_unsupported = 0

    for finding in findings:
        blob = f"{finding.title} {finding.detail} {finding.evidence}"

        topics = banned_topics_in(blob)
        if topics:
            # Fabricated causality — remove it, and record *why* so the user sees a
            # gap rather than a quietly shortened list.
            dropped_topics.update(topics)
            continue
        if ungrounded_causal_claim(f"{finding.title} {finding.detail}", columns, numbers):
            dropped_ungrounded += 1
            continue
        if contains_sql(blob):
            dropped_unsupported += 1
            continue
        if not finding.evidence.strip():
            # Rule 5. An uncitable finding is indistinguishable from an invented one.
            dropped_unsupported += 1
            continue

        if contains_speculation(blob):
            finding.confidence = round(finding.confidence * SPECULATION_PENALTY, 3)
        kept.append(finding)

    if dropped_topics:
        limitations.append(
            "Some generated findings referenced factors outside this data ("
            + ", ".join(sorted(dropped_topics))
            + ") and were removed. This result cannot support conclusions about them."
        )
    if dropped_ungrounded:
        limitations.append(
            f"{dropped_ungrounded} generated finding(s) explained the result using "
            "factors it does not contain and were removed. This data shows what "
            "happened, not why."
        )
    if dropped_unsupported:
        limitations.append(
            f"{dropped_unsupported} generated finding(s) cited no evidence from the "
            "result and were removed."
        )

    kept.sort(key=lambda f: f.confidence, reverse=True)
    return kept, limitations


def validate_answer(answer: str, evidence: str,
                    columns: list[str] | None = None,
                    numbers: set[str] | None = None) -> tuple[str, list[str]]:
    """Guardrail a follow-up answer.

    An answer that invents an external cause is replaced wholesale with the
    "cannot determine" sentence — for a direct question there is no salvageable
    partial answer, and a hedged version of a fabricated cause is still fabricated.
    """
    answer = strip_sql(answer)
    limitations: list[str] = []

    topics = banned_topics_in(answer)
    if topics:
        return CANNOT_DETERMINE, [
            "The generated answer relied on factors not present in this data ("
            + ", ".join(sorted(topics))
            + "). Answering it would require additional data sources."
        ]
    if ungrounded_causal_claim(answer, columns, numbers):
        # A follow-up is usually a "why" question, which is exactly where a model
        # reaches outside the evidence. No partial answer is salvageable here.
        return CANNOT_DETERMINE, [
            "The generated answer explained the result using factors it does not "
            "contain. This data shows what happened, not why."
        ]
    if contains_sql(answer):
        return CANNOT_DETERMINE, [
            "The generated answer contained SQL. The Insights Engine explains "
            "results; it does not write queries."
        ]

    if answer.strip() != CANNOT_DETERMINE and not evidence.strip():
        # A substantive claim with nothing behind it. Keep the answer (it may be a
        # restatement of the visible numbers) but flag that it is uncited.
        limitations.append("This answer was not tied to specific values in the result.")
    if contains_speculation(answer):
        limitations.append(
            "This answer contains qualified language; the data supports what "
            "happened, not why."
        )
    return answer.strip(), limitations
