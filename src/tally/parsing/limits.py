"""Size limits for untrusted uploads.

A 5 MB upload limit alone does not bound memory: a CSV with one very wide line
pads every row to that width, and an .xlsx is a zip that can expand enormously.
"""

from __future__ import annotations

import io
import zipfile

import pandas as pd

MAX_COLUMNS = 64
MAX_ROWS = 200_000
MAX_XLSX_UNCOMPRESSED = 50 * 1024 * 1024
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}


class FileTooLargeError(ValueError):
    pass


def check_grid(rows: int, columns: int) -> None:
    if columns > MAX_COLUMNS:
        raise FileTooLargeError(f"file has {columns} columns; at most {MAX_COLUMNS} are supported")
    if rows > MAX_ROWS:
        raise FileTooLargeError(f"file has {rows} rows; at most {MAX_ROWS} are supported")


def read_excel(content: bytes, header: int | None) -> pd.DataFrame:
    """Read an .xlsx after checking how far it would expand."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            expanded = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid .xlsx file") from exc
    if expanded > MAX_XLSX_UNCOMPRESSED:
        raise FileTooLargeError("spreadsheet expands to more than 50 MB")
    try:
        frame = pd.read_excel(io.BytesIO(content), header=header, dtype=object)
    except Exception as exc:  # openpyxl raises many exception types for corrupt files
        raise ValueError(f"could not read spreadsheet: {exc}") from exc
    check_grid(*frame.shape)
    return frame
