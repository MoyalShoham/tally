"""Benchmark: precision and recall of the engine against synthetic ground truth."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from tally.matching.engine import ReconciliationEngine
from tally.models import MatchStatus, ReconciliationReport
from tally.parsing.bank import parse_bank_file
from tally.parsing.ledger import parse_ledger_file
from tally.parsing.profiles import load_profile
from tally.synthetic import Dataset, Scenario, bank_csv_hebrew, generate, ledger_csv


@dataclass
class ScenarioStats:
    total: int = 0
    auto_correct: int = 0
    review_correct: int = 0
    wrong: int = 0
    missed: int = 0


@dataclass
class Metrics:
    transactions: int
    entries: int
    links: int
    auto_matches: int
    auto_correct: int
    review_matches: int
    review_correct: int
    review_in_top3: int
    noise_matched: int
    seconds: float
    by_scenario: dict[str, ScenarioStats] = field(default_factory=dict)

    @property
    def auto_precision(self) -> float:
        return self.auto_correct / self.auto_matches if self.auto_matches else 1.0

    @property
    def auto_recall(self) -> float:
        return self.auto_correct / self.links if self.links else 0.0

    @property
    def found_rate(self) -> float:
        """Share of true links the engine found, either automatically or for review."""
        return (self.auto_correct + self.review_correct) / self.links if self.links else 0.0

    @property
    def review_precision(self) -> float:
        return self.review_correct / self.review_matches if self.review_matches else 1.0

    @property
    def review_top3(self) -> float:
        """Review items where the right entry is the suggestion or one of the alternatives."""
        return self.review_in_top3 / self.review_matches if self.review_matches else 1.0


def score_report(dataset: Dataset, report: ReconciliationReport, seconds: float = 0.0) -> Metrics:
    truth = {t.transaction_id: t for t in dataset.truth}
    stats: dict[str, ScenarioStats] = defaultdict(ScenarioStats)
    for link in dataset.truth:
        stats[link.scenario.value].total += 1

    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for match in report.matches:
        for tid in match.transaction_ids:
            seen.add(tid)
            link = truth.get(tid)
            correct = link is not None and set(match.entry_ids) == set(link.entry_ids)
            counts[f"{match.status.value}_total"] += 1
            if link is None:
                counts["noise_matched"] += 1
                continue
            bucket = stats[link.scenario.value]
            if correct and match.status is MatchStatus.AUTO:
                counts["auto_correct"] += 1
                bucket.auto_correct += 1
            elif correct:
                counts["review_correct"] += 1
                counts["review_in_top3"] += 1
                bucket.review_correct += 1
            else:
                bucket.wrong += 1
                if (match.status is MatchStatus.REVIEW and len(link.entry_ids) == 1
                        and link.entry_ids[0] in match.alternative_entry_ids[:2]):
                    counts["review_in_top3"] += 1
    for link in dataset.truth:
        if link.transaction_id not in seen:
            stats[link.scenario.value].missed += 1

    return Metrics(
        transactions=len(dataset.transactions), entries=len(dataset.entries),
        links=len(dataset.truth),
        auto_matches=counts["auto_total"], auto_correct=counts["auto_correct"],
        review_matches=counts["review_total"], review_correct=counts["review_correct"],
        review_in_top3=counts["review_in_top3"],
        noise_matched=counts["noise_matched"], seconds=seconds,
        by_scenario=dict(sorted(stats.items())),
    )


def run_benchmark(seed: int = 7, engine: ReconciliationEngine | None = None) -> Metrics:
    """Generate data, write it to files, parse it back and reconcile, end to end."""
    dataset = generate(seed=seed)
    bank = parse_bank_file(bank_csv_hebrew(dataset.transactions), "statement.csv",
                           load_profile("hebrew_debit_credit")).transactions
    entries = parse_ledger_file(ledger_csv(dataset.entries), "open_items.csv")
    if [t.id for t in bank] != [t.id for t in dataset.transactions]:
        raise AssertionError("bank file round trip changed transaction ids")
    started = time.perf_counter()
    report = (engine or ReconciliationEngine()).reconcile(bank, entries)
    return score_report(dataset, report, time.perf_counter() - started)


def format_markdown(metrics: Metrics, seed: int) -> str:
    m = metrics
    lines = [
        f"Seed {seed}: {m.transactions:,} bank transactions, {m.entries:,} open entries, "
        f"{m.links:,} true payment links. Reconciled in {m.seconds:.2f}s.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Auto-match precision | {m.auto_precision:.1%} ({m.auto_correct:,} of "
        f"{m.auto_matches:,}) |",
        f"| Auto-match recall | {m.auto_recall:.1%} of all true links |",
        f"| Found (auto + review) | {m.found_rate:.1%} of all true links |",
        f"| Review suggestions correct | {m.review_precision:.1%} ({m.review_correct:,} of "
        f"{m.review_matches:,}) |",
        f"| Review: right entry among top 3 options | {m.review_top3:.1%} |",
        f"| Unrelated bank lines wrongly linked | {m.noise_matched} |",
        "",
        "| Scenario | Links | Auto, correct | Review, correct | Wrong | Missed |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, s in m.by_scenario.items():
        lines.append(f"| {name} | {s.total} | {s.auto_correct} | {s.review_correct} | "
                     f"{s.wrong} | {s.missed} |")
    return "\n".join(lines) + "\n"


__all__ = ["Metrics", "Scenario", "format_markdown", "run_benchmark", "score_report"]
