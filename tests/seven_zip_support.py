from __future__ import annotations

import os
import shutil
from pathlib import Path

import package_latest_bios as packager


SEVEN_ZIP_FILES = ("7z.exe", "7z.dll")


def find_seven_zip_directory() -> Path | None:
    candidates = [packager.DEFAULT_REPACKAGING_DIR / "7-Zip"]
    executable = shutil.which("7z.exe")
    if executable:
        candidates.append(Path(executable).parent)
    candidates.extend((
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip",
        Path.home() / "scoop" / "apps" / "7zip" / "current",
    ))
    return next(
        (directory for directory in candidates
         if all((directory / name).is_file() for name in SEVEN_ZIP_FILES)),
        None,
    )


SEVEN_ZIP_DIRECTORY = find_seven_zip_directory()


def copy_seven_zip(destination: Path) -> Path:
    if SEVEN_ZIP_DIRECTORY is None:
        raise FileNotFoundError("Integration tests require an existing native 7-Zip.")
    destination.mkdir(parents=True, exist_ok=True)
    for name in SEVEN_ZIP_FILES:
        shutil.copyfile(SEVEN_ZIP_DIRECTORY / name, destination / name)
    return destination / "7z.exe"
