"""Globally optimal one-to-one assignment.

Greedy matching (take each transaction's best entry) fails when two customers
pay the same amount in the same week: the first transaction can grab the entry
that the second one fits better. Instead the candidate pairs form a bipartite
graph; each connected component is solved with the Hungarian algorithm
(scipy.optimize.linear_sum_assignment), maximizing the total score.

Components are usually tiny, so many small assignments replace one huge matrix.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

from tally.matching.candidates import Candidate

_MISSING = 1e6  # cost of a pair that is not a candidate


@dataclass(frozen=True)
class Assignment:
    candidate: Candidate
    runner_up: float  # best competing score for either side, 0 if none
    alternatives: tuple[str, ...]  # other entry ids for this transaction, best first


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def connected_components(candidates: list[Candidate]) -> list[list[Candidate]]:
    uf = _UnionFind()
    for c in candidates:
        uf.union("t:" + c.transaction_id, "e:" + c.entry_id)
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        groups[uf.find("t:" + c.transaction_id)].append(c)
    return list(groups.values())


def _solve_component(component: list[Candidate]) -> list[Candidate]:
    if len(component) == 1:
        return component
    txn_ids = sorted({c.transaction_id for c in component})
    entry_ids = sorted({c.entry_id for c in component})
    row = {t: i for i, t in enumerate(txn_ids)}
    col = {e: j for j, e in enumerate(entry_ids)}
    cost = np.full((len(txn_ids), len(entry_ids)), _MISSING)
    lookup: dict[tuple[int, int], Candidate] = {}
    for c in component:
        i, j = row[c.transaction_id], col[c.entry_id]
        cost[i, j] = -c.pair.score
        lookup[(i, j)] = c
    rows, cols = linear_sum_assignment(cost)
    return [lookup[(i, j)] for i, j in zip(rows, cols, strict=True) if (i, j) in lookup]


def assign(candidates: list[Candidate]) -> list[Assignment]:
    by_txn: dict[str, list[Candidate]] = defaultdict(list)
    by_entry: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_txn[c.transaction_id].append(c)
        by_entry[c.entry_id].append(c)

    result: list[Assignment] = []
    for component in connected_components(candidates):
        for chosen in _solve_component(component):
            rivals = [c.pair.score for c in by_txn[chosen.transaction_id] if c is not chosen]
            rivals += [c.pair.score for c in by_entry[chosen.entry_id] if c is not chosen]
            alternatives = sorted(
                (c for c in by_txn[chosen.transaction_id] if c is not chosen),
                key=lambda c: c.pair.score,
                reverse=True,
            )
            result.append(
                Assignment(
                    candidate=chosen,
                    runner_up=max(rivals, default=0.0),
                    alternatives=tuple(c.entry_id for c in alternatives[:3]),
                )
            )
    return result
