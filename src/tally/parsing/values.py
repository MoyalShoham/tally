"""Parsing of individual cell values as they appear in bank and ERP exports."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

_EXCEL_EPOCH = date(1899, 12, 30)
_CURRENCY_NOISE = re.compile(r"[₪$€\s‎‏‪-‮]|ILS|NIS|ש\"ח|ש״ח", re.IGNORECASE)


class ValueParseError(ValueError):
    pass


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value.strip() in {"", "-", "nan", "NaN"}


def parse_amount(value: Any) -> Decimal | None:
    """Parse an amount such as '1,234.50', '(1,234.50)', '1,234.50-' or '₪ -80'.

    Returns None for empty cells. Raises ValueParseError for anything else that
    is not a number, so bad rows fail loudly instead of becoming zero.
    """
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise ValueParseError(f"not an amount: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    if isinstance(value, Decimal):
        return value

    text = _CURRENCY_NOISE.sub("", str(value))
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    text = text.replace(",", "")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueParseError(f"not an amount: {value!r}") from exc
    return -amount if negative else amount


def parse_date(value: Any, formats: Sequence[str]) -> date | None:
    """Parse a date cell: datetime objects, Excel serial numbers or text in `formats`."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        if 20000 < value < 80000:  # Excel serial date (1954..2119)
            return _EXCEL_EPOCH + timedelta(days=int(value))
        raise ValueParseError(f"not a date: {value!r}")
    text = str(value).strip().split(" ")[0]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueParseError(f"not a date: {value!r}")


def clean_text(value: Any) -> str:
    if is_blank(value):
        return ""
    text = str(value)
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    return " ".join(text.replace("‏", "").replace("‎", "").split())
