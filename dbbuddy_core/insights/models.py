"""Data shapes for the Insights Engine, plus its environment-backed settings.

Everything here is a plain dataclass with a ``to_dict`` — the router serializes
these directly, and the tests assert on them without touching FastAPI or the
application database.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    """Read a float setting, ignoring an unparseable value rather than failing at
    import time — a typo in one tuning knob must not stop the process booting."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class InsightsSettings:
    """Phase 8 configuration. Provider choice is deliberately absent: insights use
    the organization's existing AI provider chain, not a separate selection."""

    enabled: bool = field(default_factory=lambda: _env_flag("INSIGHTS_ENABLED"))
    # Low by design. This is analysis, not prose — near-deterministic keeps the
    # model close to the numbers it was given.
    temperature: float = field(default_factory=lambda: _env_float("INSIGHTS_TEMPERATURE", 0.2))
    max_sample_rows: int = field(default_factory=lambda: _env_int("INSIGHTS_MAX_SAMPLE_ROWS", 20))
    max_columns: int = field(default_factory=lambda: _env_int("INSIGHTS_MAX_COLUMNS", 40))
    max_cell_chars: int = field(default_factory=lambda: _env_int("INSIGHTS_MAX_CELL_CHARS", 120))
    max_history_turns: int = field(default_factory=lambda: _env_int("INSIGHTS_MAX_HISTORY_TURNS", 8))
    # Per-turn clip for the client-supplied transcript, which is attacker-controlled.
    max_turn_chars: int = field(default_factory=lambda: _env_int("INSIGHTS_MAX_TURN_CHARS", 1000))
    cache_ttl_hours: int = field(default_factory=lambda: _env_int("INSIGHTS_CACHE_TTL_HOURS", 24))


settings = InsightsSettings()


@dataclass
class ColumnProfile:
    """One column as the model sees it: shape and statistics, never the full data."""

    name: str
    kind: str  # "numeric" | "date" | "text"
    null_count: int = 0
    distinct_count: int = 0
    # Populated for numeric columns only; ``change`` is last − first, which is what
    # a trend question ("revenue declined 18%") is actually asking about.
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    total: float | None = None
    first: float | None = None
    last: float | None = None
    change: float | None = None
    change_pct: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ResultContext:
    """The bounded evidence packet — the *only* thing that reaches the model.

    Built by :mod:`.context` from an executed result. Bounded on every axis
    (rows sampled, columns profiled, characters per cell) so a million-row result
    and a ten-row result produce contexts of comparable size.
    """

    question: str
    sql: str
    database: str
    row_count: int
    columns: list[ColumnProfile] = field(default_factory=list)
    sample_rows: list[dict] = field(default_factory=list)
    chart_type: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "sql": self.sql,
            "database": self.database,
            "row_count": self.row_count,
            "columns": [c.to_dict() for c in self.columns],
            "sample_rows": self.sample_rows,
            "chart_type": self.chart_type,
            "truncated": self.truncated,
        }


@dataclass
class Finding:
    """One observation. ``evidence`` is mandatory — a finding that cannot point at
    something in the result set is dropped by the validator, not shown with a
    caveat. ``confidence`` is tapered down by weak-language detection rather than
    used as a delete threshold."""

    title: str
    detail: str
    evidence: str
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InsightBundle:
    """The rendered result of one insight generation."""

    summary: str
    findings: list[Finding] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    provider: str | None = None
    prompt_version: str = ""
    cached: bool = False

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "recommendations": self.recommendations,
            "limitations": self.limitations,
            "provider": self.provider,
            "prompt_version": self.prompt_version,
            "cached": self.cached,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InsightBundle":
        """Rebuild a bundle from its serialized form (the cache round-trip)."""
        return cls(
            summary=data.get("summary", ""),
            findings=[
                Finding(
                    title=f.get("title", ""), detail=f.get("detail", ""),
                    evidence=f.get("evidence", ""), confidence=f.get("confidence", 1.0),
                )
                for f in data.get("findings", [])
            ],
            recommendations=list(data.get("recommendations", [])),
            limitations=list(data.get("limitations", [])),
            provider=data.get("provider"),
            prompt_version=data.get("prompt_version", ""),
            cached=data.get("cached", False),
        )
