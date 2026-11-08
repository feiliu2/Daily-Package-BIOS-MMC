"""Native reimplementation of OakStreamAPIfwi's restructure_new.py BIOS repackaging."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import URLError

from . import LOGGER
from .artifactory import (
    BiosBuild, HttpClient, select_latest_file, select_subdirectory,
)
from .fsutil import (
    clear_workspace, create_archive, extract_archive, validate_archive,
)
from .mapping import ComponentPackage

DSC_FILE_LIST = (
    "FvOpRomPath", "FvRawDefPath", "FvOpUcodePath", "FvOpUcode2Path",
    "FvOpUcode3Path", "FvOpUcode4Path", "FvOpUcode5Path", "FvOpUcode6Path",
)
NESTED_ZIP = "IFWIStitch20.bin"
SPR_FDF = "BinaryFvSpr.fdf"
S3M_PATH = b"OakStreamRpPkg/Restricted/Stitching/S3M_FW/s3m_fw"


@dataclass
class ComponentResult:
    component: ComponentPackage
    package_name: str
    packaged: bool
    found: int = 0
    error: str = ""


@dataclass(frozen=True)
class PlannedComponent:
    component: ComponentPackage
    url: str | None
    reason: str = ""


def split_windows_parts(value: str) -> list[str]:
    """Split a mapping cell such as 'Trunk_1P0_Debug\\\\dsc' into path parts."""
    return [part for part in re.split(r"[\\/]+", value) if part]


def safe_relative(value: str) -> Path:
    parts = split_windows_parts(value)
    if (
        not parts or any(part in (".", "..") or ":" in part for part in parts)
        or value.startswith(("/", "\\"))
    ):
        raise ValueError(f"Unsafe or empty mapping path: {value!r}")
    return Path(*parts)


def match_string(path: Path) -> str:
    """Render a path with Windows separators so mapping regexes behave identically."""
    text = str(path)
    return text if os.sep == "\\" else text.replace("/", "\\")


def walk_paths(root: Path) -> list[Path]:
    """Files then directories per level, like the original walk_file_new, sorted."""
    paths: list[Path] = []
    for current, directories, files in os.walk(root):
        directories.sort(key=str.upper)
        base = Path(current)
        paths.extend(base / name for name in sorted(files, key=str.upper))
        paths.extend(base / name for name in directories)
    return paths


def _windows_text(data: bytes) -> bytes:
    """Emulate a Windows text-mode read followed by a text-mode write."""
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized.replace(b"\n", b"\r\n")


def combine_dsc(pattern: str, destination: Path, extracted: Path) -> bool:
    """Concatenate <name>*.dsc siblings exactly as restructure_new.py does on Windows."""
    source = extracted.joinpath(*split_windows_parts(pattern))
    name = source.stem
    if not source.parent.is_dir():
        return False
    pieces: list[bytes] = []
    for child in sorted(source.parent.iterdir(), key=lambda item: item.name.upper()):
        stem, suffix = os.path.splitext(child.name)
        if suffix.lower() != ".dsc" or stem.lower() == name:
            continue
        if not child.name.lower().startswith(name.lower()) or not child.is_file():
            continue
        LOGGER.debug("Combining DSC: %s", child)
        # Original: buffer += f.read() + os.linesep, then a text-mode write turns
        # every "\n" into "\r\n", so each separator becomes "\r\r\n" on Windows.
        pieces.append(_windows_text(child.read_bytes()) + b"\r\r\n")
    if not pieces:
        return False
    (destination / source.name).write_bytes(b"".join(pieces))
    return True


def rewrite_spr_fdf(pattern: str, extracted: Path) -> None:
    source = extracted.joinpath(*split_windows_parts(pattern))
    if source.is_file():
        data = source.read_bytes().replace(S3M_PATH, b"$(FD_BIN_PKG)")
        source.write_bytes(_windows_text(data))


def copy_matches(pattern: str, destination: Path, candidates: list[Path]) -> bool:
    expression = re.compile(pattern + "$", re.I)
    found = False
    for path in candidates:
        if not expression.search(match_string(path)):
            continue
        found = True
        LOGGER.debug("Copy %s -> %s", path, destination)
        if path.is_dir():
            shutil.copytree(path, destination, dirs_exist_ok=True)
        else:
            shutil.copy(path, destination)
    return found


def copy_component(
    component: ComponentPackage, extracted: Path, packaging: Path
) -> int:
    found = 0
    candidates = walk_paths(extracted)
    for row in component.rows:
        destination = packaging / safe_relative(row.target)
        destination.mkdir(parents=True, exist_ok=True)
        parts = split_windows_parts(row.binary)
        stem = os.path.splitext(parts[-1])[0] if parts else ""
        if SPR_FDF.lower() in row.binary.lower():
            rewrite_spr_fdf(row.binary, extracted)
        if stem in DSC_FILE_LIST:
            ok = combine_dsc(row.binary, destination, extracted)
        else:
            ok = copy_matches(row.binary, destination, candidates)
        if ok:
            found += 1
        else:
            LOGGER.warning(
                "Not found in %s: %s", component.package_pattern, row.binary
            )
    return found


def expand_nested_zip(extracted: Path) -> None:
    nested = extracted / NESTED_ZIP
    if not nested.is_file() or not zipfile.is_zipfile(nested):
        return
    root = extracted.resolve()
    with zipfile.ZipFile(nested) as archive:
        for member in archive.namelist():
            target = (extracted / member).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe member in {NESTED_ZIP}: {member}")
        archive.extractall(extracted)


def plan_components(
    components: list[ComponentPackage], build_url: str, http: HttpClient
) -> list[PlannedComponent]:
    build_index = http.get_text(build_url)
    planned: list[PlannedComponent] = []
    for component in components:
        folder_url = select_subdirectory(build_index, build_url, component.folder)
        if folder_url is None:
            planned.append(PlannedComponent(
                component, None, f"folder {component.folder}/ not in {build_url}"
            ))
            continue
        package_url = select_latest_file(
            http.get_text(folder_url), folder_url, component.package_pattern
        )
        if package_url is None:
            planned.append(PlannedComponent(
                component, None,
                f"no file matching {component.package_pattern} in {folder_url}",
            ))
            continue
        planned.append(PlannedComponent(component, package_url))
    return planned


@dataclass
class FetchedComponent:
    plan: PlannedComponent
    package_name: str
    job: Path | None = None
    extracted: Path | None = None
    error: str = ""


def fetch_component(
    plan: PlannedComponent, work: Path, http: HttpClient, seven_zip: Path, retries: int,
) -> FetchedComponent:
    """Download and extract one component package (safe to run in parallel)."""
    component = plan.component
    if plan.url is None:
        return FetchedComponent(plan, component.folder, error=plan.reason)
    name = PurePosixPath(plan.url).name
    package_name = name.split(".")[0]
    job = work / f"line{component.excel_line}"
    archive = job / name
    extracted = job / package_name
    error = ""
    for attempt in range(1, max(1, retries) + 1):
        try:
            job.mkdir(parents=True, exist_ok=True)
            http.download(plan.url, archive)
            if extracted.exists():
                shutil.rmtree(extracted)
            extract_archive(archive, extracted, seven_zip)
            archive.unlink()
            expand_nested_zip(extracted)
            return FetchedComponent(plan, package_name, job, extracted)
        except (OSError, ValueError, RuntimeError, URLError) as failure:
            error = (str(failure).splitlines() or [repr(failure)])[0]
            LOGGER.warning(
                "Download/extract failed for %s (%s/%s): %s",
                name, attempt, retries, error,
            )
    clear_workspace(job, remove_directory=True)
    return FetchedComponent(plan, package_name, error=error)


def copy_fetched(fetched: FetchedComponent, packaging: Path) -> ComponentResult:
    """Copy one extracted component into the packaging tree (run sequentially)."""
    component = fetched.plan.component
    if fetched.extracted is None:
        return ComponentResult(component, fetched.package_name, False, error=fetched.error)
    try:
        found = copy_component(component, fetched.extracted, packaging)
    except (OSError, ValueError, re.error, shutil.Error) as failure:
        return ComponentResult(
            component, fetched.package_name, False, error=str(failure)[:500]
        )
    finally:
        if fetched.job is not None:
            clear_workspace(fetched.job, remove_directory=True)
    packaged = found == len(component.rows)
    return ComponentResult(
        component, fetched.package_name, packaged, found=found,
        error="" if packaged else f"{len(component.rows) - found} files not found",
    )


def log_summary(results: list[ComponentResult]) -> None:
    missing = [result for result in results if not result.packaged]
    total = len(results)
    LOGGER.log(
        logging.WARNING if missing else logging.INFO,
        "BIOS component package summary: total=%s, packaged=%s, not packaged=%s",
        total, total - len(missing), len(missing),
    )
    for result in missing:
        LOGGER.warning(
            "Not packaged: %s (%s)", result.package_name, result.error or "unknown"
        )


def package_bios(
    build: BiosBuild,
    *,
    build_url: str,
    components: list[ComponentPackage],
    output_dir: Path,
    http: HttpClient,
    seven_zip: Path,
    jobs: int,
    retries: int,
    dry_run: bool,
) -> Path:
    destination = output_dir / build.package_name
    LOGGER.info("BIOS %s from %s; final archive: %s", build.bios_id, build_url, destination)
    planned = plan_components(components, build_url, http)
    for plan in planned:
        LOGGER.info(
            "Component line %s: %s", plan.component.excel_line,
            plan.url or f"UNAVAILABLE ({plan.reason})",
        )
    if dry_run:
        LOGGER.info("Dry run; nothing downloaded, extracted or written.")
        return destination
    if not any(plan.url for plan in planned):
        raise ValueError("None of the BIOS component packages are available.")

    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="bios-", dir=output_dir))
    try:
        work = staging / "work"
        packaging = staging / "pack"
        packaging.mkdir()
        for component in components:
            for row in component.rows:
                (packaging / safe_relative(row.target)).mkdir(parents=True, exist_ok=True)
        workers = max(1, min(jobs, len(planned)))
        # Downloads/extractions run in parallel; copies run one component at a
        # time in spreadsheet order, so components sharing a target folder
        # (e.g. Tools\BaseTools) never race and the last row wins deterministically.
        results: list[ComponentResult] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(fetch_component, plan, work, http, seven_zip, retries)
                for plan in planned
            ]
            for future in futures:
                results.append(copy_fetched(future.result(), packaging))
        log_summary(results)
        if not any(result.found for result in results):
            raise ValueError("No BIOS component files were found; nothing to publish.")
        archive = staging / build.package_name
        create_archive(packaging, archive, seven_zip)
        validate_archive(archive, seven_zip)
        archive.replace(destination)
    finally:
        clear_workspace(staging, remove_directory=True)
    LOGGER.info("BIOS packaging completed: %s", destination)
    return destination
