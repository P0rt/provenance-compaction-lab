"""Lineage: append-only hop lists, taint sets, and the cold-storage hop log.

Lineage compresses *lossily* (Part 4): compaction keeps the last K hops and
replaces the folded prefix with aggregate counts. Folded taint ids are
dropped — only the count survives. ``FoldedPrefix.folded_hop_ids`` is the
pointer into the append-only hop log (cold storage) that ``--rehydrate``
follows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterable


@dataclass(frozen=True)
class Hop:
    hop_id: str
    step: int
    op: str
    #: multiplicative factors applied to axes at this hop (axis → factor)
    axis_deltas: dict[str, float]
    taints_added: frozenset[str]
    parent_hop_ids: tuple[str, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "hop_id": self.hop_id,
            "step": self.step,
            "op": self.op,
            "axis_deltas": dict(sorted(self.axis_deltas.items())),
            "taints_added": sorted(self.taints_added),
            "parent_hop_ids": list(self.parent_hop_ids),
        }

    @staticmethod
    def from_json_obj(obj: dict[str, object]) -> "Hop":
        deltas = obj["axis_deltas"]
        taints = obj["taints_added"]
        parents = obj["parent_hop_ids"]
        assert isinstance(deltas, dict)
        assert isinstance(taints, list)
        assert isinstance(parents, list)
        return Hop(
            hop_id=str(obj["hop_id"]),
            step=int(str(obj["step"])),
            op=str(obj["op"]),
            axis_deltas={str(k): float(v) for k, v in deltas.items()},
            taints_added=frozenset(str(t) for t in taints),
            parent_hop_ids=tuple(str(p) for p in parents),
        )


@dataclass
class FoldedPrefix:
    """Aggregate that replaces a folded lineage prefix after compaction.

    This is the deliberate lossy step: taint ids attached in folded hops are
    dropped from ``tainted_by`` — only ``n_taints_folded`` survives.
    """

    n_hops_folded: int = 0
    op_counts: dict[str, int] = field(default_factory=dict)
    n_taints_folded: int = 0
    #: pointers into the append-only hop log (cold storage)
    folded_hop_ids: list[str] = field(default_factory=list)

    def absorb(self, hop: Hop) -> None:
        self.n_hops_folded += 1
        self.op_counts[hop.op] = self.op_counts.get(hop.op, 0) + 1
        self.n_taints_folded += len(hop.taints_added)
        self.folded_hop_ids.append(hop.hop_id)

    def combined_with(self, other: "FoldedPrefix") -> "FoldedPrefix":
        counts = dict(self.op_counts)
        for op, n in other.op_counts.items():
            counts[op] = counts.get(op, 0) + n
        ids = list(dict.fromkeys([*self.folded_hop_ids, *other.folded_hop_ids]))
        return FoldedPrefix(
            n_hops_folded=self.n_hops_folded + other.n_hops_folded,
            op_counts=counts,
            n_taints_folded=self.n_taints_folded + other.n_taints_folded,
            folded_hop_ids=ids,
        )


@dataclass
class Lineage:
    hops: list[Hop] = field(default_factory=list)
    folded: FoldedPrefix | None = None
    #: prose arm only: after compaction, lineage is just the prose blob
    prose_blob: str | None = None

    @property
    def truncated(self) -> bool:
        """True when part of this value's history is no longer inspectable."""
        return self.folded is not None or self.prose_blob is not None


def merge_lineages(lineages: Iterable[Lineage]) -> Lineage:
    """Combine input lineages for a MERGE: union of visible hops (deduped by
    hop_id, ordered by (step, hop_id)), folded prefixes combined by summing
    their aggregates. The MERGE hop itself is appended by the caller."""
    seen: dict[str, Hop] = {}
    folded: FoldedPrefix | None = None
    blobs: list[str] = []
    for lin in lineages:
        for hop in lin.hops:
            seen[hop.hop_id] = hop
        if lin.folded is not None:
            folded = lin.folded if folded is None else folded.combined_with(lin.folded)
        if lin.prose_blob is not None:
            blobs.append(lin.prose_blob)
    hops = sorted(seen.values(), key=lambda h: (h.step, h.hop_id))
    return Lineage(
        hops=hops,
        folded=FoldedPrefix(
            n_hops_folded=folded.n_hops_folded,
            op_counts=dict(folded.op_counts),
            n_taints_folded=folded.n_taints_folded,
            folded_hop_ids=list(folded.folded_hop_ids),
        )
        if folded is not None
        else None,
        prose_blob=" | ".join(blobs) if blobs else None,
    )


class HopLog:
    """Append-only JSONL log of every full hop (simulated cold storage).

    Structural arms write every hop here; a lineage gate running in
    ``rehydrate`` mode fetches folded hops back instead of deciding blind.
    Fetches report bytes read so the report can price rehydration.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lines: dict[str, str] = {}
        self._hops: dict[str, Hop] = {}
        self._fh: IO[str] | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")

    def append(self, hop: Hop) -> None:
        line = json.dumps(hop.to_json_obj(), sort_keys=True, separators=(",", ":"))
        self._lines[hop.hop_id] = line
        self._hops[hop.hop_id] = hop
        if self._fh is not None:
            self._fh.write(line + "\n")

    def fetch(self, hop_ids: Iterable[str]) -> tuple[list[Hop], int]:
        """Fetch hops from cold storage. Returns (hops, bytes_read)."""
        hops: list[Hop] = []
        bytes_read = 0
        for hop_id in hop_ids:
            hops.append(self._hops[hop_id])
            bytes_read += len(self._lines[hop_id]) + 1  # +1 for the newline
        return hops, bytes_read

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
