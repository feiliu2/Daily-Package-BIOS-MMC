"""File system and 7-Zip helpers shared by BIOS and MMC packaging."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from . import LOGGER

SEVEN_ZIP_NAMES = ("7zz", "7z", "7za")
SEVEN_ZIP_TIMEOUT = 1800


@dataclass(frozen=True)
class ArchiveEntry:
    path: PureWindowsPath
    size: int
    crc: str
    directory: bool
    link: bool


def find_seven_zip(explicit: Path | None = None) -> Path:
    """Locate a native 7-Zip executable on Windows or Linux."""
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"7-Zip executable not found: {explicit}")
        return explicit.resolve()
    for name in SEVEN_ZIP_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base and (Path(base) / "7-Zip" / "7z.exe").is_file():
                return (Path(base) / "7-Zip" / "7z.exe").resolve()
    raise FileNotFoundError(
        "7-Zip not found. Install 7-Zip (Linux: the '7zip' or 'p7zip-full' package) "
        "or pass --seven-zip."
    )


def run_7zip(
    seven_zip: Path, arguments: list[str], *, cwd: Path | None = None
) -> str:
    result = subprocess.run(
        [str(seven_zip), *arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SEVEN_ZIP_TIMEOUT,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"7-Zip failed ({result.returncode}): "
            f"{subprocess.list2cmdline(arguments)}\n"
            f"{result.stdout}"
        )
    return result.stdout


def validate_archive(archive: Path, seven_zip: Path) -> None:
    if not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError(f"Archive is missing or empty: {archive}")
    run_7zip(seven_zip, ["t", "-bso0", "-bsp0", "-y", "--", str(archive)])


def extract_archive(archive: Path, destination: Path, seven_zip: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    run_7zip(
        seven_zip,
        ["x", "-y", "-bso0", "-bsp0", f"-o{destination}", "--", str(archive)],
    )


def create_archive(
    source_dir: Path, archive: Path, seven_zip: Path,
    *, archive_threads: int | None = None,
) -> None:
    """Archive the children of source_dir so they become the archive root."""
    if archive_threads is not None and archive_threads < 1:
        raise ValueError("--archive-threads must be at least 1.")
    thread_options = [] if archive_threads is None else [f"-mmt={archive_threads}"]
    children = sorted(child.name for child in source_dir.iterdir())
    if not children:
        raise ValueError(f"Nothing to archive in {source_dir}")
    run_7zip(
        seven_zip,
        ["a", "-t7z", "-y", "-bso0", "-bsp0", *thread_options,
         "--", str(archive), *children],
        cwd=source_dir,
    )


def list_archive(archive: Path, seven_zip: Path) -> list[ArchiveEntry]:
    listing = run_7zip(
        seven_zip, ["l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)]
    )
    entries: list[ArchiveEntry] = []
    for block in re.split(r"\r?\n\s*\r?\n", listing.strip()):
        fields = dict(
            line.split(" = ", 1) for line in block.splitlines() if " = " in line
        )
        try:
            path = PureWindowsPath(fields["Path"])
            size = int(fields["Size"])
            attributes = fields["Attributes"]
        except (KeyError, ValueError) as error:
            raise ValueError(f"Invalid 7-Zip entry metadata: {block[:500]}") from error
        if (
            not path.parts or path.drive or path.root or size < 0
            or any(part == ".." or ":" in part for part in path.parts)
        ):
            raise ValueError(f"Unsafe archive member path: {path}")
        entries.append(
            ArchiveEntry(
                path=path,
                size=size,
                crc=fields.get("CRC", ""),
                directory=attributes.startswith("D"),
                link=(
                    "Symbolic Link" in fields or "Hard Link" in fields
                    or "L" in attributes.split(" ", 1)[0]
                    or re.search(r"(?:^|\s)l[rwxstST-]{9}", attributes) is not None
                ),
            )
        )
    return entries


def ensure_output_dir(output_dir: Path, protected: tuple[Path, ...] = ()) -> Path:
    """Refuse linked, root or protected output directories; return the resolved path."""
    directory = output_dir.absolute()
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"Refusing a linked output directory: {directory}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Output path is not a directory: {directory}")
    directory = directory.resolve()
    guarded = (Path.home().resolve(), *(path.resolve() for path in protected))
    if directory == Path(directory.anchor) or any(
        directory == path or directory in path.parents for path in guarded
    ):
        raise ValueError(f"Refusing a protected output directory: {directory}")
    return directory


def clear_workspace(workspace: Path, *, remove_directory: bool = False) -> None:
    if not workspace.exists():
        LOGGER.info("Packaging workspace already absent: %s", workspace)
        return
    LOGGER.info("Cleaning packaging workspace: %s", workspace)
    for attempt in range(6):
        try:
            for entry in workspace.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            if remove_directory:
                workspace.rmdir()
            LOGGER.info("Packaging workspace cleanup completed: %s", workspace)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (32, 33) or attempt == 5:
                raise
            LOGGER.warning(
                "Packaging workspace is in use; retrying cleanup (%s/5): %s",
                attempt + 1, workspace,
            )
            time.sleep(2)
