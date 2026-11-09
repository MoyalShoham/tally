"""Regression guard: a change that hurts accuracy fails CI."""

from tally.evaluation import run_benchmark
from tally.resolver import parse_answer
from tally.synthetic import generate


def test_generator_is_deterministic() -> None:
    a, b = generate(seed=5, months=2), generate(seed=5, months=2)
    assert [t.id for t in a.transactions] == [t.id for t in b.transactions]
    assert [e.id for e in a.entries] == [e.id for e in b.entries]


def test_benchmark_quality_floor() -> None:
    m = run_benchmark(seed=7)  # also checks that the bank file round trip keeps ids
    assert m.transactions > 1800
    assert m.auto_precision >= 0.99
    assert m.auto_recall >= 0.60
    assert m.found_rate >= 0.80
    assert m.noise_matched <= 5
    assert m.seconds < 10


def test_resolver_answer_must_be_one_of_the_options() -> None:
    from conftest import entry

    options = [entry("I1", "א", "100", "2026-01-01", "2026-01-31")]
    assert parse_answer('{"entry_id": "I1", "rationale": "fits"}', options) is not None
    assert parse_answer('{"entry_id": "I999", "rationale": "made up"}', options) is None
    assert parse_answer("I think it is I1", options) is None
    none = parse_answer('Sure! {"entry_id": null, "rationale": "nothing fits"}', options)
    assert none is not None and none.entry_id is None
