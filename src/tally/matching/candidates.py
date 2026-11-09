"""Candidate generation with blocking.

Scoring every transaction against every open entry is O(n*m) fuzzy comparisons.
A real payment must agree on direction and (within the fee tolerance) on amount,
so entries are indexed by amount and each transaction only looks at a narrow
amount band. On the 2,000-transaction benchmark this cuts the pairs scored from
about 3.6 million to a few thousand.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from tally.matching.scoring import PairScore, ScoringConfig, score_pair
from tally.models import BankTransaction, EntryKind, LedgerEntry


@dataclass(frozen=True)
class Candidate:
    transaction_id: str
    entry_id: str
    pair: PairScore


class AmountIndex:
    """Open entries per direction, sorted by amount, for range lookups."""

    def __init__(self, entries: list[LedgerEntry]) -> None:
        by_kind: dict[EntryKind, list[LedgerEntry]] = defaultdict(list)
        for entry in entries:
            by_kind[entry.kind].append(entry)
        self._entries = {k: sorted(v, key=lambda e: e.amount) for k, v in by_kind.items()}
        self._amounts = {k: [e.amount for e in v] for k, v in self._entries.items()}

    def in_range(self, kind: EntryKind, low: Decimal, high: Decimal) -> list[LedgerEntry]:
        amounts = self._amounts.get(kind, [])
        lo = bisect.bisect_left(amounts, low)
        hi = bisect.bisect_right(amounts, high)
        return self._entries[kind][lo:hi]


def generate_candidates(
    transactions: list[BankTransaction], entries: list[LedgerEntry], cfg: ScoringConfig
) -> list[Candidate]:
    index = AmountIndex(entries)
    out: list[Candidate] = []
    for txn in transactions:
        paid = abs(txn.amount)
        # Tolerance is relative to the entry amount, which is close to `paid`;
        # widen the band slightly so the exact check in score_pair decides.
        band = cfg.fee_tolerance(paid * Decimal("1.02")) + Decimal("0.01")
        for entry in index.in_range(txn.direction, paid - band, paid + band):
            pair = score_pair(txn, entry, cfg)
            if pair is not None and pair.score >= cfg.min_candidate_score:
                out.append(Candidate(txn.id, entry.id, pair))
    return out
