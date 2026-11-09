from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tally.models import BankTransaction, EntryKind, LedgerEntry


def txn(tid: str, day: str, amount: str, description: str, reference: str = "") -> BankTransaction:
    return BankTransaction(
        id=tid,
        date=date.fromisoformat(day),
        amount=Decimal(amount),
        description=description,
        reference=reference or None,
    )


def entry(
    eid: str,
    party: str,
    amount: str,
    issued: str,
    due: str,
    number: str = "",
    kind: EntryKind = EntryKind.INVOICE,
) -> LedgerEntry:
    return LedgerEntry(
        id=eid,
        kind=kind,
        number=number or f"INV-{eid}",
        counterparty=party,
        amount=Decimal(amount),
        issue_date=date.fromisoformat(issued),
        due_date=date.fromisoformat(due),
    )


@pytest.fixture
def make_txn():  # type: ignore[no-untyped-def]
    return txn


@pytest.fixture
def make_entry():  # type: ignore[no-untyped-def]
    return entry
