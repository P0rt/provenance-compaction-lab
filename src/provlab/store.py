"""``provlab.store`` — SQLite reference implementation of the hop-log pattern.

The vendorable artifact behind the series' rule 4: **append-only hop log
plus rehydrate-on-demand**. Two tables on stdlib ``sqlite3``:

* ``hops``   — append-only cold storage: every full hop, forever.
* ``values`` — the compacted hot state per value: running-min scores, the
               visible (post-fold) taint set, the last-K hop ids, and the
               fold aggregates.

Three gate read modes, each behind one function:

* :meth:`ProvenanceStore.read_blind`      — decide on what survived compaction.
* :meth:`ProvenanceStore.read_degraded`   — same state, plus ``untrusted=True``
  whenever detail was folded away; a policy treats such a value as untrusted.
* :meth:`ProvenanceStore.read_rehydrated` — fetch the folded hops back from
  cold storage and reconstruct the full taint set and hop window, with the
  cost (lookups, bytes) reported.

This module is deliberately standalone: it depends only on ``sqlite3`` and
the ``Hop``/``FoldedPrefix`` dataclasses, so it can be vendored into an agent
runtime next to your own compaction code.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from .lineage import FoldedPrefix, Hop

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hops (
    hop_id   TEXT PRIMARY KEY,
    step     INTEGER NOT NULL,
    hop_json TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS values_state (
    value_id             TEXT PRIMARY KEY,
    scores_json          TEXT NOT NULL,
    tainted_by_json      TEXT NOT NULL,
    visible_hop_ids_json TEXT NOT NULL,
    folded_hop_ids_json  TEXT NOT NULL,
    n_hops_folded        INTEGER NOT NULL,
    n_taints_folded      INTEGER NOT NULL
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class StoredView:
    """What a gate sees for one value under one read mode."""

    value_id: str
    scores: dict[str, float]
    tainted_by: frozenset[str]
    hops: tuple[Hop, ...]
    n_hops_folded: int
    n_taints_folded: int
    #: degrade mode only: True when detail the gate may need was folded away
    untrusted: bool = False
    #: rehydrate mode only: cold-storage cost of this read
    lookups: int = 0
    bytes_read: int = 0

    @property
    def history_truncated(self) -> bool:
        return self.n_hops_folded > 0


class ProvenanceStore:
    """Append-only hop log + compacted per-value state, on stdlib sqlite3."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- writes --------------------------------------------------------------

    def append_hop(self, hop: Hop) -> None:
        """Append to cold storage. The log is append-only: re-appending an
        existing hop_id with different content is an error."""
        line = json.dumps(hop.to_json_obj(), sort_keys=True, separators=(",", ":"))
        try:
            self._conn.execute(
                "INSERT INTO hops (hop_id, step, hop_json) VALUES (?, ?, ?)",
                (hop.hop_id, hop.step, line),
            )
        except sqlite3.IntegrityError as err:
            row = self._conn.execute(
                "SELECT hop_json FROM hops WHERE hop_id = ?", (hop.hop_id,)
            ).fetchone()
            if row is not None and row[0] == line:
                return  # idempotent re-append of the identical hop is fine
            raise ValueError(
                f"hop log is append-only: {hop.hop_id} already stored with "
                f"different content"
            ) from err
        self._conn.commit()

    def save_value(
        self,
        value_id: str,
        scores: dict[str, float],
        tainted_by: frozenset[str] | set[str],
        visible_hops: list[Hop] | tuple[Hop, ...],
        folded: FoldedPrefix | None = None,
    ) -> None:
        """Persist a value's compacted state (running-min scores, post-fold
        taints, last-K hops, fold aggregates). Overwrites the previous state
        for the value — that is the point of compaction; the hop log is where
        nothing is lost."""
        self._conn.execute(
            "INSERT OR REPLACE INTO values_state VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                value_id,
                json.dumps(dict(sorted(scores.items()))),
                json.dumps(sorted(tainted_by)),
                json.dumps([h.hop_id for h in visible_hops]),
                json.dumps(list(folded.folded_hop_ids) if folded else []),
                folded.n_hops_folded if folded else 0,
                folded.n_taints_folded if folded else 0,
            ),
        )
        self._conn.commit()

    # -- the three gate read modes --------------------------------------------

    def read_blind(self, value_id: str) -> StoredView:
        """Decide on what survived compaction. Cheap; a default-allow gate
        reading this will false-proceed on folded taints."""
        state = self._load_state(value_id)
        return StoredView(
            value_id=value_id,
            scores=state.scores,
            tainted_by=state.tainted_by,
            hops=self._fetch_hops(state.visible_hop_ids)[0],
            n_hops_folded=state.n_hops_folded,
            n_taints_folded=state.n_taints_folded,
        )

    def read_degraded(self, value_id: str) -> StoredView:
        """Degrade-to-untrusted: same stored state, but flagged ``untrusted``
        whenever detail was folded away. Costs nothing; converts dangerous
        false-proceeds into expensive false-stops."""
        view = self.read_blind(value_id)
        missing = view.n_taints_folded > 0 or view.n_hops_folded > 0
        return StoredView(
            value_id=view.value_id,
            scores=view.scores,
            tainted_by=view.tainted_by,
            hops=view.hops,
            n_hops_folded=view.n_hops_folded,
            n_taints_folded=view.n_taints_folded,
            untrusted=missing,
        )

    def read_rehydrated(self, value_id: str) -> StoredView:
        """Rehydrate-on-demand: fetch the folded hops from the append-only
        log and reconstruct the full taint set and the full ordered hop
        window. Recovers the uncompacted decision exactly, at a measured
        cold-storage cost."""
        state = self._load_state(value_id)
        visible, _ = self._fetch_hops(state.visible_hop_ids)
        folded, bytes_read = self._fetch_hops(state.folded_hop_ids)
        merged = {h.hop_id: h for h in (*folded, *visible)}
        full = tuple(sorted(merged.values(), key=lambda h: (h.step, h.hop_id)))
        taints = set(state.tainted_by)
        for hop in folded:
            taints |= set(hop.taints_added)
        return StoredView(
            value_id=value_id,
            scores=state.scores,
            tainted_by=frozenset(taints),
            hops=full,
            n_hops_folded=0,  # nothing is folded any more from the gate's view
            n_taints_folded=0,
            lookups=len(state.folded_hop_ids),
            bytes_read=bytes_read,
        )

    # -- plumbing --------------------------------------------------------------

    @dataclass(frozen=True)
    class _State:
        scores: dict[str, float]
        tainted_by: frozenset[str]
        visible_hop_ids: list[str]
        folded_hop_ids: list[str]
        n_hops_folded: int
        n_taints_folded: int

    def _load_state(self, value_id: str) -> "ProvenanceStore._State":
        row = self._conn.execute(
            "SELECT scores_json, tainted_by_json, visible_hop_ids_json, "
            "folded_hop_ids_json, n_hops_folded, n_taints_folded "
            "FROM values_state WHERE value_id = ?",
            (value_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no stored state for value {value_id!r}")
        return ProvenanceStore._State(
            scores={str(k): float(v) for k, v in json.loads(row[0]).items()},
            tainted_by=frozenset(str(t) for t in json.loads(row[1])),
            visible_hop_ids=[str(h) for h in json.loads(row[2])],
            folded_hop_ids=[str(h) for h in json.loads(row[3])],
            n_hops_folded=int(row[4]),
            n_taints_folded=int(row[5]),
        )

    def _fetch_hops(self, hop_ids: list[str]) -> tuple[tuple[Hop, ...], int]:
        hops: list[Hop] = []
        bytes_read = 0
        for hop_id in hop_ids:
            row = self._conn.execute(
                "SELECT hop_json FROM hops WHERE hop_id = ?", (hop_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"hop {hop_id!r} missing from the append-only log")
            hops.append(Hop.from_json_obj(json.loads(row[0])))
            bytes_read += len(row[0])
        return tuple(hops), bytes_read

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ProvenanceStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
