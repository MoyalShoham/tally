from decimal import Decimal

from conftest import entry, txn
from tally.matching.engine import ReconciliationEngine
from tally.models import EntryKind, LedgerEntry, MatchKind, MatchStatus
from tally.resolver import ResolverDecision

ENGINE = ReconciliationEngine()


def only_match(txns, entries):  # type: ignore[no-untyped-def]
    report = ENGINE.reconcile(txns, entries)
    assert len(report.matches) == 1, report.matches
    return report.matches[0]


def test_clean_payment_is_auto_matched() -> None:
    m = only_match(
        [txn("T1", "2026-03-05", "1770.00", "העברה מ-גלבוע ציוד מש")],
        [
            entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03"),
            entry("I2", "כרמל תוכנה", "1770.00", "2026-02-01", "2026-03-03"),
        ],
    )
    assert (m.entry_ids, m.status, m.kind) == (["I1"], MatchStatus.AUTO, MatchKind.ONE_TO_ONE)
    assert "amount exact" in m.reasons


def test_vendor_bill_matches_outgoing_payment_only() -> None:
    bill = entry(
        "B1", "תקשורת פלוס", "1250.00", "2026-02-01", "2026-02-10", number="88123", kind=EntryKind.BILL
    )
    incoming = ENGINE.reconcile([txn("T1", "2026-02-10", "1250.00", "תקשורת פלוס")], [bill])
    assert incoming.matches == []
    outgoing = ENGINE.reconcile([txn("T1", "2026-02-10", "-1250.00", 'הו"ק תקשורת פלוס')], [bill])
    assert outgoing.matches[0].status is MatchStatus.AUTO


def test_twin_invoices_go_to_review() -> None:
    m = only_match(
        [txn("T1", "2026-03-04", "566.40", "ארז תכנון פיננסי")],
        [
            entry("I1", "ארז תכנון פיננסי", "566.40", "2026-02-20", "2026-03-01"),
            entry("I2", "ארז תכנון פיננסי", "566.40", "2026-02-25", "2026-03-06"),
        ],
    )
    assert m.status is MatchStatus.REVIEW
    assert any("another open entry" in r for r in m.reasons)
    assert len(m.alternative_entry_ids) == 1


def test_last_months_rent_is_not_a_twin() -> None:
    rent = [
        entry(
            f"B{m}",
            "משרדי הצפון נכסים",
            "1000.00",
            f"2026-0{m}-01",
            f"2026-0{m}-10",
            number=f"{m}0000",
            kind=EntryKind.BILL,
        )
        for m in (1, 2, 3)
    ]
    report = ENGINE.reconcile([txn("T1", "2026-02-10", "-1000.00", 'הו"ק משרדי הצפון נכס')], rent)
    assert report.matches[0].entry_ids == ["B2"]
    assert report.matches[0].status is MatchStatus.AUTO


def test_quoted_number_breaks_a_tie() -> None:
    m = only_match(
        [txn("T1", "2026-03-04", "566.40", "ארז תכנון פיננסי INV-260002")],
        [
            entry("I1", "ארז תכנון פיננסי", "566.40", "2026-02-20", "2026-03-01", "INV-260001"),
            entry("I2", "ארז תכנון פיננסי", "566.40", "2026-02-25", "2026-03-06", "INV-260002"),
        ],
    )
    assert m.entry_ids == ["I2"]
    assert m.status is MatchStatus.AUTO


def test_fee_deducted_payment_matches() -> None:
    m = only_match(
        [txn("T1", "2026-03-05", "1762.50", "העברה מ-גלבוע ציוד מש")],
        [entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03")],
    )
    assert m.entry_ids == ["I1"]
    assert m.amount_difference == Decimal("-7.50")


def test_overpayment_is_not_auto() -> None:
    report = ENGINE.reconcile(
        [txn("T1", "2026-03-05", "1785.00", "העברה מ-גלבוע ציוד מש")],
        [entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03")],
    )
    assert all(m.status is not MatchStatus.AUTO for m in report.matches)


def test_amount_and_date_alone_never_auto_match() -> None:
    report = ENGINE.reconcile(
        [txn("T1", "2026-03-03", "1770.00", "העברה בנקאית")],
        [entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03")],
    )
    assert all(m.status is not MatchStatus.AUTO for m in report.matches)


def test_payment_before_invoice_issue_is_rejected() -> None:
    report = ENGINE.reconcile(
        [txn("T1", "2026-01-15", "1770.00", "גלבוע ציוד משרדי")],
        [entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03")],
    )
    assert report.matches == []
    assert report.summary.unmatched_transactions == 1


def test_split_payment() -> None:
    m = only_match(
        [txn("T1", "2026-03-20", "4130.00", "העברה מ-שקד ייעוץ ומחקר")],
        [
            entry("I1", "שקד ייעוץ ומחקר", "1770.00", "2026-02-01", "2026-03-03"),
            entry("I2", "שקד ייעוץ ומחקר", "2360.00", "2026-02-10", "2026-03-12"),
            entry("I3", "שקד ייעוץ ומחקר", "590.00", "2026-02-15", "2026-03-17"),
        ],
    )
    assert m.kind is MatchKind.SPLIT
    assert sorted(m.entry_ids) == ["I1", "I2"]
    assert m.status is MatchStatus.AUTO


def test_installments() -> None:
    invoice = entry("I1", "תמר שיווק דיגיטלי", "1699.20", "2026-05-20", "2026-06-18")
    report = ENGINE.reconcile(
        [
            txn("T1", "2026-06-18", "679.68", 'זיכוי מס"ב תמר שיווק דיגיטלי'),
            txn("T2", "2026-07-18", "1019.52", 'זיכוי מס"ב תמר שיווק דיגיטלי'),
        ],
        [invoice],
    )
    kinds = [(m.transaction_ids, m.entry_ids, m.kind) for m in report.matches]
    assert kinds == [(["T1"], ["I1"], MatchKind.PARTIAL), (["T2"], ["I1"], MatchKind.PARTIAL)]
    assert "final installment" in report.matches[1].reasons[0]
    assert report.summary.unmatched_entries == 0


def test_bank_fees_stay_unmatched() -> None:
    report = ENGINE.reconcile(
        [txn("T1", "2026-03-05", "-6.50", "עמלת ערוץ ישיר")],
        [entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03")],
    )
    assert report.unmatched_transaction_ids == ["T1"]
    assert report.unmatched_entry_ids == ["I1"]


class FakeResolver:
    def __init__(self, pick: str) -> None:
        self.pick = pick
        self.calls = 0

    def resolve(self, txn, options: list[LedgerEntry]) -> ResolverDecision:  # type: ignore[no-untyped-def]
        self.calls += 1
        return ResolverDecision(entry_id=self.pick, rationale="test")


def test_resolver_reorders_but_never_promotes() -> None:
    txns = [txn("T1", "2026-03-04", "566.40", "ארז תכנון פיננסי")]
    entries = [
        entry("I1", "ארז תכנון פיננסי", "566.40", "2026-02-20", "2026-03-01"),
        entry("I2", "ארז תכנון פיננסי", "566.40", "2026-02-25", "2026-03-06"),
    ]
    plain = ReconciliationEngine().reconcile(txns, entries).matches[0]
    other = plain.alternative_entry_ids[0]
    resolver = FakeResolver(other)
    m = ReconciliationEngine(resolver=resolver).reconcile(txns, entries).matches[0]
    assert resolver.calls == 1
    assert m.status is MatchStatus.REVIEW
    assert m.entry_ids == plain.entry_ids  # the suggestion itself is untouched
    assert m.alternative_entry_ids[0] == other
    assert any("second opinion prefers" in r for r in m.reasons)


def test_summary_counts() -> None:
    report = ENGINE.reconcile(
        [
            txn("T1", "2026-03-05", "1770.00", "העברה מ-גלבוע ציוד מש"),
            txn("T2", "2026-03-05", "-6.50", "עמלת ערוץ ישיר"),
        ],
        [entry("I1", 'גלבוע ציוד משרדי בע"מ', "1770.00", "2026-02-01", "2026-03-03")],
    )
    s = report.summary
    assert (s.auto_matched_transactions, s.unmatched_transactions, s.unmatched_entries) == (1, 1, 0)
    assert s.auto_match_rate == 0.5
