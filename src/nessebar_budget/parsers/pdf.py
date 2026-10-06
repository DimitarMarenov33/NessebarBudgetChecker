"""PDF parser stub.

TODO: implement extraction of tabular budget/procurement data from PDF
documents (using pdfplumber/pypdf) into plain dicts or pandas DataFrames.
No implementation yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def parse_pdf(path: Path) -> list[dict[str, Any]]:
    raise NotImplementedError("parse_pdf is not implemented yet")
