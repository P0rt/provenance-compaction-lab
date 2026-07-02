"""Gate policies across the four classes of the taxonomy.

Every consumer applies its own policy at the moment it's about to act
(Part 3: "the gate is per-consumer, not global"). The classes:

1. score gates            — thresholds on base axes; never consult reconstruction.
                            Must agree 100% between ground_truth and the structural
                            arms *by construction* (running min is lossless).
2. reconstruction-coupled — consult the reconstruction scalar; where
                            structural_min and structural_perhop diverge.
3. lineage gates          — both styles on purpose; the direction of error is
                            the finding:
                            * blocklist / default-allow → lossy compaction
                              forgets taints → false-PROCEEDS
                            * allowlist / default-deny → the proof gets folded
                              away when W > K → false-STOPS
4. irreversible gates     — same logic, flagged irreversible=True (payment,
                            send). False-proceeds here are the headline metric.

Lineage gates evaluate in one of three modes:
* ``blind``     — decide on what survived compaction (the default)
* ``degrade``   — if detail is missing, treat the value as untrusted (block)
* ``rehydrate`` — fetch folded hops from the append-only log, then decide
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .axes import CAPABILITY, FRESHNESS, TOOL_INTEGRITY, VERIFICATION
from .compaction import GateView
from .lineage import Hop, HopLog

SCORE = "score"
RECONSTRUCTION_COUPLED = "reconstruction"
LINEAGE_BLOCKLIST = "lineage_blocklist"
LINEAGE_ALLOWLIST = "lineage_allowlist"

MODES: tuple[str, ...] = ("blind", "degrade", "rehydrate")


@dataclass(frozen=True)
class GateDecision:
    proceed: bool
    lookups: int = 0
    bytes_read: int = 0


class Policy(ABC):
    name: str
    gate_class: str
    irreversible: bool

    #: whether this policy inspects lineage (and thus has degrade/rehydrate modes)
    lineage_sensitive: bool = False

    @abstractmethod
    def evaluate(
        self, view: GateView, mode: str = "blind", hop_log: HopLog | None = None
    ) -> GateDecision: ...


class ScoreGate(Policy):
    """proceed iff every thresholded base axis clears its floor.

    Does NOT consult ``reconstruction`` — agreement with ground truth for the
    structural arms is guaranteed by construction, not a discovery."""

    gate_class = SCORE

    def __init__(
        self, name: str, thresholds: dict[str, float], irreversible: bool
    ) -> None:
        self.name = name
        self.thresholds = thresholds
        self.irreversible = irreversible

    def evaluate(
        self, view: GateView, mode: str = "blind", hop_log: HopLog | None = None
    ) -> GateDecision:
        ok = all(view.scores[axis] >= floor for axis, floor in self.thresholds.items())
        return GateDecision(proceed=ok)


class MinFloorGate(Policy):
    """proceed iff min(all five axes) >= floor — reconstruction included."""

    gate_class = RECONSTRUCTION_COUPLED

    def __init__(self, name: str, floor: float, irreversible: bool) -> None:
        self.name = name
        self.floor = floor
        self.irreversible = irreversible

    def evaluate(
        self, view: GateView, mode: str = "blind", hop_log: HopLog | None = None
    ) -> GateDecision:
        return GateDecision(proceed=min(view.effective_scores().values()) >= self.floor)


class DiscountedGate(Policy):
    """proceed iff axis * reconstruction >= threshold — thresholds effectively
    tighten as reconstruction degrades."""

    gate_class = RECONSTRUCTION_COUPLED

    def __init__(
        self, name: str, thresholds: dict[str, float], irreversible: bool
    ) -> None:
        self.name = name
        self.thresholds = thresholds
        self.irreversible = irreversible

    def evaluate(
        self, view: GateView, mode: str = "blind", hop_log: HopLog | None = None
    ) -> GateDecision:
        r = view.reconstruction_scalar()
        ok = all(
            view.scores[axis] * r >= floor for axis, floor in self.thresholds.items()
        )
        return GateDecision(proceed=ok)


def _matches(taint: str, prefixes: tuple[str, ...]) -> bool:
    return any(taint.startswith(p) for p in prefixes)


class BlocklistGate(Policy):
    """blocklist / default-allow: block iff any taint matching the patterns is
    in ``tainted_by``. Lossy compaction *forgets* taints → expected
    false-proceeds."""

    gate_class = LINEAGE_BLOCKLIST
    lineage_sensitive = True

    def __init__(
        self, name: str, block_prefixes: tuple[str, ...], irreversible: bool
    ) -> None:
        self.name = name
        self.block_prefixes = block_prefixes
        self.irreversible = irreversible

    def evaluate(
        self, view: GateView, mode: str = "blind", hop_log: HopLog | None = None
    ) -> GateDecision:
        taints = set(view.tainted_by)
        lookups = 0
        bytes_read = 0
        detail_missing = view.folded is not None and view.folded.n_taints_folded > 0
        if mode == "degrade" and detail_missing:
            # degrade-to-untrusted: taints were dropped, refuse to act
            return GateDecision(proceed=False)
        if mode == "rehydrate" and detail_missing and hop_log is not None:
            assert view.folded is not None
            hops, bytes_read = hop_log.fetch(view.folded.folded_hop_ids)
            lookups = len(view.folded.folded_hop_ids)
            for hop in hops:
                taints |= set(hop.taints_added)
        blocked = any(_matches(t, self.block_prefixes) for t in taints)
        return GateDecision(proceed=not blocked, lookups=lookups, bytes_read=bytes_read)


class AllowlistGate(Policy):
    """allowlist / default-deny: proceed iff lineage PROVES no forbidden hop
    within the last W hops. The proof gets folded away when W > K → expected
    false-stops."""

    gate_class = LINEAGE_ALLOWLIST
    lineage_sensitive = True

    def __init__(
        self,
        name: str,
        window: int,
        forbidden_prefixes: tuple[str, ...],
        irreversible: bool,
    ) -> None:
        self.name = name
        self.window = window
        self.forbidden_prefixes = forbidden_prefixes
        self.irreversible = irreversible

    def _window_clean(self, hops: list[Hop]) -> bool:
        return not any(
            _matches(t, self.forbidden_prefixes)
            for hop in hops[-self.window :]
            for t in hop.taints_added
        )

    def evaluate(
        self, view: GateView, mode: str = "blind", hop_log: HopLog | None = None
    ) -> GateDecision:
        hops = list(view.visible_hops)
        if len(hops) >= self.window:
            return GateDecision(proceed=self._window_clean(hops))
        if not view.history_truncated:
            # the value's entire history is shorter than the window — provable
            return GateDecision(proceed=self._window_clean(hops))
        if mode == "rehydrate" and view.folded is not None and hop_log is not None:
            fetched, bytes_read = hop_log.fetch(view.folded.folded_hop_ids)
            merged = {h.hop_id: h for h in [*fetched, *hops]}
            full = sorted(merged.values(), key=lambda h: (h.step, h.hop_id))
            return GateDecision(
                proceed=self._window_clean(full),
                lookups=len(view.folded.folded_hop_ids),
                bytes_read=bytes_read,
            )
        # blind / degrade: the proof was folded away (or is a prose blob) —
        # default-deny
        return GateDecision(proceed=False)


def default_policies(allowlist_window: int = 8) -> tuple[Policy, ...]:
    """The concrete gate roster, covering all four classes. Default W=8 > K=5
    deliberately, so allowlist starvation shows."""
    return (
        # 1. score gates (sanity class — lossless by construction)
        ScoreGate(
            "summarize_freshness_strict",
            {FRESHNESS: 0.6, VERIFICATION: 0.5},
            irreversible=False,
        ),
        ScoreGate(
            "draft_reply_capability",
            {CAPABILITY: 0.5, TOOL_INTEGRITY: 0.5},
            irreversible=False,
        ),
        ScoreGate(
            "send_email_fresh_verified",
            {FRESHNESS: 0.5, VERIFICATION: 0.6},
            irreversible=True,
        ),
        # 2. reconstruction-coupled gates
        MinFloorGate("archive_all_axes_floor", floor=0.5, irreversible=False),
        DiscountedGate(
            "publish_discounted_thresholds",
            {FRESHNESS: 0.55, VERIFICATION: 0.40},
            irreversible=False,
        ),
        # 3. lineage gates — both styles on purpose
        BlocklistGate(
            "summarize_no_unverified_taint",
            ("taint:unverified_web:",),
            irreversible=False,
        ),
        AllowlistGate(
            "audit_requires_clean_window",
            allowlist_window,
            ("taint:fallback_model:",),
            irreversible=False,
        ),
        # 4. irreversible-action gates (payment, send)
        BlocklistGate(
            "payment_no_untrusted_taint",
            ("taint:unverified_web:", "taint:tool_flaky:"),
            irreversible=True,
        ),
        AllowlistGate(
            "send_wire_clean_window",
            allowlist_window,
            ("taint:fallback_model:", "taint:tool_flaky:"),
            irreversible=True,
        ),
    )
