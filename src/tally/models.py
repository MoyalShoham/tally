"""Domain model.

Sign convention for bank transactions: a positive amount is money coming into the
account, a negative amount is money going out. Ledger entries always carry a
positive amount; their kind says which direction the payment should flow.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


class EntryKind(StrEnum):
    INVOICE = "invoice"  # receivable: a customer owes us, we expect money in
    BILL = "bill"  # payable: we owe a vendor, we expect money out


class MatchKind(StrEnum):
    ONE_TO_ONE = "one_to_one"
    SPLIT = "split"  # one bank transaction settles several open entries
    PARTIAL = "partial"  # bank transaction covers only part of one entry


class MatchStatus(StrEnum):
    AUTO = "auto"  # confident enough to post without a human
    REVIEW = "review"  # plausible, a human should confirm


class BankTransaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    date: date
    amount: Decimal
    description: str
    reference: str | None = None
    balance: Decimal | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def direction(self) -> EntryKind:
        return EntryKind.INVOICE if self.amount > 0 else EntryKind.BILL


class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: EntryKind
    number: str
    counterparty: str
    amount: Decimal = Field(gt=0)
    issue_date: date
    due_date: date
    currency: str = "ILS"


class Match(BaseModel):
    transaction_ids: list[str]
    entry_ids: list[str]
    kind: MatchKind
    status: MatchStatus
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)
    amount_difference: Decimal = Decimal("0")


class ReconciliationSummary(BaseModel):
    transactions: int
    entries: int
    auto_matched_transactions: int
    review_transactions: int
    unmatched_transactions: int
    unmatched_entries: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def auto_match_rate(self) -> float:
        if self.transactions == 0:
            return 0.0
        return round(self.auto_matched_transactions / self.transactions, 4)


class ReconciliationReport(BaseModel):
    matches: list[Match]
    unmatched_transaction_ids: list[str]
    unmatched_entry_ids: list[str]
    summary: ReconciliationSummary
