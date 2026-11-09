"""Read a bank statement export (CSV or Excel) into BankTransaction objects."""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from tally.models import BankTransaction
from tally.parsing.limits import EXCEL_SUFFIXES, FileTooLargeError, check_grid, read_excel
from tally.parsing.profiles import BankProfile
from tally.parsing.values import ValueParseError, clean_text, is_blank, parse_amount, parse_date


class BankFileError(ValueError):
    pass


@dataclass
class ParseResult:
    transactions: list[BankTransaction]
    skipped_rows: list[tuple[int, str]] = field(default_factory=list)  # (row number, reason)


def _read_grid(content: bytes, filename: str, profile: BankProfile) -> pd.DataFrame:
    """Read the raw grid with no header assumptions; header detection happens later."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        raise BankFileError("old .xls files are not supported; save the file as .xlsx or .csv")
    if suffix in EXCEL_SUFFIXES:
        try:
            return read_excel(content, header=None)
        except ValueError as exc:
            raise BankFileError(str(exc)) from exc
    for encoding in profile.encodings:
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Exports often start with title lines that have fewer cells than the table,
        # which pandas' CSV reader rejects; read rows with csv and pad them instead.
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = []
        width = 0
        try:
            for row in csv.reader(io.StringIO(text), dialect):
                rows.append(row)
                width = max(width, len(row))
                check_grid(len(rows), width)  # fail before padding, not after
        except (FileTooLargeError, csv.Error) as exc:
            raise BankFileError(str(exc)) from exc
        return pd.DataFrame([r + [""] * (width - len(r)) for r in rows], dtype=object)
    raise BankFileError(f"could not decode {filename} with any of {profile.encodings}")


def _normalize_header(value: Any) -> str:
    return clean_text(value).strip(" :").lower()


def _find_header(grid: pd.DataFrame, profile: BankProfile) -> tuple[int, dict[str, int]]:
    """Find the header row and map each column role to its column index."""
    roles = profile.columns.model_dump()
    for row_index in range(min(profile.header_search_rows, len(grid))):
        cells = [_normalize_header(v) for v in grid.iloc[row_index].tolist()]
        mapping: dict[str, int] = {}
        for role, aliases in roles.items():
            wanted = {a.lower() for a in aliases}
            for col_index, cell in enumerate(cells):
                if cell in wanted and col_index not in mapping.values():
                    mapping[role] = col_index
                    break
        has_amount = "amount" in mapping or {"debit", "credit"} <= mapping.keys()
        if "date" in mapping and "description" in mapping and has_amount:
            return row_index, mapping
    raise BankFileError(
        f"no header row matching profile {profile.name!r} in the first {profile.header_search_rows} rows"
    )


def _row_amount(row: list[Any], mapping: dict[str, int]) -> Decimal | None:
    if "amount" in mapping:
        return parse_amount(row[mapping["amount"]])
    debit = parse_amount(row[mapping["debit"]]) or Decimal(0)
    credit = parse_amount(row[mapping["credit"]]) or Decimal(0)
    if debit == 0 and credit == 0:
        return None
    # Debit columns are written as positive numbers by most banks; money out is negative.
    return credit - abs(debit)


def transaction_id(date_text: str, amount: str, description: str, reference: str, n: int) -> str:
    """Stable id: importing the same statement twice yields the same ids.

    `n` counts earlier identical rows, so two genuine identical payments on the
    same day stay distinct instead of being collapsed as duplicates.
    """
    key = "|".join([date_text, amount, description, reference, str(n)])
    return "T" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def parse_bank_file(content: bytes, filename: str, profile: BankProfile) -> ParseResult:
    grid = _read_grid(content, filename, profile)
    header_row, mapping = _find_header(grid, profile)
    result = ParseResult(transactions=[])
    seen: Counter[str] = Counter()

    for row_index in range(header_row + 1, len(grid)):
        row = grid.iloc[row_index].tolist()
        line = row_index + 1
        if all(is_blank(v) for v in row):
            continue
        try:
            when = parse_date(row[mapping["date"]], profile.date_formats)
            amount = _row_amount(row, mapping)
        except ValueParseError as exc:
            # Totals and footer lines ("סה"כ", "Closing balance") land here.
            result.skipped_rows.append((line, str(exc)))
            continue
        if when is None or amount is None or amount == 0:
            result.skipped_rows.append((line, "missing date or amount"))
            continue

        description = clean_text(row[mapping["description"]])
        reference = clean_text(row[mapping["reference"]]) if "reference" in mapping else ""
        balance = None
        if "balance" in mapping:
            try:
                balance = parse_amount(row[mapping["balance"]])
            except ValueParseError:
                balance = None

        fingerprint = f"{when.isoformat()}|{amount}|{description}|{reference}"
        txn_id = transaction_id(when.isoformat(), str(amount), description, reference, seen[fingerprint])
        seen[fingerprint] += 1
        result.transactions.append(
            BankTransaction(
                id=txn_id,
                date=when,
                amount=amount,
                description=description,
                reference=reference or None,
                balance=balance,
            )
        )

    if not result.transactions:
        raise BankFileError("the file has a header but no transaction rows")
    return result
