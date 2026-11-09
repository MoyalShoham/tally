"""Pairwise scoring of a bank transaction against one open ledger entry.

The score combines three signals that an accountant uses by eye: does the amount
fit, does the description look like the counterparty, and is the timing
plausible. An invoice number quoted in the transfer is strong extra evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from tally.models import BankTransaction, LedgerEntry
from tally.normalize import name_similarity, reference_hit


@dataclass(frozen=True)
class ScoringConfig:
    weight_amount: float = 0.45
    weight_name: float = 0.35
    weight_date: float = 0.20
    reference_bonus: float = 0.25
    # Payers' banks often deduct a transfer fee, so an incoming payment can be a
    # little short. Allowed gap = max(absolute, relative * amount).
    fee_tolerance_abs: Decimal = Decimal("20")
    fee_tolerance_rel: Decimal = Decimal("0.005")
    days_before_issue_allowed: int = 3
    grace_days_after_due: int = 7
    max_days_after_due: int = 120
    date_decay_days: float = 20.0
    weak_name_threshold: float = 0.6
    weak_name_penalty: float = 0.6
    min_candidate_score: float = 0.40
    auto_threshold: float = 0.80
    auto_margin: float = 0.08
    review_threshold: float = 0.50

    def fee_tolerance(self, amount: Decimal) -> Decimal:
        return max(self.fee_tolerance_abs, (self.fee_tolerance_rel * amount).quantize(Decimal("0.01")))


@dataclass(frozen=True)
class PairScore:
    score: float
    amount: float
    name: float
    date: float
    reference: bool
    amount_difference: Decimal
    reasons: tuple[str, ...] = field(default_factory=tuple)


def amount_fit(
    paid: Decimal, expected: Decimal, cfg: ScoringConfig, incoming: bool = True
) -> float | None:
    """1.0 for an exact match, lower inside the fee tolerance, None outside it."""
    gap = paid - expected
    if abs(gap) <= Decimal("0.01"):
        return 1.0
    if abs(gap) > cfg.fee_tolerance(expected):
        return None
    if incoming and gap < 0:
        return 0.8  # the payer's bank deducted a transfer fee: common
    # Receiving more than invoiced, or paying a vendor a different amount than
    # the bill, is unusual; keep it as a weak candidate only.
    return 0.4


def date_fit(txn: BankTransaction, entry: LedgerEntry, cfg: ScoringConfig) -> float | None:
    """1.0 between issue date and due date (+grace); decays outside; None if implausible."""
    days_after_issue = (txn.date - entry.issue_date).days
    days_after_due = (txn.date - entry.due_date).days
    if days_after_issue < -cfg.days_before_issue_allowed or days_after_due > cfg.max_days_after_due:
        return None
    if days_after_issue >= 0 and days_after_due <= cfg.grace_days_after_due:
        return 1.0
    outside = -days_after_issue if days_after_issue < 0 else days_after_due - cfg.grace_days_after_due
    return math.exp(-outside / cfg.date_decay_days)


def direction_matches(txn: BankTransaction, entry: LedgerEntry) -> bool:
    return txn.direction == entry.kind


def score_pair(txn: BankTransaction, entry: LedgerEntry, cfg: ScoringConfig) -> PairScore | None:
    """Score one transaction/entry pair, or None if they cannot be the same payment."""
    if not direction_matches(txn, entry):
        return None
    paid = abs(txn.amount)
    a = amount_fit(paid, entry.amount, cfg, incoming=txn.amount > 0)
    if a is None:
        return None
    d = date_fit(txn, entry, cfg)
    if d is None:
        return None

    text = f"{txn.description} {txn.reference or ''}"
    ref = reference_hit(text, entry.number)
    n = name_similarity(txn.description, entry.counterparty)

    score = cfg.weight_amount * a + cfg.weight_name * n + cfg.weight_date * d
    if ref:
        score += cfg.reference_bonus
    elif n < cfg.weak_name_threshold:
        # Amount and date alone are a coincidence waiting to happen.
        score *= cfg.weak_name_penalty
    # Not capped at 1.0: a quoted document number must still outrank an
    # otherwise perfect twin. Confidence shown to users is capped separately.

    reasons = [
        "amount exact" if a == 1.0 else f"amount differs by {paid - entry.amount:+.2f}",
        f"name similarity {n:.2f}",
        f"paid {(txn.date - entry.due_date).days:+d} days from due date",
    ]
    if ref:
        reasons.insert(0, f"document number {entry.number} quoted in transfer")
    return PairScore(
        score=round(score, 4), amount=a, name=n, date=round(d, 4), reference=ref,
        amount_difference=paid - entry.amount, reasons=tuple(reasons),
    )
