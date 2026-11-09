"""Synthetic but realistic test data with ground truth.

Real bank statements cannot be published, so the benchmark uses a generator that
reproduces the patterns that make reconciliation hard in practice:

- descriptions truncated by the bank, with boilerplate and occasional typos
- the same customer paying the same retainer every month
- different customers with similar names and identical amounts
- transfer fees deducted from incoming payments
- one transfer paying several invoices, and invoices paid in installments
- invoices that are never paid, and bank lines with no ledger entry at all
- transfers whose description says nothing about the payer

Everything is seeded, so a given seed always produces the same files.
"""

from __future__ import annotations

import csv
import io
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from tally.models import BankTransaction, EntryKind, LedgerEntry
from tally.parsing.bank import transaction_id

VAT = Decimal("1.18")

CUSTOMERS = [
    'גלבוע ציוד משרדי בע"מ',
    'כרמל תוכנה בע"מ',
    'כרמל תעשיות מתכת בע"מ',
    "שקד ייעוץ ומחקר",
    'אורן הנדסה אזרחית בע"מ',
    "אורן הפקות",
    'תמר שיווק דיגיטלי בע"מ',
    "ברוש לוגיסטיקה",
    'נחשון מערכות מידע בע"מ',
    "רימון סטודיו לעיצוב",
    'דקל חומרי בניין בע"מ',
    "יעל קליניקה",
    'הדר מזון ומשקאות בע"מ',
    "ארז תכנון פיננסי",
    'שחר מחשוב ענן בע"מ',
    "גפן אירועים",
    'מעיין מים ומיזוג בע"מ',
    "צור ייבוא ושיווק",
    "Negev Logistics Ltd",
    "Harbor Analytics Ltd",
    "Blue Cedar Software Ltd",
    "Blue Cedar Consulting",
    "Galilee Foods Ltd",
    "Orion Medical Ltd",
    'ליבנה רהיטים בע"מ',
    "אלון שירותי ניקיון",
    'סלע קבלנות בע"מ',
    "ניר ושות' רואי חשבון",
    'פלג אנרגיה סולארית בע"מ',
    "עדן תיירות",
    'יהלום תכשיטים בע"מ',
    "מור ביטוחים",
]
VENDORS = [
    'משרדי הצפון נכסים בע"מ',
    'תקשורת פלוס בע"מ',
    "ענבל ספקי נייר",
    'אמיר הובלות בע"מ',
    "Cloudline Hosting Ltd",
    "Paperstack Inc",
    'רון ציוד היקפי בע"מ',
    'בית דפוס אלה בע"מ',
    "ניקיון ברק",
    'חשמל וגז השרון בע"מ',
    "Northwind Supplies Ltd",
    "קפה מאיה",
    'לביא אבטחה בע"מ',
    "רואה החשבון גל לוי",
    'מירב שירותי משרד בע"מ',
    "Zenith Software Inc",
]
NOISE = [
    ("עמלת ערוץ ישיר", -1, (5, 25)),
    ("עמלת העברה", -1, (6, 15)),
    ("משכורת", -1, (7000, 21000)),
    ('מע"מ', -1, (3000, 30000)),
    ("ביטוח לאומי", -1, (1500, 9000)),
    ("מס הכנסה ניכויים", -1, (1000, 8000)),
    ("כרטיס אשראי ויזה", -1, (800, 9000)),
    ("ריבית זכות", 1, (1, 40)),
    ("העברה ממשתמש bit", 1, (50, 900)),
    ("הפקדת מזומן", 1, (200, 5000)),
    ("החזר הלוואה", -1, (2500, 2500)),
]


class Scenario(StrEnum):
    CLEAN = "clean"
    REFERENCE = "reference_quoted"
    FEE = "fee_deducted"
    SPLIT = "split_payment"
    PARTIAL = "installments"
    OPAQUE = "no_payer_in_description"
    UNPAID = "unpaid"
    NOISE = "no_ledger_entry"


@dataclass(frozen=True)
class TruthLink:
    transaction_id: str
    entry_ids: tuple[str, ...]
    scenario: Scenario


@dataclass
class Dataset:
    transactions: list[BankTransaction]
    entries: list[LedgerEntry]
    truth: list[TruthLink]
    noise_transaction_ids: set[str] = field(default_factory=set)
    unpaid_entry_ids: set[str] = field(default_factory=set)


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Generator:
    def __init__(self, seed: int = 7, start: date = date(2026, 1, 1), months: int = 6) -> None:
        self.rng = random.Random(seed)
        self.start = start
        self.months = months
        self.end = start + timedelta(days=30 * months)
        self._seq: Counter[str] = Counter()
        self._raw: list[tuple[date, Decimal, str, str, Scenario, tuple[str, ...]]] = []
        self.entries: list[LedgerEntry] = []
        self.unpaid: set[str] = set()

    # --- ledger -------------------------------------------------------
    def _price(self) -> Decimal:
        base = self.rng.choice([350, 480, 750, 1200, 1500, 2400, 3200, 4500, 6800, 9900, 12500])
        base *= self.rng.choice([1, 1, 1, 2, 3])
        if self.rng.random() < 0.5:
            base += self.rng.randint(1, 40) * 10
        return _money(Decimal(base) * VAT)

    def _entry(self, kind: EntryKind, party: str, amount: Decimal, issued: date, terms: int) -> LedgerEntry:
        prefix = "INV" if kind is EntryKind.INVOICE else "BILL"
        self._seq[prefix] += 1
        number = (
            f"INV-26{self._seq[prefix]:04d}"
            if kind is EntryKind.INVOICE
            else f"{self.rng.randint(1000, 99999)}"
        )
        entry = LedgerEntry(
            id=f"{prefix[0]}{self._seq[prefix]:05d}",
            kind=kind,
            number=number,
            counterparty=party,
            amount=amount,
            issue_date=issued,
            due_date=issued + timedelta(days=terms),
        )
        self.entries.append(entry)
        return entry

    def _ledger(self) -> None:
        retainers = self.rng.sample(CUSTOMERS, 12)
        retainer_price = {c: self._price() for c in retainers}
        # Two customers with similar names share the same retainer amount on purpose.
        retainer_price[retainers[1]] = retainer_price[retainers[0]]
        for month in range(self.months):
            first = self.start + timedelta(days=30 * month)
            for customer in retainers:
                self._entry(
                    EntryKind.INVOICE,
                    customer,
                    retainer_price[customer],
                    first + timedelta(days=self.rng.randint(0, 4)),
                    30,
                )
            for _ in range(self.rng.randint(205, 230)):
                self._entry(
                    EntryKind.INVOICE,
                    self.rng.choice(CUSTOMERS),
                    self._price(),
                    first + timedelta(days=self.rng.randint(0, 27)),
                    self.rng.choice([0, 14, 30, 30, 45, 60]),
                )
            for vendor in VENDORS[:4]:  # rent, telecom, ... fixed monthly bills
                self._entry(EntryKind.BILL, vendor, _money(1000 + 250 * VENDORS.index(vendor)), first, 10)
            for _ in range(self.rng.randint(72, 88)):
                self._entry(
                    EntryKind.BILL,
                    self.rng.choice(VENDORS),
                    self._price(),
                    first + timedelta(days=self.rng.randint(0, 27)),
                    self.rng.choice([0, 30, 30, 60]),
                )

    # --- bank side ----------------------------------------------------
    def _describe(self, party: str, incoming: bool, opaque: bool = False) -> str:
        if opaque:
            return self.rng.choice(
                ["העברה בנקאית", 'זיכוי מס"ב', "העברה מחשבון אחר"]
                if incoming
                else ['חיוב מס"ב', "העברה בנקאית"]
            )
        name = party
        if self.rng.random() < 0.06 and len(name) > 6:  # typo: one character dropped
            i = self.rng.randrange(1, len(name) - 1)
            name = name[:i] + name[i + 1 :]
        english = party.isascii()
        if english:
            verb = "TRANSFER FROM " if incoming else self.rng.choice(["PAYMENT TO ", "TRANSFER TO "])
            return (verb + name.upper())[: self.rng.choice([24, 28, 32])]
        if incoming:
            verb = self.rng.choice(["העברה מ-", 'זיכוי מס"ב ', "העב' מ-", ""])
        else:
            verb = self.rng.choice(["העברה ל-", 'חיוב מס"ב ', 'הו"ק '])
        width = self.rng.choice([18, 20, 22, 26])
        return verb + name[:width].rstrip()

    def _pay_day(self, entry: LedgerEntry) -> date:
        lag = int(self.rng.triangular(-12, 25, 2))
        return max(entry.issue_date, entry.due_date + timedelta(days=lag))

    def _add(
        self,
        when: date,
        amount: Decimal,
        description: str,
        scenario: Scenario,
        entry_ids: tuple[str, ...],
        reference: str = "",
    ) -> None:
        if not reference:
            reference = str(self.rng.randint(10000, 999999))
        description = " ".join(description.split())
        self._raw.append((when, amount, description, reference, scenario, entry_ids))

    def _payments(self) -> None:
        by_party: dict[tuple[EntryKind, str], list[LedgerEntry]] = {}
        for e in self.entries:
            by_party.setdefault((e.kind, e.counterparty), []).append(e)

        handled: set[str] = set()
        # Split payments: a customer settles 2-4 open invoices with one transfer.
        for (kind, party), items in by_party.items():
            if kind is not EntryKind.INVOICE:
                continue
            items = sorted(items, key=lambda e: e.issue_date)
            for i in range(0, len(items) - 3, 9):
                if self.rng.random() < 0.55:
                    group = items[i : i + self.rng.randint(2, 4)]
                    when = max(self._pay_day(e) for e in group)
                    total = sum((e.amount for e in group), Decimal(0))
                    self._add(
                        when, total, self._describe(party, True), Scenario.SPLIT, tuple(e.id for e in group)
                    )
                    handled.update(e.id for e in group)

        for e in self.entries:
            if e.id in handled:
                continue
            incoming = e.kind is EntryKind.INVOICE
            sign = 1 if incoming else -1
            roll = self.rng.random()
            when = self._pay_day(e)
            if when > self.end + timedelta(days=20) or roll < 0.05:
                self.unpaid.add(e.id)
                continue
            if roll < 0.62:
                self._add(
                    when, sign * e.amount, self._describe(e.counterparty, incoming), Scenario.CLEAN, (e.id,)
                )
            elif roll < 0.74:
                self._add(
                    when,
                    sign * e.amount,
                    f"{self._describe(e.counterparty, incoming)} {e.number}"[:40],
                    Scenario.REFERENCE,
                    (e.id,),
                )
            elif roll < 0.84 and incoming:
                fee = _money(self.rng.choice([5, 7.5, 10, 12, 15, 20]))
                self._add(when, e.amount - fee, self._describe(e.counterparty, True), Scenario.FEE, (e.id,))
            elif roll < 0.90 and e.amount > 1000:
                first = _money(e.amount * Decimal(self.rng.choice(["0.3", "0.4", "0.5"])))
                second = e.amount - first
                quote = self.rng.random() < 0.5
                for part, day in ((first, when), (second, when + timedelta(days=30))):
                    text = self._describe(e.counterparty, incoming)
                    if quote:
                        text = f"{text} {e.number}"[:40]
                    self._add(day, sign * part, text, Scenario.PARTIAL, (e.id,))
            elif roll < 0.95:
                self._add(
                    when,
                    sign * e.amount,
                    self._describe(e.counterparty, incoming, True),
                    Scenario.OPAQUE,
                    (e.id,),
                )
            else:
                self._add(
                    when, sign * e.amount, self._describe(e.counterparty, incoming), Scenario.CLEAN, (e.id,)
                )

        day = self.start
        while day < self.end:
            for text, sign, (low, high) in NOISE:
                if self.rng.random() < (0.35 if low < 100 else 0.06):
                    amount = _money(self.rng.uniform(low, high)) if low != high else _money(low)
                    self._add(day, sign * amount, text, Scenario.NOISE, ())
            day += timedelta(days=1)

    def build(self) -> Dataset:
        self._ledger()
        self._payments()
        self._raw.sort(key=lambda r: (r[0], r[2]))
        seen: Counter[str] = Counter()
        txns, truth, noise = [], [], set()
        for when, amount, description, reference, scenario, entry_ids in self._raw:
            amount = _money(amount)
            fingerprint = f"{when.isoformat()}|{amount}|{description}|{reference}"
            tid = transaction_id(when.isoformat(), str(amount), description, reference, seen[fingerprint])
            seen[fingerprint] += 1
            txns.append(
                BankTransaction(
                    id=tid, date=when, amount=amount, description=description, reference=reference
                )
            )
            if scenario is Scenario.NOISE:
                noise.add(tid)
            else:
                truth.append(TruthLink(tid, entry_ids, scenario))
        return Dataset(txns, self.entries, truth, noise, self.unpaid)


def generate(seed: int = 7, months: int = 6) -> Dataset:
    return Generator(seed=seed, months=months).build()


# --- writers: produce files in the formats the parsers read -------------------
def bank_csv_hebrew(transactions: list[BankTransaction]) -> bytes:
    """Write transactions as a Hebrew debit/credit export, with title and total lines."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["תנועות בחשבון 12-345-678901"])
    writer.writerow(["הופק בתאריך", date.today().strftime("%d/%m/%Y")])
    writer.writerow([])
    writer.writerow(["תאריך", "תיאור", "אסמכתא", "חובה", "זכות", "יתרה"])
    balance = Decimal("50000")
    for t in transactions:
        balance += t.amount
        debit = f"{-t.amount:,.2f}" if t.amount < 0 else ""
        credit = f"{t.amount:,.2f}" if t.amount > 0 else ""
        writer.writerow(
            [t.date.strftime("%d/%m/%Y"), t.description, t.reference or "", debit, credit, f"{balance:,.2f}"]
        )
    writer.writerow([])
    writer.writerow(['סה"כ', "", "", "", "", f"{balance:,.2f}"])
    return out.getvalue().encode("utf-8-sig")


def ledger_csv(entries: list[LedgerEntry]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "kind", "number", "counterparty", "amount", "issue_date", "due_date"])
    for e in entries:
        writer.writerow(
            [
                e.id,
                e.kind.value,
                e.number,
                e.counterparty,
                f"{e.amount:.2f}",
                e.issue_date.isoformat(),
                e.due_date.isoformat(),
            ]
        )
    return out.getvalue().encode("utf-8-sig")
