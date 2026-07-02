"""The prose channel: summarize provenance into prose, then extract it back.

The prose arm's storage boundary is a lossy summarize→extract round trip.
``MockProseChannel`` simulates it as a noisy channel (default, no API key
needed); ``AnthropicProseChannel`` runs the real two-call LLM pipeline via
the ``anthropic`` SDK when ``--llm anthropic`` is passed and
``ANTHROPIC_API_KEY`` is set.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .axes import AXES, clip01

#: taint families the mock channel may fabricate (precision < 1.0)
TAINT_FAMILIES: tuple[str, ...] = (
    "unverified_web",
    "fallback_model",
    "tool_flaky",
    "stale_cache",
)

#: worst-case scores used when extraction output cannot be parsed
WORST_CASE_SCORES: dict[str, float] = {axis: 0.0 for axis in AXES}


@dataclass(frozen=True)
class ProseExtraction:
    """Result of one summarize→extract round trip for one value."""

    scores: dict[str, float]
    taints: frozenset[str]
    blob: str
    parse_failed: bool
    n_true_taints: int
    n_kept: int
    n_fabricated: int


class ProseChannel(Protocol):
    def compress(
        self, *, scores: dict[str, float], taints: frozenset[str], step: int
    ) -> ProseExtraction: ...


class MockProseChannel:
    """Simulated noisy summarize→extract channel.

    * gaussian noise on each axis score (sigma, clipped to [0, 1])
    * taint recall: each true taint survives with p = ``taint_recall``
    * taint precision: fabricated taints are added so that roughly
      ``1 - taint_precision`` of reported taints are invented
    * optional parse failures (worst-case scores), matching the defensive
      fallback of the real LLM channel
    """

    def __init__(
        self,
        rng: np.random.Generator,
        sigma: float = 0.08,
        taint_recall: float = 0.6,
        taint_precision: float = 0.9,
        parse_failure_rate: float = 0.0,
    ) -> None:
        self._rng = rng
        self.sigma = sigma
        self.taint_recall = taint_recall
        self.taint_precision = taint_precision
        self.parse_failure_rate = parse_failure_rate

    def compress(
        self, *, scores: dict[str, float], taints: frozenset[str], step: int
    ) -> ProseExtraction:
        rng = self._rng
        n_true = len(taints)
        if self.parse_failure_rate > 0 and float(rng.random()) < self.parse_failure_rate:
            return ProseExtraction(
                scores=dict(WORST_CASE_SCORES),
                taints=frozenset(),
                blob=f"[mock summary @step {step}: unparseable]",
                parse_failed=True,
                n_true_taints=n_true,
                n_kept=0,
                n_fabricated=0,
            )
        noisy = {
            axis: clip01(scores[axis] + float(rng.normal(0.0, self.sigma)))
            for axis in AXES
        }
        kept = {t for t in sorted(taints) if float(rng.random()) < self.taint_recall}
        # fabricated / (kept + fabricated) ≈ 1 - precision (in expectation)
        fab_ratio = (1.0 - self.taint_precision) / self.taint_precision
        n_fab = int(rng.binomial(len(kept), min(fab_ratio, 1.0))) if kept else 0
        fabricated = {
            f"taint:{TAINT_FAMILIES[int(rng.integers(len(TAINT_FAMILIES)))]}:{step}~fab{i}"
            for i in range(n_fab)
        }
        blob = (
            f"[mock summary @step {step}: {n_true} taints observed, "
            f"scores ~ {', '.join(f'{a}={noisy[a]:.2f}' for a in AXES)}]"
        )
        return ProseExtraction(
            scores=noisy,
            taints=frozenset(kept | fabricated),
            blob=blob,
            parse_failed=False,
            n_true_taints=n_true,
            n_kept=len(kept),
            n_fabricated=len(fabricated),
        )


_SUMMARIZE_PROMPT = """\
You are compacting an AI agent's working memory. Summarize the state below in
AT MOST 150 words of prose. You MUST preserve the provenance information:
the trust score of each of the five axes and every taint id, as faithfully
as prose allows.

Axis scores (1.0 = pristine):
{scores}

Taints attached to this value:
{taints}
"""

_EXTRACT_PROMPT = """\
Below is a prose summary of an AI agent's working memory. Extract the
provenance information from it. Respond with STRICT JSON only — no prose,
no markdown fences — matching exactly this schema:

{{"scores": {{"freshness": float, "capability": float, "tool_integrity": float,
"verification": float, "reconstruction": float}}, "taints": [string, ...]}}

Summary:
{summary}
"""


class AnthropicProseChannel:
    """Real LLM channel: summarize into ≤150 words, then extract strict JSON.

    Parses defensively; falls back to worst-case scores on parse failure
    (counted as a metric).
    """

    def __init__(self, model: str = "claude-haiku-4-5", max_attempts: int = 5) -> None:
        import anthropic

        self._client = anthropic.Anthropic(timeout=60.0)
        self._anthropic = anthropic
        self.model = model
        self.max_attempts = max_attempts

    def _complete(self, prompt: str, max_tokens: int) -> str:
        """One API call with retry — a long run must survive transient network
        failures; the SDK's built-in retries alone have killed 40-minute runs."""
        import time

        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                parts: list[str] = []
                for block in response.content:
                    if block.type == "text":
                        parts.append(block.text)
                return "".join(parts)
            except (
                self._anthropic.APITimeoutError,
                self._anthropic.APIConnectionError,
                self._anthropic.RateLimitError,
                self._anthropic.InternalServerError,
            ) as err:
                last_error = err
                time.sleep(min(2.0**attempt, 30.0))
        raise RuntimeError("prose channel exhausted retries") from last_error

    def compress(
        self, *, scores: dict[str, float], taints: frozenset[str], step: int
    ) -> ProseExtraction:
        n_true = len(taints)
        score_lines = "\n".join(f"- {a}: {scores[a]:.3f}" for a in AXES)
        taint_lines = "\n".join(f"- {t}" for t in sorted(taints)) or "(none)"
        try:
            blob = self._complete(
                _SUMMARIZE_PROMPT.format(scores=score_lines, taints=taint_lines),
                max_tokens=300,
            )
            raw = self._complete(_EXTRACT_PROMPT.format(summary=blob), max_tokens=1000)
        except RuntimeError:
            # the round trip is unrecoverable — degrade to worst case rather
            # than killing the run; counted as a parse failure
            return ProseExtraction(
                scores=dict(WORST_CASE_SCORES),
                taints=frozenset(),
                blob=f"[channel failure @step {step}]",
                parse_failed=True,
                n_true_taints=n_true,
                n_kept=0,
                n_fabricated=0,
            )
        parsed = _parse_extraction(raw)
        if parsed is None:
            return ProseExtraction(
                scores=dict(WORST_CASE_SCORES),
                taints=frozenset(),
                blob=blob,
                parse_failed=True,
                n_true_taints=n_true,
                n_kept=0,
                n_fabricated=0,
            )
        out_scores, out_taints = parsed
        return ProseExtraction(
            scores=out_scores,
            taints=out_taints,
            blob=blob,
            parse_failed=False,
            n_true_taints=n_true,
            n_kept=len(out_taints & taints),
            n_fabricated=len(out_taints - taints),
        )


def _parse_extraction(raw: str) -> tuple[dict[str, float], frozenset[str]] | None:
    """Defensive parse of the extraction call's output."""
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    scores_obj = obj.get("scores")
    taints_obj = obj.get("taints", [])
    if not isinstance(scores_obj, dict) or not isinstance(taints_obj, list):
        return None
    try:
        scores = {axis: clip01(float(scores_obj[axis])) for axis in AXES}
    except (KeyError, TypeError, ValueError):
        return None
    taints = frozenset(str(t) for t in taints_obj)
    return scores, taints
