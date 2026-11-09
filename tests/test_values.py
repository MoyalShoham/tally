from datetime import date
from decimal import Decimal

import pytest

from tally.parsing.values import ValueParseError, clean_text, parse_amount, parse_date

FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.50", Decimal("1234.50")),
        ("(1,234.50)", Decimal("-1234.50")),
        ("1,234.50-", Decimal("-1234.50")),
        ("₪ -80", Decimal("-80")),
        ('2,000 ש"ח', Decimal("2000")),
        ("\u200f1,500.00\u200f", Decimal("1500.00")),
        (1500, Decimal("1500")),
        (12.1, Decimal("12.10")),
    ],
)
def test_parse_amount(raw: object, expected: Decimal) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "-", float("nan")])
def test_blank_amount_is_none(raw: object) -> None:
    assert parse_amount(raw) is None


def test_bad_amount_raises() -> None:
    with pytest.raises(ValueParseError):
        parse_amount('סה"כ')


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("05/03/2026", date(2026, 3, 5)),
        ("05/03/26", date(2026, 3, 5)),
        ("2026-03-05", date(2026, 3, 5)),
        ("05/03/2026 00:00", date(2026, 3, 5)),
        (46086, date(2026, 3, 5)),  # Excel serial date
    ],
)
def test_parse_date(raw: object, expected: date) -> None:
    assert parse_date(raw, FORMATS) == expected


def test_bad_date_raises() -> None:
    with pytest.raises(ValueParseError):
        parse_date("יתרת סגירה", FORMATS)


def test_clean_text_strips_direction_marks_and_spaces() -> None:
    assert clean_text("\u200fהעברה   מ-גלבוע \u200e") == "העברה מ-גלבוע"
    assert clean_text(260142.0) == "260142"
