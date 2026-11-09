"""Bank export profiles.

Every bank exports statements a little differently: Hebrew or English headers, one
signed amount column or separate debit/credit columns, title rows above the
header, totals below the data. A profile describes one layout declaratively, so
supporting a new bank means adding a JSON file, not writing code.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class ColumnAliases(BaseModel):
    date: list[str]
    description: list[str]
    amount: list[str] = Field(default_factory=list)  # one signed column ...
    debit: list[str] = Field(default_factory=list)  # ... or a debit/credit pair
    credit: list[str] = Field(default_factory=list)
    reference: list[str] = Field(default_factory=list)
    balance: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _amount_columns(self) -> ColumnAliases:
        if not self.amount and not (self.debit and self.credit):
            raise ValueError("a profile needs an amount column or both debit and credit columns")
        return self


class BankProfile(BaseModel):
    name: str
    description: str
    encodings: list[str] = Field(default_factory=lambda: ["utf-8-sig", "cp1255"])
    date_formats: list[str] = Field(default_factory=lambda: ["%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%Y-%m-%d"])
    columns: ColumnAliases
    header_search_rows: int = 30


def _builtin_dir() -> Path:
    return Path(str(resources.files("tally") / "profiles"))


def load_profile(name_or_path: str) -> BankProfile:
    """Load a built-in profile by name, or any profile JSON file by path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _builtin_dir() / f"{name_or_path}.json"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in _builtin_dir().glob("*.json")))
        raise FileNotFoundError(f"unknown profile {name_or_path!r}; built-in: {available}")
    return BankProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))


def builtin_profiles() -> list[BankProfile]:
    return [load_profile(p.stem) for p in sorted(_builtin_dir().glob("*.json"))]
