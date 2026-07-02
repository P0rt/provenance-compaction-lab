"""Provenance vectors: per-axis trust scores with min-merge semantics.

Terminology follows the blog series:

* Part 3 — "Trust Isn't a Scalar": trust is a per-axis vector, merge is
  element-wise min ("an output is only as fresh as its stalest input").
* Part 4 — "Your Provenance Vector Dies at the Storage Boundary": axis
  scores compress losslessly (a running min per axis is constant-size);
  compression itself is a degradation source, tracked as the
  ``reconstruction`` axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

FRESHNESS = "freshness"
CAPABILITY = "capability"
TOOL_INTEGRITY = "tool_integrity"
VERIFICATION = "verification"
RECONSTRUCTION = "reconstruction"

#: Canonical axis names, in canonical order. Do not rename — they match the series.
AXES: tuple[str, ...] = (
    FRESHNESS,
    CAPABILITY,
    TOOL_INTEGRITY,
    VERIFICATION,
    RECONSTRUCTION,
)

#: The four axes degraded by trajectory events. ``reconstruction`` is only
#: ever degraded by the storage boundary itself.
BASE_AXES: tuple[str, ...] = (FRESHNESS, CAPABILITY, TOOL_INTEGRITY, VERIFICATION)


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def pristine_scores() -> dict[str, float]:
    return {axis: 1.0 for axis in AXES}


@dataclass
class ProvenanceVector:
    """Mapping axis → score, each a float in [0.0, 1.0]; 1.0 is pristine."""

    scores: dict[str, float] = field(default_factory=pristine_scores)

    def degrade(self, axis: str, factor: float) -> None:
        """Multiply one axis down by ``factor`` (a degradation event)."""
        self.scores[axis] = clip01(self.scores[axis] * factor)

    def floor(self, axis: str, value: float) -> None:
        """Fold ``value`` into one axis with min (running-min semantics)."""
        self.scores[axis] = min(self.scores[axis], clip01(value))

    def copy(self) -> "ProvenanceVector":
        return ProvenanceVector(scores=dict(self.scores))


def merge(vectors: Iterable[ProvenanceVector]) -> ProvenanceVector:
    """Merge = element-wise min across inputs (Part 3)."""
    vs = list(vectors)
    if not vs:
        raise ValueError("merge() needs at least one input vector")
    return ProvenanceVector(
        scores={axis: min(v.scores[axis] for v in vs) for axis in AXES}
    )
