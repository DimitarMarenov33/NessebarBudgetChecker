"""XLSX parser stub.

TODO: implement extraction of tabular budget/procurement data from XLSX
workbooks (using openpyxl/pandas) into plain dicts or pandas DataFrames.
No implementation yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def parse_xlsx(path: Path) -> list[dict[str, Any]]:
    raise NotImplementedError("parse_xlsx is not implemented yet")
