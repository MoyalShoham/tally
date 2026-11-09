import random
from decimal import Decimal

import pytest

from tally.matching.assignment import assign
from tally.matching.candidates import Candidate
from tally.matching.scoring import PairScore
from tally.matching.split import MAX_CANDIDATES, brute_force_subsets, find_subsets


@pytest.mark.parametrize("seed", range(30))
def test_find_subsets_agrees_with_brute_force(seed: int) -> None:
    rng = random.Random(seed)
    amounts = [
        Decimal(rng.choice([350, 480, 750, 1200, 1500])) + Decimal(rng.randint(0, 99)) / 100
        for _ in range(rng.randint(2, 12))
    ]
    chosen = rng.sample(range(len(amounts)), k=min(len(amounts), rng.randint(2, 4)))
    target = sum((amounts[i] for i in chosen), Decimal(0))
    found = find_subsets(amounts, target)
    expected = brute_force_subsets(amounts, target)
    assert set(found) == expected or (len(found) == 20 and set(found) <= expected)


def test_find_subsets_with_tolerance() -> None:
    amounts = [Decimal("1000.00"), Decimal("500.00"), Decimal("300.00")]
    assert find_subsets(amounts, Decimal("1490.00")) == []
    assert find_subsets(amounts, Decimal("1490.00"), tolerance=Decimal("15")) == [(0, 1)]


def test_find_subsets_prefers_smaller_combinations() -> None:
    amounts = [Decimal(x) for x in ("100", "500", "200", "300")]
    found = find_subsets(amounts, Decimal("600"))
    assert found == [(0, 1), (0, 2, 3)]


def test_find_subsets_caps_input_size() -> None:
    with pytest.raises(ValueError):
        find_subsets([Decimal(1)] * (MAX_CANDIDATES + 1), Decimal(2))


def _cand(t: str, e: str, score: float) -> Candidate:
    pair = PairScore(
        score=score, amount=1.0, name=1.0, date=1.0, reference=False, amount_difference=Decimal(0)
    )
    return Candidate(t, e, pair)


def test_assignment_beats_greedy() -> None:
    # Greedy would give T1 its best entry E1 (0.90) and leave T2 with nothing good.
    # The global optimum is T1-E2 + T2-E1 (0.85 + 0.88 > 0.90 + 0.10).
    candidates = [
        _cand("T1", "E1", 0.90),
        _cand("T1", "E2", 0.85),
        _cand("T2", "E1", 0.88),
        _cand("T2", "E2", 0.10),
    ]
    chosen = {(a.candidate.transaction_id, a.candidate.entry_id) for a in assign(candidates)}
    assert chosen == {("T1", "E2"), ("T2", "E1")}


def test_assignment_reports_runner_up_and_alternatives() -> None:
    result = assign([_cand("T1", "E1", 0.95), _cand("T1", "E2", 0.60)])
    assert len(result) == 1
    assert result[0].candidate.entry_id == "E1"
    assert result[0].runner_up == 0.60
    assert result[0].alternatives == ("E2",)


def test_independent_components_are_solved_separately() -> None:
    result = assign([_cand("T1", "E1", 0.9), _cand("T2", "E2", 0.9), _cand("T3", "E3", 0.9)])
    assert len(result) == 3
