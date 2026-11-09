import pytest

from tally.normalize import (
    document_digits,
    name_similarity,
    normalize_description,
    normalize_name,
    reference_hit,
)


def test_normalize_name_drops_legal_suffix_and_final_letters() -> None:
    assert normalize_name('גלבוע ציוד משרדי בע"מ') == "גלבוע ציוד משרדי"
    assert normalize_name("Negev Logistics Ltd.") == "negev logistics"
    assert normalize_name("שלום") == normalize_name("שלומ")


def test_normalize_description_drops_bank_boilerplate() -> None:
    assert normalize_description("העברה מ-גלבוע ציוד מש") == "גלבוע ציוד מש"
    assert normalize_description('זיכוי מס"ב שקד ייעוץ') == "שקד ייעוצ"
    assert normalize_description("TRANSFER FROM NEGEV LOGI") == "negev logi"


@pytest.mark.parametrize(
    ("description", "name"),
    [
        ("העברה מ-גלבוע ציוד מש", 'גלבוע ציוד משרדי בע"מ'),  # truncated
        ("TRANSFER FROM NEGEV LOGI INV-260170", "Negev Logistics Ltd"),
        ('העברה מ-כרמל תוכנ בע"מ', 'כרמל תוכנה בע"מ'),  # typo
        ('הו"ק משרדי הצפון נכס', 'משרדי הצפון נכסים בע"מ'),
    ],
)
def test_same_party_scores_high(description: str, name: str) -> None:
    assert name_similarity(description, name) >= 0.9


@pytest.mark.parametrize(
    ("description", "name"),
    [
        ("מור ביטוחים INV-260364", 'דקל חומרי בניין בע"מ'),
        ('זיכוי מס"ב תמר שיווק דיגיטלי', "מור ביטוחים"),
        ("העברה מ-כרמל תוכנה", 'כרמל תעשיות מתכת בע"מ'),  # similar names, different company
        ("העברה בנקאית", "Negev Logistics Ltd"),
    ],
)
def test_different_party_scores_low(description: str, name: str) -> None:
    assert name_similarity(description, name) < 0.75


def test_document_digits() -> None:
    assert document_digits("INV-2026-0142") == "20260142"


def test_reference_hit() -> None:
    assert reference_hit("העברה חשבונית 260142", "INV-260142")
    assert reference_hit("pay 2026-0142", "INV-2026-0142")
    assert reference_hit("payment 20260142", "INV-0142") is False  # 4 digits need an exact hit
    assert not reference_hit("pay 1234", "INV-260142")
    assert not reference_hit("pay 260142", "INV-12")
