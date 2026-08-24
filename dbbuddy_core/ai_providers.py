"""Provider-agnostic AI adapter registry.

DB Buddy's AI layer does one thing: **semantic column labeling** (classify each
column into one of a small canonical vocabulary). This module makes the *provider*
behind that call a matter of configuration rather than code:

  * an :class:`AIProvider` implements only the transport (``generate`` / ``healthcheck``);
  * the shared classification prompt-building and JSON parsing live once, here;
  * a registry resolves an adapter by name — the single place an adapter string is
    dispatched. Everywhere else branches on *capabilities*, never on adapter name.

The engine stays decoupled from the application database: callers pass a flat,
self-contained :class:`ProviderRuntimeConfig` (or an ordered *chain* of them for
fallback). The backend resolves those from its per-org provider records; the CLI
can build them directly. Adding a provider that speaks the OpenAI or Ollama
protocol needs no code here — just a new config. A genuinely different API (e.g.
native Anthropic/Gemini/Bedrock) is a new :class:`AIProvider` subclass + one
``register(...)`` call.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from dbbuddy_core import safe_http

from dbbuddy_core import ai_metrics

# Reuse the existing, well-tested labeling helpers rather than duplicating them.
from dbbuddy_core.ai import (
    OLLAMA_KEEP_ALIVE,
    OLLAMA_TIMEOUT,
    OLLAMA_URL,
    _OLLAMA_NO_PROXY,
    _as_result,
    _clear_provider_error,
    _extract_json_object,
    _fallback_result,
    _match_value,
    _record_provider_error,
    build_knowledge_base,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderCapabilities:
    """What an adapter's transport can do, so callers branch on capability rather
    than on adapter name (mirrors the SQL dialect-capabilities pattern). Only
    ``supports_json_mode`` is exercised today; the rest are declared now so future
    features (streaming, embeddings, tool calls) never need a schema change."""

    supports_json_mode: bool = True
    supports_streaming: bool = False
    supports_embeddings: bool = False
    supports_tools: bool = False


@dataclass
class ProviderRuntimeConfig:
    """Everything an adapter needs to make one call — flat and self-contained.

    Fallback is *not* nested here: a caller that wants resilience passes an ordered
    ``list[ProviderRuntimeConfig]`` (built and cycle-checked once, upstream). Keeping
    this flat avoids recursive dataclasses and makes the config trivial to log and
    serialize.
    """

    adapter: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    name: str = ""
    timeout: int = 60


class RecoverableProviderError(Exception):
    """An infrastructure/provider failure that should fall through to the next
    provider in the chain — a missing key or a permanent 4xx (bad model/request).
    Retrying the *same* provider won't help, so it fails over immediately.
    A *bad model answer* is never a RecoverableProviderError; quality does not
    trigger fallback."""


class TransientProviderError(RecoverableProviderError):
    """A *transient* failure worth retrying the same provider before failing over —
    timeout, connection error, HTTP 429, or 5xx. Carries an optional server-hinted
    ``retry_after`` (seconds, parsed from a rate-limit response) and a
    ``rate_limited`` flag for metrics. Subclasses RecoverableProviderError so that,
    once retries are exhausted, it still falls through to the next provider."""

    def __init__(self, message: str, *, retry_after: float | None = None, rate_limited: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class RetryPolicy:
    """How aggressively to retry a single provider before failing over. Defaults are
    tuned for the one-time Analyze labeling call and overridable via env for ops."""

    max_attempts: int = 3       # total tries per provider (1 = no retry)
    base_delay: float = 0.5     # seconds; exponential base
    max_delay: float = 8.0      # cap on any single backoff sleep
    retry_after_cap: float = 8.0  # ignore a server Retry-After larger than this → fail over now


DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts=int(os.getenv("AI_RETRY_MAX_ATTEMPTS", "3")),
    base_delay=float(os.getenv("AI_RETRY_BASE_DELAY", "0.5")),
    max_delay=float(os.getenv("AI_RETRY_MAX_DELAY", "8")),
    retry_after_cap=float(os.getenv("AI_RETRY_AFTER_CAP", "8")),
)

# Circuit-breaker tuning: after this many consecutive *transient* failures a
# provider is tripped OPEN and skipped (straight to fallback) for a cooldown that
# grows on repeated trips, capped — so an obviously-unhealthy provider stops
# costing every request its full retry budget.
BREAKER_THRESHOLD = int(os.getenv("AI_BREAKER_THRESHOLD", "5"))
BREAKER_BASE_COOLDOWN = float(os.getenv("AI_BREAKER_COOLDOWN", "60"))
BREAKER_MAX_COOLDOWN = float(os.getenv("AI_BREAKER_MAX_COOLDOWN", "300"))


def _parse_retry_after(response) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds.

    Fully defensive: a malformed or unparseable header simply yields ``None`` (fall
    back to exponential backoff) rather than raising inside the 429 path."""
    from datetime import datetime, timezone

    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        if parsed is None:
            return None
        if parsed.tzinfo is None:  # a naive HTTP-date is GMT by spec
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError):
        return None


class AIProvider(ABC):
    """Transport for a single AI provider. Subclasses implement only the wire call."""

    adapter: str = ""
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __init__(self, config: ProviderRuntimeConfig):
        self.config = config

    @abstractmethod
    def generate(self, prompt: str, *, temperature: float | None = None,
                 json_mode: bool | None = None) -> str:
        """Return the model's raw text for a single-turn prompt.

        ``temperature`` and ``json_mode`` default to the labeling behavior
        (deterministic, JSON-mode when the adapter supports it) so existing
        callers are unaffected; the Insights Engine overrides them.

        Must raise :class:`RecoverableProviderError` on any infrastructure failure
        (so the chain can fall through) and let genuinely unexpected errors surface.
        """

    def healthcheck(self) -> tuple[bool, str | None]:
        """Cheap transport probe: ask the model to reply "OK" and confirm a
        non-empty response. Deliberately does *not* run classification — it tests
        connectivity/credentials only, independent of the labeling prompt format."""
        try:
            text = self.generate("Reply with the single word: OK")
            return (bool(text and text.strip()), None)
        except RecoverableProviderError as exc:
            return (False, str(exc))
        except Exception as exc:  # noqa: BLE001 — report, don't crash the endpoint
            return (False, f"{type(exc).__name__}: {exc}")


class OpenAICompatibleProvider(AIProvider):
    """Any provider exposing the OpenAI Chat Completions API — OpenAI, NVIDIA/
    Nemotron, OpenRouter, Groq, Together, Azure, and Anthropic/Gemini via their
    compatible endpoints. Vendor differences are just ``base_url`` + ``model``."""

    adapter = "openai_compatible"
    capabilities = ProviderCapabilities(supports_json_mode=True)

    def generate(self, prompt: str, *, temperature: float | None = None,
                 json_mode: bool | None = None) -> str:
        cfg = self.config
        if not cfg.base_url:
            raise RecoverableProviderError(f"{cfg.name or 'Provider'} has no base URL configured.")
        if not cfg.api_key:
            # Most compatible endpoints require a key; treat missing as recoverable
            # so a configured fallback can take over.
            raise RecoverableProviderError(f"{cfg.name or 'Provider'} has no API key configured.")

        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0 if temperature is None else temperature,
        }
        want_json = self.capabilities.supports_json_mode if json_mode is None else json_mode
        if want_json and self.capabilities.supports_json_mode:
            body["response_format"] = {"type": "json_object"}

        response = None
        try:
            # safe_http, not requests: it resolves the host once, validates
            # every address that lookup returned, and dials that address — so the
            # socket cannot open somewhere other than what was approved. See
            # dbbuddy_core/net_guard.py.
            response = safe_http.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers=headers, json=body, timeout=cfg.timeout,
            )
        except ValueError as exc:
            # A blocked destination is a refusal, not a transient failure — it
            # must not be retried against, and must not read as the provider
            # being briefly unavailable.
            raise RecoverableProviderError(
                f"{cfg.name or 'OpenAI-compatible'} endpoint refused: {exc}"
            ) from exc
        except requests.RequestException as exc:
            # Timeout / connection reset — transient, worth a retry.
            raise TransientProviderError(
                f"{cfg.name or 'OpenAI-compatible'} request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == 429:
            raise TransientProviderError(
                f"{cfg.name or 'OpenAI-compatible'} rate-limited (HTTP 429).",
                retry_after=_parse_retry_after(response), rate_limited=True,
            )
        if response.status_code >= 500:
            raise TransientProviderError(
                f"{cfg.name or 'OpenAI-compatible'} API returned HTTP {response.status_code}."
            )
        if response.status_code >= 400:
            # 4xx (bad key/model/request) — not something a retry on the same
            # provider fixes, but the chain's next provider might succeed.
            body_text = (response.text or "").strip().replace("\n", " ")[:300]
            raise RecoverableProviderError(
                f"{cfg.name or 'OpenAI-compatible'} API returned HTTP {response.status_code}: {body_text}"
            )

        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise RecoverableProviderError(
                f"{cfg.name or 'OpenAI-compatible'} returned an unexpected response shape."
            ) from exc


class OllamaProvider(AIProvider):
    """A local Ollama server (offline labeling). No API key; ``base_url`` defaults
    to the configured ``OLLAMA_URL`` and is never routed through a proxy."""

    adapter = "ollama"
    capabilities = ProviderCapabilities(supports_json_mode=True)

    def generate(self, prompt: str, *, temperature: float | None = None,
                 json_mode: bool | None = None) -> str:
        cfg = self.config
        url = (cfg.base_url or OLLAMA_URL).rstrip("/")
        want_json = self.capabilities.supports_json_mode if json_mode is None else json_mode
        try:
            response = safe_http.post(
                f"{url}/api/generate",
                json={
                    "model": cfg.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json" if (want_json and self.capabilities.supports_json_mode) else None,
                    # Ollama defaults num_ctx to 2048 regardless of the model's real
                    # window, so a labeling prompt (schema context + a batch of
                    # columns) plus its JSON reply overflows and comes back
                    # truncated — invalid JSON that reads as a failed provider. Raise
                    # the context and reply budget to the model's capacity; both are
                    # env-tunable for smaller local models.
                    "options": {
                        "temperature": 0 if temperature is None else temperature,
                        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
                        "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "4096")),
                    },
                    "keep_alive": OLLAMA_KEEP_ALIVE,
                },
                timeout=cfg.timeout or OLLAMA_TIMEOUT,
                proxies=_OLLAMA_NO_PROXY,
            )
        except ValueError as exc:
            # A blocked destination is a refusal, not a transient failure: it must
            # not be retried against.
            raise RecoverableProviderError(
                f"{cfg.name or 'Ollama'} endpoint refused: {exc}"
            ) from exc
        except requests.RequestException as exc:
            # Cold model load / connection blip — transient, worth a retry.
            raise TransientProviderError(
                f"{cfg.name or 'Ollama'} request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code >= 500:
            raise TransientProviderError(f"Ollama returned HTTP {response.status_code}.")
        if response.status_code >= 400:
            raise RecoverableProviderError(
                f"Ollama returned HTTP {response.status_code} (is model '{cfg.model}' pulled?)."
            )
        # Validate the response shape like the OpenAI-compatible sibling does, so a
        # non-JSON body or a changed API surface fails over cleanly instead of
        # letting a raw ValueError escape the transport.
        try:
            return response.json().get("response", "")
        except (ValueError, AttributeError) as exc:
            raise RecoverableProviderError(
                f"{cfg.name or 'Ollama'} returned an unexpected (non-JSON) response."
            ) from exc


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[AIProvider]] = {}


def register(adapter: str, cls: type[AIProvider]) -> None:
    _REGISTRY[adapter] = cls


def supported_adapters() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(config: ProviderRuntimeConfig) -> AIProvider:
    """Resolve an adapter for a runtime config. The *only* place an adapter name is
    dispatched — everything else stays adapter-agnostic."""
    cls = _REGISTRY.get(config.adapter)
    if cls is None:
        raise ValueError(
            f"Unknown AI adapter '{config.adapter}'. Registered: {', '.join(supported_adapters())}."
        )
    return cls(config)


register(OpenAICompatibleProvider.adapter, OpenAICompatibleProvider)
register(OllamaProvider.adapter, OllamaProvider)


# ── Shared, adapter-agnostic column classification ───────────────────────────

def _build_classification_prompt(column_keys: list[str], schema: dict | None) -> str:
    knowledge_base = build_knowledge_base(schema)
    return (
        "Use the connected database schema as the knowledge base. "
        "Classify each column into one word from: "
        "value, quantity, name, date, identifier, status, description.\n\n"
        "Return JSON mapping with the exact column keys provided.\n\n"
        f"Knowledge base: {knowledge_base}\n\n"
        f"Columns: {column_keys}"
    )


def _retry_delay(exc: TransientProviderError, attempt: int, policy: RetryPolicy) -> float | None:
    """Seconds to wait before retrying, or None to stop retrying this provider.

    Honors a server ``Retry-After`` when present (capped — a very long hint means
    "come back much later", so we fail over to the next provider instead). Otherwise
    exponential backoff with full jitter to avoid synchronized retries."""
    if exc.retry_after is not None:
        if exc.retry_after > policy.retry_after_cap:
            return None  # too long to wait inline → fail over now
        return min(exc.retry_after, policy.max_delay)
    backoff = min(policy.base_delay * (2 ** (attempt - 1)), policy.max_delay)
    return random.uniform(0, backoff)  # full jitter


def _generate_with_retries(
    provider: AIProvider, prompt: str, policy: RetryPolicy,
    *, temperature: float | None = None, json_mode: bool | None = None,
) -> tuple[str, int, bool]:
    """Call ``provider.generate`` with retry/backoff on *transient* failures.

    Returns ``(text, attempts, rate_limited)``. A non-transient
    :class:`RecoverableProviderError` propagates immediately (retrying won't help).
    An exhausted transient error also propagates (so the chain fails over), tagged
    with ``.attempts`` for accurate metrics."""
    attempt = 0
    rate_limited = False
    while True:
        attempt += 1
        try:
            # Only forward the tuning kwargs a caller actually set, so an adapter
            # written against the original single-argument ``generate(prompt)``
            # signature keeps working unchanged.
            options = {}
            if temperature is not None:
                options["temperature"] = temperature
            if json_mode is not None:
                options["json_mode"] = json_mode
            return provider.generate(prompt, **options), attempt, rate_limited
        except TransientProviderError as exc:
            rate_limited = rate_limited or exc.rate_limited
            exc.rate_limited = rate_limited
            exc.attempts = attempt
            if attempt >= policy.max_attempts:
                raise
            delay = _retry_delay(exc, attempt, policy)
            if delay is None:
                raise
            logger.info(
                "AI provider '%s' transient failure (attempt %d/%d): %s — retrying in %.2fs",
                provider.config.name or provider.adapter, attempt, policy.max_attempts, exc, delay,
            )
            time.sleep(delay)


# ── Circuit breaker ───────────────────────────────────────────────────────────

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    """Per-provider circuit breaker over the resilience layer.

    Keeps a small state machine per provider (keyed by display name, matching the
    metrics): ``CLOSED`` (normal) → ``OPEN`` (skip, use fallback) once consecutive
    *transient* failures reach the threshold → ``HALF_OPEN`` (allow a single probe)
    after a cooldown. A probe success closes it; a probe failure re-opens it with a
    longer cooldown. Non-transient failures (bad key / permanent 4xx) fail over
    cheaply once and do **not** trip the breaker.

    Process-global and thread-safe (like the metrics); ``clock`` is injectable so
    the transitions are deterministically testable without real time.
    """

    def __init__(self, *, threshold=None, base_cooldown=None, max_cooldown=None, clock=time.monotonic):
        self.threshold = threshold if threshold is not None else BREAKER_THRESHOLD
        self.base_cooldown = base_cooldown if base_cooldown is not None else BREAKER_BASE_COOLDOWN
        self.max_cooldown = max_cooldown if max_cooldown is not None else BREAKER_MAX_COOLDOWN
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}

    def _entry(self, name: str) -> dict:
        e = self._state.get(name)
        if e is None:
            e = {"state": CLOSED, "failures": 0, "opens": 0, "open_until": 0.0}
            self._state[name] = e
        return e

    def allow(self, name: str) -> bool:
        """True if a request may go to ``name`` now. An OPEN breaker whose cooldown
        has elapsed transitions to HALF_OPEN and grants a single probe."""
        with self._lock:
            e = self._entry(name)
            if e["state"] == CLOSED:
                return True
            if e["state"] == OPEN and self._clock() >= e["open_until"]:
                e["state"] = HALF_OPEN  # grant exactly one probe
                return True
            # OPEN (still cooling) or HALF_OPEN (a probe is already in flight).
            return False

    def record_success(self, name: str) -> None:
        with self._lock:
            self._state[name] = {"state": CLOSED, "failures": 0, "opens": 0, "open_until": 0.0}

    def record_failure(self, name: str, *, transient: bool) -> None:
        with self._lock:
            e = self._entry(name)
            if e["state"] == HALF_OPEN:
                self._trip(e)  # the probe failed → back to OPEN (longer cooldown)
                return
            if transient:
                e["failures"] += 1
                if e["failures"] >= self.threshold:
                    self._trip(e)
            # non-transient in CLOSED: cheap failover, leave the counter untouched.

    def _trip(self, e: dict) -> None:
        e["opens"] += 1
        cooldown = min(self.base_cooldown * (2 ** (e["opens"] - 1)), self.max_cooldown)
        e["state"] = OPEN
        e["open_until"] = self._clock() + cooldown
        e["failures"] = 0

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            now = self._clock()
            return {
                name: {
                    "state": e["state"],
                    "opens": e["opens"],
                    "cooldown_remaining_s": max(0.0, round(e["open_until"] - now, 1)) if e["state"] == OPEN else 0.0,
                }
                for name, e in self._state.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


# Process-global breaker used by the labeling path.
_breaker = CircuitBreaker()


def get_breaker() -> CircuitBreaker:
    return _breaker


def breaker_snapshot() -> dict[str, dict]:
    return _breaker.snapshot()


def reset_breaker() -> None:
    _breaker.reset()


def generate_with_chain(
    prompt: str,
    chain: list[ProviderRuntimeConfig],
    *,
    policy: RetryPolicy | None = None,
    parse=None,
    temperature: float | None = None,
    json_mode: bool | None = None,
) -> tuple[object, str] | None:
    """Run one prompt through an ordered provider chain and return ``(value, label)``.

    The single resilience path shared by every AI feature: per-provider retries on
    transient failures, circuit-breaker skipping of unhealthy providers, metrics for
    each invocation, and failover to the next provider on any
    :class:`RecoverableProviderError`.

    ``parse`` is an optional callable applied to the model's raw text. It is run
    *inside* the per-provider try, so a provider that answers with unusable output
    is treated as a failed provider and the chain fails over — the behavior column
    labeling has always had. Without it, the raw text is returned.

    Returns ``None`` when the whole chain is exhausted; callers decide what
    degrading gracefully means for them (labeling falls back to rule-based names;
    insights report that no provider was reachable). Never raises for a provider
    failure, so no caller has to defend against one.
    """
    if not chain:
        return None
    policy = policy or DEFAULT_RETRY_POLICY

    def _failover(label: str, adapter: str, started: float, attempts: int,
                  rate_limited: bool, message: str) -> None:
        ai_metrics.record(
            label, adapter, latency_ms=(time.perf_counter() - started) * 1000,
            outcome="failover", attempts=attempts, rate_limited=rate_limited,
        )
        _record_provider_error(message)

    for cfg in chain:
        label = cfg.name or cfg.adapter

        # Circuit breaker: an unhealthy provider is skipped straight to the fallback
        # (no retry budget spent) until its cooldown elapses and a probe is allowed.
        if not _breaker.allow(label):
            ai_metrics.record(label, cfg.adapter, latency_ms=0.0, outcome="skipped", attempts=0)
            _record_provider_error(f"{label}: circuit breaker open — skipping to fallback")
            continue

        started = time.perf_counter()
        try:
            provider = get_provider(cfg)
            text, attempts, rate_limited = _generate_with_retries(
                provider, prompt, policy, temperature=temperature, json_mode=json_mode,
            )
            value = parse(text) if parse is not None else text
        except TransientProviderError as exc:
            _failover(label, cfg.adapter, started,
                      getattr(exc, "attempts", policy.max_attempts), exc.rate_limited, str(exc))
            _breaker.record_failure(label, transient=True)
            continue
        except RecoverableProviderError as exc:
            _failover(label, cfg.adapter, started, 1, False, str(exc))
            _breaker.record_failure(label, transient=False)
            continue
        except (ValueError, TypeError, KeyError) as exc:
            # Unknown adapter, or output ``parse`` could not use — try the next provider.
            _failover(label, cfg.adapter, started, 1, False,
                      f"{label} returned output that could not be parsed: {exc}")
            _breaker.record_failure(label, transient=False)
            continue

        ai_metrics.record(
            label, cfg.adapter, latency_ms=(time.perf_counter() - started) * 1000,
            outcome="success", attempts=attempts, rate_limited=rate_limited,
        )
        _breaker.record_success(label)
        return value, label

    return None


# Columns classified per provider request. Bounds the JSON response so a wide
# schema cannot overflow a model's context and return truncated (unparseable)
# output. Small enough for a modest local-model context, large enough that a
# typical schema is one or two round-trips.
_CLASSIFY_CHUNK = 30


def classify_columns(
    column_keys: list[str],
    schema: dict | None,
    chain: list[ProviderRuntimeConfig],
    policy: RetryPolicy | None = None,
) -> dict[str, dict]:
    """Classify columns using an ordered provider chain, with per-provider retries.

    Thin wrapper over :func:`generate_with_chain` — the resilience behavior lives
    there. If the whole chain is exhausted, degrades to the rule-based normalized
    name (honest ``provider=None`` provenance) — never raises, so labeling always
    yields a result.

    Returns ``{col_key: {"term": str, "provider": str | None}}``.
    """
    _clear_provider_error()
    if not column_keys:
        return {}
    if not chain:
        return {col: _fallback_result(col) for col in column_keys}

    # Classify in chunks. One prompt for every column produces a single JSON
    # object whose size grows with the schema width; on a wide schema (e.g.
    # AdventureWorks' 465 columns) that response overflows a local model's
    # context and comes back truncated — invalid JSON — so the *whole* chain
    # reads as failed and every column silently drops to rule-based. Bounding the
    # request keeps each response parseable regardless of schema width, and a
    # chunk that still fails degrades only its own columns, not all of them.
    results: dict[str, dict] = {}
    for start in range(0, len(column_keys), _CLASSIFY_CHUNK):
        chunk = column_keys[start:start + _CLASSIFY_CHUNK]
        outcome = generate_with_chain(
            _build_classification_prompt(chunk, schema), chain,
            policy=policy, parse=lambda text: json.loads(_extract_json_object(text)),
        )
        if outcome is None:
            results.update({col: _fallback_result(col) for col in chunk})
        else:
            parsed, label = outcome
            results.update({col: _as_result(_match_value(parsed, col), col, label) for col in chunk})
    return results
