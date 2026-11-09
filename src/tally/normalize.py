"""Text normalization for matching bank descriptions against counterparty names.

Bank descriptions are noisy: they are truncated to a fixed width, mix Hebrew and
English, add boilerplate ("העברה מ-", "TRANSFER FROM"), and drop punctuation.
Normalizing both sides the same way makes fuzzy comparison meaningful.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from rapidfuzz import fuzz

_FINAL_LETTERS = str.maketrans("ךםןףץ", "כמנפצ")
_QUOTES = str.maketrans({"״": '"', "׳": "'", "“": '"', "”": '"', "’": "'", "`": "'"})

# Legal suffixes carry no identity: "גלבוע בע"מ" and "גלבוע" are the same company.
_LEGAL_SUFFIXES = re.compile(
    r"\b(בע\"מ|בעמ|בע מ|ע\"ר|ער|שותפות מוגבלת|ltd|limited|inc|llc|co|corp|gmbh)\b\.?"
)

# Words the bank puts in front of the counterparty name. Only the leading phrase is
# removed: "TRANSFER FROM WIRE WORKS" keeps the company name "wire works".
_LEADING_BOILERPLATE = re.compile(
    r"^\s*(?:העברה בנקאית|העברה מחשבון|העברה מ|העברה ל|העב' מ|העב' ל|העב מ|העב ל|העברה|העב'|"
    r"זיכוי מס\"ב|חיוב מס\"ב|זיכוי|חיוב|מס\"ב|תשלום ל|תשלום מ|תשלום|הוראת קבע|הו\"ק|"
    r"transfer from|transfer to|transfer|payment from|payment to|payment|wire|ach|credit|debit)"
    r"(?=\W|$)[\s\-:]*"
)
# Labels that introduce a reference number, anywhere in the text.
_REFERENCE_LABELS = re.compile(r"(?<!\w)(?:ref|reference|inv|invoice|חשבונית|חש')(?=\W|$)")
_NON_WORD = re.compile(r"[^\w\s]")
_DIGIT_RUN = re.compile(r"\d{4,}")


def _strip_marks(text: str) -> str:
    # Remove Hebrew vowel points and other combining marks.
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


@lru_cache(maxsize=65536)
def normalize_name(text: str) -> str:
    """Canonical form of a counterparty name."""
    text = _strip_marks(text).translate(_QUOTES).lower()
    text = _LEGAL_SUFFIXES.sub(" ", text)
    text = _NON_WORD.sub(" ", text.replace("-", " "))
    text = re.sub(r"\d+", " ", text)
    return " ".join(text.translate(_FINAL_LETTERS).split())


@lru_cache(maxsize=65536)
def normalize_description(text: str) -> str:
    """Canonical form of a bank description, with bank boilerplate removed."""
    text = _strip_marks(text).translate(_QUOTES).lower()
    text = _LEADING_BOILERPLATE.sub(" ", text, count=1)
    text = _REFERENCE_LABELS.sub(" ", text)
    return normalize_name(text)


def reference_numbers(text: str) -> set[str]:
    """Digit runs of 4+ characters, e.g. invoice numbers quoted in a transfer.

    Runs are taken both as written ("260142-260143" quotes two invoices) and with
    separators removed ("2026-0142" is one document number).
    """
    joined = re.sub(r"(?<=\d)[-/](?=\d)", "", text)
    return set(_DIGIT_RUN.findall(text)) | set(_DIGIT_RUN.findall(joined))


def document_digits(number: str) -> str:
    """'INV-2026-0142' -> '20260142'."""
    return re.sub(r"\D", "", number)


def reference_hit(text: str, number: str) -> bool:
    """True if an invoice number appears in a bank description or reference field."""
    digits = document_digits(number)
    if len(digits) < 4:
        return False
    for run in reference_numbers(text):
        if run == digits:
            return True
        # Prefixes get dropped on both sides: "INV-2026-0142" may appear as "260142".
        if len(digits) >= 5 and run.endswith(digits):
            return True
        if len(run) >= 5 and digits.endswith(run):
            return True
    return False


def name_similarity(description: str, counterparty: str) -> float:
    """Similarity in [0, 1] between a bank description and a counterparty name.

    Handles the two failure modes that dominate real data: truncation (the bank
    cut the name after N characters) and reordering/extra words.
    """
    desc = normalize_description(description)
    name = normalize_name(counterparty)
    if not desc or not name:
        return 0.0
    name_words = name.split()
    if len(name_words) >= 2 and set(name_words) <= set(desc.split()):
        # Every word of a multi-word name is present, maybe with extra words around it.
        # (A one-word name like "כרמל" would match every "כרמל ..." company.)
        return 1.0
    # Word order varies between systems; compare sorted words.
    score = fuzz.token_sort_ratio(desc, name) / 100
    if 6 <= len(desc) < len(name):
        # The bank cut the name after N characters: compare with the same-length prefix.
        # (A plain substring search scores unrelated Hebrew names far too high.)
        score = max(score, fuzz.ratio(desc, name[: len(desc)]) / 100)
    return round(score, 4)
