from fastapi.testclient import TestClient

from tally.api import app
from tally.synthetic import bank_csv_hebrew, generate, ledger_csv

client = TestClient(app)
DATA = generate(seed=3, months=1)
BANK = bank_csv_hebrew(DATA.transactions)
LEDGER = ledger_csv(DATA.entries)


def test_health() -> None:
    assert client.get("/health").json()["status"] == "ok"


def test_profiles() -> None:
    names = {p["name"] for p in client.get("/profiles").json()}
    assert "hebrew_debit_credit" in names


def test_reconcile() -> None:
    response = client.post(
        "/reconcile",
        files={
            "bank_file": ("statement.csv", BANK, "text/csv"),
            "ledger_file": ("open_items.csv", LEDGER, "text/csv"),
        },
        data={"profile": "hebrew_debit_credit"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["transactions"] == len(DATA.transactions)
    assert body["summary"]["auto_matched_transactions"] > 0
    assert body["skipped_rows"]  # the totals line
    first = body["matches"][0]
    assert {"transaction_ids", "entry_ids", "status", "confidence", "reasons"} <= first.keys()


def test_unknown_profile_is_rejected() -> None:
    response = client.post(
        "/reconcile",
        files={"bank_file": ("s.csv", BANK, "text/csv"), "ledger_file": ("l.csv", LEDGER, "text/csv")},
        data={"profile": "../../etc/passwd"},
    )
    assert response.status_code == 422


def test_bad_file_gives_a_clear_error() -> None:
    response = client.post(
        "/reconcile",
        files={
            "bank_file": ("s.csv", b"hello,world\n1,2\n", "text/csv"),
            "ledger_file": ("l.csv", LEDGER, "text/csv"),
        },
    )
    assert response.status_code == 422
    assert "no header row" in response.json()["detail"]
