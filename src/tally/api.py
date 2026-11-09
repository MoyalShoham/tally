"""HTTP API.

Run locally:  uvicorn tally.api:app --reload
Docs:         http://localhost:8000/docs
"""

from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from tally import __version__
from tally.matching.engine import ReconciliationEngine
from tally.models import ReconciliationReport
from tally.parsing.bank import BankFileError, parse_bank_file
from tally.parsing.ledger import LedgerFileError, parse_ledger_file
from tally.parsing.profiles import builtin_profiles, load_profile
from tally.resolver import Resolver

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

app = FastAPI(
    title="Tally",
    version=__version__,
    description="Matches bank statement lines to open invoices and bills.",
)


class ProfileInfo(BaseModel):
    name: str
    description: str


class ReconcileResponse(ReconciliationReport):
    skipped_rows: list[tuple[int, str]]


def _resolver() -> Resolver | None:
    if os.environ.get("TALLY_USE_LLM") != "1":
        return None
    from tally.resolver import ClaudeResolver

    return ClaudeResolver()


def _read(upload: UploadFile) -> bytes:
    content = upload.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{upload.filename} is larger than 5 MB")
    return content


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/profiles", response_model=list[ProfileInfo])
def profiles() -> list[ProfileInfo]:
    return [ProfileInfo(name=p.name, description=p.description) for p in builtin_profiles()]


# A plain `def` endpoint: FastAPI runs it in a worker thread, so CPU-bound
# matching and optional LLM calls do not block the event loop for other clients.
@app.post("/reconcile", response_model=ReconcileResponse)
def reconcile(
    bank_file: UploadFile = File(..., description="Bank statement export (CSV or XLSX)"),
    ledger_file: UploadFile = File(..., description="Open invoices and bills (CSV or XLSX)"),
    profile: str = Form("hebrew_debit_credit", description="Bank export profile name"),
) -> ReconcileResponse:
    if "/" in profile or "\\" in profile or "." in profile:
        raise HTTPException(422, "profile must be a built-in profile name")
    try:
        bank_profile = load_profile(profile)
    except FileNotFoundError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        parsed = parse_bank_file(_read(bank_file), bank_file.filename or "bank.csv", bank_profile)
        entries = parse_ledger_file(_read(ledger_file), ledger_file.filename or "ledger.csv")
    except (BankFileError, LedgerFileError) as exc:
        raise HTTPException(422, str(exc)) from exc

    report = ReconciliationEngine(resolver=_resolver()).reconcile(parsed.transactions, entries)
    return ReconcileResponse(**report.model_dump(), skipped_rows=parsed.skipped_rows)
