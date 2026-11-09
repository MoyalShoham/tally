"""Index of document numbers, to find which open entries a transfer quotes.

A transfer that quotes invoice 260284 is evidence *for* that invoice and
*against* every other one, even if another invoice has a closer amount.
"""

from __future__ import annotations

from collections import defaultdict

from tally.models import LedgerEntry
from tally.normalize import document_digits, reference_numbers

MIN_SUFFIX = 5


class DocumentIndex:
    def __init__(self, entries: list[LedgerEntry]) -> None:
        self._exact: dict[str, set[str]] = defaultdict(set)
        self._suffix: dict[str, set[str]] = defaultdict(set)
        for entry in entries:
            digits = document_digits(entry.number)
            if len(digits) < 4:
                continue
            self._exact[digits].add(entry.id)
            for k in range(MIN_SUFFIX, len(digits)):
                self._suffix[digits[-k:]].add(entry.id)

    def quoted(self, text: str) -> set[str]:
        """Ids of entries whose number appears in `text` (same rules as reference_hit)."""
        found: set[str] = set()
        for run in reference_numbers(text):
            found |= self._exact.get(run, set())
            if len(run) >= MIN_SUFFIX:
                found |= self._suffix.get(run, set())  # the number ends with the quoted digits
                for k in range(MIN_SUFFIX, len(run)):  # the quoted digits end with the number
                    found |= self._exact.get(run[-k:], set())
        return found
