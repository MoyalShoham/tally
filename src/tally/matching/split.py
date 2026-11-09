"""Split payments: one transfer that settles several open entries.

Customers often pay three invoices with one transfer. Finding which invoices add
up to the transferred amount is subset-sum, which is NP-hard in general, but
the candidate set is small (open entries of one counterparty), so a
meet-in-the-middle search is exact and fast: split the n candidates in two
halves, enumerate the 2^(n/2) sums of each half, sort one side and binary-search
for complements. With the cap of 24 candidates that is at most 2 * 4,096 sums.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from decimal import Decimal
from itertools import combinations

MAX_CANDIDATES = 24
MAX_SOLUTIONS = 20


def _subset_sums(amounts: Sequence[int], offset: int) -> list[tuple[int, int]]:
    """All (sum, bitmask) pairs for `amounts`, with mask bits shifted by `offset`."""
    sums = [(0, 0)]
    for i, amount in enumerate(amounts):
        bit = 1 << (i + offset)
        sums += [(s + amount, mask | bit) for s, mask in sums]
    return sums


def find_subsets(
    amounts: Sequence[Decimal], target: Decimal, tolerance: Decimal = Decimal("0.01"),
    min_size: int = 2,
) -> list[tuple[int, ...]]:
    """Index tuples whose amounts sum to `target` within `tolerance`.

    Returns at most MAX_SOLUTIONS subsets of at least `min_size` items, smallest
    subsets first. Amounts are handled in agorot (integers) to avoid rounding.
    """
    if len(amounts) > MAX_CANDIDATES:
        raise ValueError(f"at most {MAX_CANDIDATES} candidates, got {len(amounts)}")
    cents = [int((a * 100).to_integral_value()) for a in amounts]
    goal = int((target * 100).to_integral_value())
    tol = int((tolerance * 100).to_integral_value())

    half = len(cents) // 2
    left = _subset_sums(cents[:half], 0)
    right = sorted(_subset_sums(cents[half:], half))
    right_sums = [s for s, _ in right]

    solutions: set[int] = set()
    for s, mask in left:
        lo = bisect.bisect_left(right_sums, goal - tol - s)
        hi = bisect.bisect_right(right_sums, goal + tol - s)
        for _, other in right[lo:hi]:
            combined = mask | other
            if combined.bit_count() >= min_size:
                solutions.add(combined)
        if len(solutions) > MAX_SOLUTIONS * 4:
            break

    ordered = sorted(solutions, key=lambda m: (m.bit_count(), m))[:MAX_SOLUTIONS]
    return [tuple(i for i in range(len(cents)) if m >> i & 1) for m in ordered]


def brute_force_subsets(
    amounts: Sequence[Decimal], target: Decimal, tolerance: Decimal = Decimal("0.01"),
    min_size: int = 2,
) -> set[tuple[int, ...]]:
    """Reference implementation used by the tests to check find_subsets."""
    found = set()
    for size in range(min_size, len(amounts) + 1):
        for combo in combinations(range(len(amounts)), size):
            if abs(sum((amounts[i] for i in combo), Decimal(0)) - target) <= tolerance:
                found.add(combo)
    return found
