from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bios_mmc.fsutil import find_seven_zip  # noqa: E402


def _locate() -> Path | None:
    try:
        return find_seven_zip()
    except FileNotFoundError:
        return None


SEVEN_ZIP = _locate()
