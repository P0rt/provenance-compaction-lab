"""Deterministic trajectory generator.

A trajectory is a sequence of ``steps`` operations sampled from
SOURCE_FETCH / CACHE_READ / LLM_TRANSFORM / TOOL_CALL / MERGE, with
degradation events attached per the profile probabilities. Every D steps a
DECISION point is emitted. Deterministic per seed via
``numpy.random.default_rng(seed)``.

The generator owns the value topology (which value is the working value,
which recent values a MERGE pulls in), so every arm sees the exact same
events and only differs in storage.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .axes import CAPABILITY, FRESHNESS, TOOL_INTEGRITY, VERIFICATION

SOURCE_FETCH = "SOURCE_FETCH"
CACHE_READ = "CACHE_READ"
LLM_TRANSFORM = "LLM_TRANSFORM"
TOOL_CALL = "TOOL_CALL"
MERGE = "MERGE"

OPS: tuple[str, ...] = (SOURCE_FETCH, CACHE_READ, LLM_TRANSFORM, TOOL_CALL, MERGE)
OP_WEIGHTS: tuple[float, ...] = (0.18, 0.22, 0.30, 0.22, 0.08)

# degradation magnitudes (multiplicative factors applied on a hit)
VERIFICATION_HIT = 0.45  # unverified web fetch
CAPABILITY_HIT = 0.65  # fallback model
TOOL_INTEGRITY_HIT = 0.65  # flaky tool
FRESHNESS_STALE = 0.70  # cache read beyond the staleness threshold
FRESHNESS_AGING = 0.95  # ordinary cache read still costs a little freshness

#: how many replaced working values stay mergeable
RECENTS_MAX = 6


@dataclass(frozen=True)
class Profile:
    p_unverified: float
    p_fallback: float
    p_flaky: float
    p_stale: float


@dataclass(frozen=True)
class Event:
    step: int


@dataclass(frozen=True)
class NewValue(Event):
    value_id: int
    unverified: bool


@dataclass(frozen=True)
class OpEvent(Event):
    """A non-merge op applied to the working value. ``axis`` is None for ops
    that happened to not degrade anything (they still add a lineage hop)."""

    value_id: int
    op: str
    axis: str | None
    factor: float
    taint: str | None


@dataclass(frozen=True)
class MergeEvent(Event):
    value_id: int  # the merged value (becomes the working value)
    input_ids: tuple[int, ...]


@dataclass(frozen=True)
class Retire(Event):
    value_id: int


@dataclass(frozen=True)
class DecisionPoint(Event):
    value_id: int


def generate_trajectory(
    seed: int, steps: int, decision_every: int, profile: Profile
) -> list[Event]:
    rng = np.random.default_rng(seed)
    events: list[Event] = []
    weights = np.asarray(OP_WEIGHTS)

    working = 0
    next_id = 1
    recents: list[int] = []
    events.append(NewValue(step=0, value_id=0, unverified=False))

    def replace_working(new_id: int, step: int) -> None:
        nonlocal working
        recents.append(working)
        if len(recents) > RECENTS_MAX:
            events.append(Retire(step=step, value_id=recents.pop(0)))
        working = new_id

    for step in range(1, steps + 1):
        op = OPS[int(rng.choice(len(OPS), p=weights))]
        if op == SOURCE_FETCH:
            unverified = float(rng.random()) < profile.p_unverified
            new_id = next_id
            next_id += 1
            events.append(NewValue(step=step, value_id=new_id, unverified=unverified))
            replace_working(new_id, step)
        elif op == CACHE_READ:
            stale = float(rng.random()) < profile.p_stale
            events.append(
                OpEvent(
                    step=step,
                    value_id=working,
                    op=CACHE_READ,
                    axis=FRESHNESS,
                    factor=FRESHNESS_STALE if stale else FRESHNESS_AGING,
                    taint=f"taint:stale_cache:{step}" if stale else None,
                )
            )
        elif op == LLM_TRANSFORM:
            fallback = float(rng.random()) < profile.p_fallback
            events.append(
                OpEvent(
                    step=step,
                    value_id=working,
                    op=LLM_TRANSFORM,
                    axis=CAPABILITY if fallback else None,
                    factor=CAPABILITY_HIT if fallback else 1.0,
                    taint=f"taint:fallback_model:{step}" if fallback else None,
                )
            )
        elif op == TOOL_CALL:
            flaky = float(rng.random()) < profile.p_flaky
            events.append(
                OpEvent(
                    step=step,
                    value_id=working,
                    op=TOOL_CALL,
                    axis=TOOL_INTEGRITY if flaky else None,
                    factor=TOOL_INTEGRITY_HIT if flaky else 1.0,
                    taint=f"taint:tool_flaky:{step}" if flaky else None,
                )
            )
        else:  # MERGE: combine the working value with 1–3 recent values
            if not recents:
                events.append(
                    OpEvent(
                        step=step,
                        value_id=working,
                        op=MERGE,
                        axis=None,
                        factor=1.0,
                        taint=None,
                    )
                )
            else:
                k = int(rng.integers(1, min(3, len(recents)) + 1))
                picked_idx = rng.choice(len(recents), size=k, replace=False)
                picked = tuple(recents[int(i)] for i in sorted(picked_idx))
                new_id = next_id
                next_id += 1
                inputs = (working, *picked)
                # emit the merge before replace_working so a Retire of an
                # evicted recent can never precede a merge that uses it
                events.append(MergeEvent(step=step, value_id=new_id, input_ids=inputs))
                replace_working(new_id, step)

        if step % decision_every == 0:
            events.append(DecisionPoint(step=step, value_id=working))

    return events


def unverified_fetch_deltas(unverified: bool) -> dict[str, float]:
    return {VERIFICATION: VERIFICATION_HIT} if unverified else {}
