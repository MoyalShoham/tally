"""Reconciliation pipeline.

Pass 1  one-to-one   score candidate pairs, solve a global assignment
Pass 2  split        one transfer settling several entries of one counterparty
Pass 3  partial      a transfer covering part of one entry (installments)
Pass 4  resolver     optional LLM second opinion on matches sent to review

Every match carries a confidence and human-readable reasons. Only confident,
unambiguous matches are marked AUTO; everything else goes to REVIEW, because a
wrong automatic match in the books costs more than a manual click.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from tally.matching.assignment import assign
from tally.matching.candidates import generate_candidates
from tally.matching.scoring import ScoringConfig, date_fit, direction_matches
from tally.matching.split import MAX_CANDIDATES, find_subsets
from tally.models import (
    BankTransaction,
    LedgerEntry,
    Match,
    MatchKind,
    MatchStatus,
    ReconciliationReport,
    ReconciliationSummary,
)
from tally.normalize import name_similarity, reference_hit
from tally.resolver import Resolver

SAME_PARTY = 0.75  # name similarity needed to treat entries as one counterparty's


@dataclass
class _State:
    open_txns: dict[str, BankTransaction]
    open_entries: dict[str, LedgerEntry]
    matches: list[Match] = field(default_factory=list)
    outstanding: dict[str, Decimal] = field(default_factory=dict)  # partially paid entries


class ReconciliationEngine:
    def __init__(self, config: ScoringConfig | None = None, resolver: Resolver | None = None):
        self.cfg = config or ScoringConfig()
        self.resolver = resolver

    def reconcile(
        self, transactions: list[BankTransaction], entries: list[LedgerEntry]
    ) -> ReconciliationReport:
        state = _State(
            open_txns={t.id: t for t in transactions},
            open_entries={e.id: e for e in entries},
        )
        self._one_to_one(state)
        self._splits(state)
        self._partials(state)
        if self.resolver is not None:
            self._resolve(state, {e.id: e for e in entries}, {t.id: t for t in transactions})
        return self._report(state, transactions, entries)

    # Pass 1 ---------------------------------------------------------------
    def _one_to_one(self, state: _State) -> None:
        cfg = self.cfg
        candidates = generate_candidates(
            list(state.open_txns.values()), list(state.open_entries.values()), cfg
        )
        for a in assign(candidates):
            pair = a.candidate.pair
            if pair.score < cfg.review_threshold:
                continue
            margin = pair.score - a.runner_up
            confident = pair.score >= cfg.auto_threshold and margin >= cfg.auto_margin
            reasons = list(pair.reasons)
            if not confident and pair.score >= cfg.auto_threshold:
                reasons.append(f"close alternative (margin {margin:.2f})")
            state.matches.append(
                Match(
                    transaction_ids=[a.candidate.transaction_id],
                    entry_ids=[a.candidate.entry_id],
                    kind=MatchKind.ONE_TO_ONE,
                    status=MatchStatus.AUTO if confident else MatchStatus.REVIEW,
                    confidence=pair.score,
                    reasons=reasons,
                    amount_difference=pair.amount_difference,
                    alternative_entry_ids=list(a.alternatives),
                )
            )
            del state.open_txns[a.candidate.transaction_id]
            del state.open_entries[a.candidate.entry_id]

    # Pass 2 ---------------------------------------------------------------
    def _party_pool(
        self, txn: BankTransaction, entries: list[LedgerEntry]
    ) -> list[tuple[LedgerEntry, float, bool]]:
        pool = []
        for entry in entries:
            if not direction_matches(txn, entry) or date_fit(txn, entry, self.cfg) is None:
                continue
            similarity = name_similarity(txn.description, entry.counterparty)
            ref = reference_hit(f"{txn.description} {txn.reference or ''}", entry.number)
            if similarity >= SAME_PARTY or ref:
                pool.append((entry, similarity, ref))
        return pool

    def _splits(self, state: _State) -> None:
        for txn in sorted(state.open_txns.values(), key=lambda t: (t.date, t.id)):
            pool = self._party_pool(txn, list(state.open_entries.values()))
            if len(pool) < 2:
                continue
            # Customers usually pay oldest invoices first; keep the oldest when capping.
            pool.sort(key=lambda p: (p[0].due_date, p[0].id))
            pool = pool[:MAX_CANDIDATES]
            paid = abs(txn.amount)
            amounts = [p[0].amount for p in pool]
            if sum(amounts, Decimal(0)) + Decimal("0.01") < paid:
                continue

            exact = True
            solutions = find_subsets(amounts, paid)
            if not solutions:
                exact = False
                tolerance = self.cfg.fee_tolerance(paid)
                # Only a shortfall (fee deducted) is plausible, never an overpayment.
                solutions = [s for s in find_subsets(amounts, paid, tolerance)
                             if sum((amounts[i] for i in s), Decimal(0)) >= paid]
            if not solutions:
                continue

            def rank(solution: tuple[int, ...]) -> tuple[int, int]:
                refs = sum(pool[i][2] for i in solution)
                age = sum(pool[i][0].due_date.toordinal() for i in solution) // len(solution)
                return (-refs, age)

            solutions.sort(key=rank)
            chosen = solutions[0]
            unique = len(solutions) == 1
            all_refs = all(pool[i][2] for i in chosen)
            min_name = min(pool[i][1] for i in chosen)

            confidence = 0.70 + (0.15 if unique else 0) + (0.10 if all_refs or min_name >= 0.9 else 0)
            if not exact:
                confidence -= 0.10
            auto = unique and exact and confidence >= self.cfg.auto_threshold

            chosen_entries = [pool[i][0] for i in chosen]
            total = sum((e.amount for e in chosen_entries), Decimal(0))
            alternatives = sorted({pool[i][0].id for s in solutions[1:] for i in s}
                                  - {e.id for e in chosen_entries})
            reasons = [
                f"{len(chosen_entries)} entries of the same counterparty sum to {total:.2f}",
                "only combination that fits" if unique
                else f"{len(solutions)} combinations fit; chose oldest first",
            ]
            if not exact:
                reasons.append(f"short by {total - paid:.2f}, likely a bank fee")
            state.matches.append(
                Match(
                    transaction_ids=[txn.id], entry_ids=[e.id for e in chosen_entries],
                    kind=MatchKind.SPLIT,
                    status=MatchStatus.AUTO if auto else MatchStatus.REVIEW,
                    confidence=round(min(confidence, 0.99), 4), reasons=reasons,
                    amount_difference=paid - total, alternative_entry_ids=alternatives[:5],
                )
            )
            del state.open_txns[txn.id]
            for entry in chosen_entries:
                del state.open_entries[entry.id]

    # Pass 3 ---------------------------------------------------------------
    def _partials(self, state: _State) -> None:
        entries = dict(state.open_entries)
        for txn in sorted(state.open_txns.values(), key=lambda t: (t.date, t.id)):
            paid = abs(txn.amount)
            options = []
            for entry, similarity, ref in self._party_pool(txn, list(entries.values())):
                due = state.outstanding.get(entry.id, entry.amount)
                if paid >= due - Decimal("0.01") or paid < due * Decimal("0.05"):
                    continue
                if ref or similarity >= 0.85:
                    options.append((entry, ref))
            if not options:
                continue
            options.sort(key=lambda o: (not o[1], o[0].due_date, o[0].id))
            entry, ref = options[0]
            due = state.outstanding.get(entry.id, entry.amount)
            state.outstanding[entry.id] = due - paid
            confidence = (0.70 if ref else 0.60) - (0.10 if len(options) > 1 else 0)
            state.matches.append(
                Match(
                    transaction_ids=[txn.id], entry_ids=[entry.id], kind=MatchKind.PARTIAL,
                    status=MatchStatus.REVIEW, confidence=confidence,
                    reasons=[
                        f"partial payment: {paid:.2f} of {due:.2f} outstanding",
                        "document number quoted" if ref else "same counterparty",
                    ],
                    amount_difference=paid - due,
                    alternative_entry_ids=[o[0].id for o in options[1:4]],
                )
            )
            del state.open_txns[txn.id]
            state.open_entries.pop(entry.id, None)

    # Pass 4 ---------------------------------------------------------------
    def _resolve(
        self, state: _State, entries: dict[str, LedgerEntry], txns: dict[str, BankTransaction]
    ) -> None:
        """Ask the resolver about review matches that have alternatives.

        The resolver can only rank the options already found and explain why; it
        never creates a match out of nothing and never promotes one to AUTO.
        """
        assert self.resolver is not None
        for match in state.matches:
            if match.status != MatchStatus.REVIEW or match.kind != MatchKind.ONE_TO_ONE:
                continue
            if not match.alternative_entry_ids:
                continue
            txn = txns[match.transaction_ids[0]]
            option_ids = match.entry_ids + match.alternative_entry_ids
            decision = self.resolver.resolve(txn, [entries[i] for i in option_ids])
            if decision is None:
                continue
            if decision.entry_id == match.entry_ids[0]:
                match.reasons.append(f"second opinion agrees: {decision.rationale}")
            elif decision.entry_id in match.alternative_entry_ids:
                rest = [i for i in match.alternative_entry_ids if i != decision.entry_id]
                match.alternative_entry_ids = [decision.entry_id, *rest]
                match.reasons.append(
                    f"second opinion prefers {entries[decision.entry_id].number}: "
                    f"{decision.rationale}"
                )

    # Report ---------------------------------------------------------------
    def _report(
        self, state: _State, transactions: list[BankTransaction], entries: list[LedgerEntry]
    ) -> ReconciliationReport:
        status_of: dict[str, MatchStatus] = {}
        linked_entries: set[str] = set()
        for match in state.matches:
            for tid in match.transaction_ids:
                status_of[tid] = match.status
            linked_entries.update(match.entry_ids)
        counts: defaultdict[MatchStatus, int] = defaultdict(int)
        for status in status_of.values():
            counts[status] += 1

        unmatched_txns = [t.id for t in transactions if t.id not in status_of]
        unmatched_entries = [e.id for e in entries if e.id not in linked_entries]
        return ReconciliationReport(
            matches=state.matches,
            unmatched_transaction_ids=unmatched_txns,
            unmatched_entry_ids=unmatched_entries,
            summary=ReconciliationSummary(
                transactions=len(transactions), entries=len(entries),
                auto_matched_transactions=counts[MatchStatus.AUTO],
                review_transactions=counts[MatchStatus.REVIEW],
                unmatched_transactions=len(unmatched_txns),
                unmatched_entries=len(unmatched_entries),
            ),
        )
