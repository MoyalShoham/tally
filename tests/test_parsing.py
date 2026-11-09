import io
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from tally.models import EntryKind
from tally.parsing.bank import BankFileError, parse_bank_file
from tally.parsing.ledger import LedgerFileError, parse_ledger_file
from tally.parsing.profiles import builtin_profiles, load_profile

HEBREW_STATEMENT = """תנועות בחשבון 12-345-678901
הופק בתאריך,09/11/2026

תאריך,תיאור,אסמכתא,חובה,זכות,יתרה
01/03/2026,העברה מ-גלבוע ציוד מש,260142,,"1,770.00","51,770.00"
02/03/2026,"חיוב מס""ב תקשורת פלוס",88123,"1,250.00",,"50,520.00"
02/03/2026,עמלת ערוץ ישיר,,6.50,,"50,513.50"
02/03/2026,עמלת ערוץ ישיר,,6.50,,"50,507.00"

סה"כ,,,,,"50,507.00"
"""

ENGLISH_STATEMENT = """Date,Description,Reference,Amount,Balance
2026-03-01,TRANSFER FROM NEGEV LOGISTICS,INV-260170,2950.00,12950.00
2026-03-02,PAYMENT TO CLOUDLINE HOSTING,,-118.00,12832.00
"""


def test_hebrew_debit_credit_export() -> None:
    result = parse_bank_file(
        HEBREW_STATEMENT.encode("utf-8-sig"), "statement.csv", load_profile("hebrew_debit_credit")
    )
    txns = result.transactions
    assert [t.amount for t in txns] == [
        Decimal("1770.00"),
        Decimal("-1250.00"),
        Decimal("-6.50"),
        Decimal("-6.50"),
    ]
    assert txns[0].date == date(2026, 3, 1)
    assert txns[0].reference == "260142"
    assert txns[1].description == 'חיוב מס"ב תקשורת פלוס'
    assert txns[0].balance == Decimal("51770.00")
    assert len(result.skipped_rows) == 1  # the totals line


def test_identical_rows_get_distinct_but_stable_ids() -> None:
    profile = load_profile("hebrew_debit_credit")
    first = parse_bank_file(HEBREW_STATEMENT.encode("utf-8-sig"), "a.csv", profile).transactions
    second = parse_bank_file(HEBREW_STATEMENT.encode("utf-8-sig"), "a.csv", profile).transactions
    assert first[2].id != first[3].id  # two genuine identical fees
    assert [t.id for t in first] == [t.id for t in second]  # re-import is idempotent


def test_windows_hebrew_encoding() -> None:
    result = parse_bank_file(
        HEBREW_STATEMENT.encode("cp1255"), "statement.csv", load_profile("hebrew_debit_credit")
    )
    assert result.transactions[0].description == "העברה מ-גלבוע ציוד מש"


def test_english_signed_export() -> None:
    txns = parse_bank_file(ENGLISH_STATEMENT.encode(), "s.csv", load_profile("english_signed")).transactions
    assert [t.amount for t in txns] == [Decimal("2950.00"), Decimal("-118.00")]
    assert txns[0].direction is EntryKind.INVOICE
    assert txns[1].direction is EntryKind.BILL


def test_excel_export() -> None:
    frame = pd.DataFrame(
        [
            ["תנועות בחשבון", None, None, None, None],
            ["תאריך", "תיאור", "אסמכתא", "חובה", "זכות"],
            [pd.Timestamp("2026-03-01"), "העברה מ-גלבוע", 260142, None, 1770.0],
            [pd.Timestamp("2026-03-02"), 'הו"ק משרדי הצפון', None, 1000.0, None],
        ]
    )
    buffer = io.BytesIO()
    frame.to_excel(buffer, header=False, index=False)
    txns = parse_bank_file(
        buffer.getvalue(), "statement.xlsx", load_profile("hebrew_debit_credit")
    ).transactions
    assert [(t.date, t.amount) for t in txns] == [
        (date(2026, 3, 1), Decimal("1770.00")),
        (date(2026, 3, 2), Decimal("-1000.00")),
    ]
    assert txns[0].reference == "260142"


def test_wrong_profile_reports_missing_header() -> None:
    with pytest.raises(BankFileError, match="no header row"):
        parse_bank_file(ENGLISH_STATEMENT.encode(), "s.csv", load_profile("hebrew_debit_credit"))


def test_unknown_profile() -> None:
    with pytest.raises(FileNotFoundError, match="built-in"):
        load_profile("no_such_bank")


def test_builtin_profiles_are_valid() -> None:
    names = {p.name for p in builtin_profiles()}
    assert {"hebrew_debit_credit", "english_signed"} <= names


LEDGER = """id,kind,number,counterparty,amount,issue_date,due_date
I1,invoice,INV-260142,"גלבוע ציוד משרדי בע""מ",1770.00,2026-02-01,2026-03-03
B1,bill,88123,תקשורת פלוס,"1,250.00",01/02/2026,10/02/2026
"""


def test_ledger_file() -> None:
    entries = parse_ledger_file(LEDGER.encode("utf-8-sig"), "open_items.csv")
    assert entries[0].counterparty == 'גלבוע ציוד משרדי בע"מ'
    assert entries[1].kind is EntryKind.BILL
    assert entries[1].amount == Decimal("1250.00")
    assert entries[1].due_date == date(2026, 2, 10)


def test_ledger_missing_columns() -> None:
    with pytest.raises(LedgerFileError, match="missing columns: due_date"):
        parse_ledger_file(b"id,kind,number,counterparty,amount,issue_date\n", "x.csv")


def test_ledger_rejects_duplicate_ids() -> None:
    duplicated = LEDGER + LEDGER.splitlines()[1] + "\n"
    with pytest.raises(LedgerFileError, match="unique"):
        parse_ledger_file(duplicated.encode(), "x.csv")


def test_ledger_rejects_unknown_kind() -> None:
    bad = LEDGER.replace("bill,", "credit_note,")
    with pytest.raises(LedgerFileError, match="unknown kind"):
        parse_ledger_file(bad.encode(), "x.csv")
