"""Read open invoices and bills exported from an ERP or accounting system."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from tally.models import EntryKind, LedgerEntry
from tally.parsing.limits import EXCEL_SUFFIXES, check_grid, read_excel
from tally.parsing.values import ValueParseError, clean_text, parse_amount, parse_date

REQUIRED_COLUMNS = ("id", "kind", "number", "counterparty", "amount", "issue_date", "due_date")
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y")
ENCODINGS = ("utf-8-sig", "cp1255")
SUPPORTED_CURRENCIES = {"ILS", "NIS", "₪"}
_KIND_ALIASES = {
    "invoice": EntryKind.INVOICE,
    "receivable": EntryKind.INVOICE,
    "ar": EntryKind.INVOICE,
    "bill": EntryKind.BILL,
    "payable": EntryKind.BILL,
    "ap": EntryKind.BILL,
}


class LedgerFileError(ValueError):
    pass


def _read_frame(content: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        raise LedgerFileError("old .xls files are not supported; save the file as .xlsx or .csv")
    try:
        if suffix in EXCEL_SUFFIXES:
            return read_excel(content, header=0)
        for encoding in ENCODINGS:
            try:
                text = content.decode(encoding)
            except UnicodeDecodeError:
                continue
            frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
            check_grid(*frame.shape)
            return frame
    except (ValueError, pd.errors.ParserError) as exc:
        raise LedgerFileError(str(exc)) from exc
    raise LedgerFileError(f"could not decode {filename} as {' or '.join(ENCODINGS)}")


def parse_ledger_file(content: bytes, filename: str) -> list[LedgerEntry]:
    frame = _read_frame(content, filename)
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise LedgerFileError(f"ledger file is missing columns: {', '.join(missing)}")

    entries: list[LedgerEntry] = []
    for position, record in enumerate(frame.to_dict("records"), start=2):
        try:
            kind = _KIND_ALIASES[clean_text(record["kind"]).lower()]
            amount = parse_amount(record["amount"])
            issue = parse_date(record["issue_date"], DATE_FORMATS)
            due = parse_date(record["due_date"], DATE_FORMATS)
            if amount is None or issue is None or due is None:
                raise LedgerFileError(f"line {position}: amount and dates are required")
            if amount <= 0:
                raise LedgerFileError(
                    f"line {position}: amount must be positive (credit notes are not supported yet)"
                )
            currency = (clean_text(record.get("currency")) or "ILS").upper()
            if currency not in SUPPORTED_CURRENCIES:
                raise LedgerFileError(f"line {position}: only ILS entries are supported, got {currency}")
            entries.append(
                LedgerEntry(
                    id=clean_text(record["id"]),
                    kind=kind,
                    number=clean_text(record["number"]),
                    counterparty=clean_text(record["counterparty"]),
                    amount=amount,
                    issue_date=issue,
                    due_date=due,
                    currency="ILS",
                )
            )
        except KeyError as exc:
            raise LedgerFileError(f"line {position}: unknown kind {record['kind']!r}") from exc
        except (ValueParseError, ValidationError) as exc:
            raise LedgerFileError(f"line {position}: {exc}") from exc

    ids = [e.id for e in entries]
    if len(ids) != len(set(ids)):
        raise LedgerFileError("ledger ids must be unique")
    return entries
