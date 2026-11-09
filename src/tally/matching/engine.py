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

from tally.matching.assignment import Assignment, assign
from tally.matching.candidates import Candidate, generate_candidates
from tally.matching.references import DocumentIndex
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
from tally.normalize import name_similarity, normalize_name, reference_hit
from tally.resolver import Resolver

SAME_PARTY = 0.75  # name similarity needed to treat entries as one counterparty's
SPLIT_AUTO_MAX_POOL = 12  # larger pools make coincidental sums too likely to auto-post
TWIN_DATE_FIT = 0.4  # a same-amount twin competes only if the date also fits this well
_PoolItem = tuple[LedgerEntry, float, bool]  # entry, name similarity, number quoted


@dataclass
class _State:
    open_txns: dict[str, BankTransaction]
    open_entries: dict[str, LedgerEntry]
    matches: list[Match] = field(default_factory=list)
    outstanding: dict[str, Decimal] = field(default_factory=dict)  # partially paid entries
    twins: dict[str, set[str]] = field(default_factory=dict)  # same party, same amount
    settled: set[str] = field(default_factory=set)  # entries closed by AUTO matches

    def has_open_twin(self, entry_id: str, exclude: set[str]) -> bool:
        return bool(self.twins.get(entry_id, set()) - exclude - self.settled)


def _twin_map(entries: list[LedgerEntry]) -> dict[str, set[str]]:
    groups: dict[tuple[str, str, Decimal], list[str]] = defaultdict(list)
    for e in entries:
        groups[(e.kind.value, normalize_name(e.counterparty), e.amount)].append(e.id)
    return {i: set(ids) - {i} for ids in groups.values() if len(ids) > 1 for i in ids}


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
            twins=_twin_map(entries),
        )
        self._one_to_one(state)
        self._splits(state)
        self._partials(state)
        if self.resolver is not None:
            self._resolve(state, {e.id: e for e in entries}, {t.id: t for t in transactions})
        return self._report(state, transactions, entries)

    # Pass 1 ---------------------------------------------------------------
    def _blockers(
        self, a: Assignment, rivals: list[Candidate], quoted: set[str], entries: dict[str, LedgerEntry]
    ) -> list[str]:
        """Reasons why a high-scoring match must still go to a human."""
        pair = a.candidate.pair
        blockers = []
        margin = pair.score - a.runner_up
        if margin < self.cfg.auto_margin:
            blockers.append(f"close alternative (margin {margin:.2f})")
        other_quoted = quoted - {a.candidate.entry_id}
        if other_quoted:
            numbers = ", ".join(sorted(entries[i].number for i in other_quoted))
            blockers.append(f"transfer quotes a different document ({numbers})")
        if not pair.reference:
            # Same customer, same amount, another open entry whose dates also fit: the
            # bank line cannot tell them apart. A twin far outside its payment window
            # (last month's rent) does not count, so recurring bills still auto-match.
            twins = [
                r
                for r in rivals
                if r.pair.amount == 1.0 and r.pair.name >= 0.9 and r.pair.date >= TWIN_DATE_FIT
            ]
            if pair.amount == 1.0 and twins:
                blockers.append("same counterparty has another open entry for this amount")
        return blockers

    def _one_to_one(self, state: _State) -> None:
        cfg = self.cfg
        entries = dict(state.open_entries)
        documents = DocumentIndex(list(entries.values()))
        candidates = generate_candidates(list(state.open_txns.values()), list(entries.values()), cfg)
        by_txn: dict[str, list[Candidate]] = defaultdict(list)
        for c in candidates:
            by_txn[c.transaction_id].append(c)  # all candidates count as rivals ...
        # ... but only pairs good enough to report take part in the assignment.
        candidates = [c for c in candidates if c.pair.score >= cfg.review_threshold]

        for a in assign(candidates):
            pair = a.candidate.pair
            if pair.score < cfg.review_threshold:
                continue
            txn = state.open_txns[a.candidate.transaction_id]
            quoted = documents.quoted(f"{txn.description} {txn.reference or ''}")
            if quoted and a.candidate.entry_id not in quoted:
                # The transfer names another open document whose amount does not fit:
                # most likely an installment. Leave it to the partial-payment pass.
                continue
            rivals = [c for c in by_txn[txn.id] if c is not a.candidate]
            blockers = self._blockers(a, rivals, quoted, entries)
            confident = pair.score >= cfg.auto_threshold and not blockers
            reasons = list(pair.reasons)
            if pair.score >= cfg.auto_threshold:
                reasons += blockers
            state.matches.append(
                Match(
                    transaction_ids=[a.candidate.transaction_id],
                    entry_ids=[a.candidate.entry_id],
                    kind=MatchKind.ONE_TO_ONE,
                    status=MatchStatus.AUTO if confident else MatchStatus.REVIEW,
                    confidence=min(pair.score, 1.0),
                    reasons=reasons,
                    amount_difference=pair.amount_difference,
                    alternative_entry_ids=list(a.alternatives),
                )
            )
            del state.open_txns[a.candidate.transaction_id]
            del state.open_entries[a.candidate.entry_id]
            if confident:
                state.settled.add(a.candidate.entry_id)

    # Pass 2 ---------------------------------------------------------------
    def _party_pool(self, txn: BankTransaction, entries: list[LedgerEntry]) -> list[_PoolItem]:
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
            # Keep the entries due closest to the payment date when capping. If the
            # pool was cut, "only one combination fits" no longer proves anything.
            truncated = len(pool) > MAX_CANDIDATES
            pool.sort(key=lambda p: (abs((p[0].due_date - txn.date).days), p[0].id))
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
                solutions = [
                    s
                    for s in find_subsets(amounts, paid, tolerance)
                    if sum((amounts[i] for i in s), Decimal(0)) >= paid
                ]
            if not solutions:
                continue

            def rank(solution: tuple[int, ...], pool: list[_PoolItem] = pool) -> tuple[int, int]:
                # Prefer combinations with quoted numbers, then the oldest entries.
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
            chosen_entries = [pool[i][0] for i in chosen]
            quoted_elsewhere = any(ref for i, (_, _, ref) in enumerate(pool) if i not in chosen)
            chosen_ids = {e.id for e in chosen_entries}
            # A twin (same party, same amount) of a chosen entry may have been taken by
            # an earlier, unconfirmed match; then this combination is not really unique.
            twin_doubt = not all_refs and any(state.has_open_twin(i, chosen_ids) for i in chosen_ids)
            # With many open entries, a coincidental sum is likely; demand evidence.
            small_pool = len(pool) <= SPLIT_AUTO_MAX_POOL
            auto = (
                unique
                and exact
                and not truncated
                and not quoted_elsewhere
                and not twin_doubt
                and (all_refs or small_pool)
                and confidence >= self.cfg.auto_threshold
            )
            total = sum((e.amount for e in chosen_entries), Decimal(0))
            alternatives = sorted(
                {pool[i][0].id for s in solutions[1:] for i in s} - {e.id for e in chosen_entries}
            )
            reasons = [
                f"{len(chosen_entries)} entries of the same counterparty sum to {total:.2f}",
                "only combination that fits"
                if unique
                else f"{len(solutions)} combinations fit; chose oldest first",
            ]
            if not exact:
                reasons.append(f"short by {total - paid:.2f}, likely a bank fee")
            state.matches.append(
                Match(
                    transaction_ids=[txn.id],
                    entry_ids=[e.id for e in chosen_entries],
                    kind=MatchKind.SPLIT,
                    status=MatchStatus.AUTO if auto else MatchStatus.REVIEW,
                    confidence=round(min(confidence, 0.99), 4),
                    reasons=reasons,
                    amount_difference=paid - total,
                    alternative_entry_ids=alternatives[:5],
                )
            )
            del state.open_txns[txn.id]
            for entry in chosen_entries:
                del state.open_entries[entry.id]
            if auto:
                state.settled |= chosen_ids

    # Pass 3 ---------------------------------------------------------------
    def _partials(self, state: _State) -> None:
        entries = dict(state.open_entries)
        for txn in sorted(state.open_txns.values(), key=lambda t: (t.date, t.id)):
            paid = abs(txn.amount)
            options = []
            for entry, similarity, ref in self._party_pool(txn, list(entries.values())):
                due = state.outstanding.get(entry.id, entry.amount)
                started = entry.id in state.outstanding
                # The last installment settles exactly what is left of a started entry.
                final = started and abs(paid - due) <= Decimal("0.01")
                if (paid >= due - Decimal("0.01") and not final) or paid < due * Decimal("0.05"):
                    continue
                if ref or similarity >= 0.85:
                    options.append((entry, ref, final, started))
            if not options:
                continue
            # Evidence order: quoted number, settles a started entry exactly, continues a
            # started entry, then the entry due closest to the payment date.
            options.sort(
                key=lambda o: (not o[1], not o[2], not o[3], abs((o[0].due_date - txn.date).days), o[0].id)
            )
            entry, ref, final, started = options[0]
            due = state.outstanding.get(entry.id, entry.amount)
            state.outstanding[entry.id] = due - paid
            confidence = 0.60 + (0.10 if ref else 0) + (0.15 if final else 0)
            confidence -= 0.10 if len(options) > 1 else 0
            state.matches.append(
                Match(
                    transaction_ids=[txn.id],
                    entry_ids=[entry.id],
                    kind=MatchKind.PARTIAL,
                    status=MatchStatus.REVIEW,
                    confidence=round(confidence, 4),
                    reasons=[
                        f"final installment: settles the remaining {due:.2f}"
                        if final
                        else f"partial payment: {paid:.2f} of {due:.2f} outstanding",
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
                    f"second opinion prefers {entries[decision.entry_id].number}: {decision.rationale}"
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
                transactions=len(transactions),
                entries=len(entries),
                auto_matched_transactions=counts[MatchStatus.AUTO],
                review_transactions=counts[MatchStatus.REVIEW],
                unmatched_transactions=len(unmatched_txns),
                unmatched_entries=len(unmatched_entries),
            ),
        )
