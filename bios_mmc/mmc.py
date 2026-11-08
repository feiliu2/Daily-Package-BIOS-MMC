"""Package the MMC firmware build referenced by a BIOS archive."""

from __future__ import annotations

import re
import tempfile
from urllib.parse import unquote, urljoin, urlsplit
from pathlib import Path, PureWindowsPath

from . import LOGGER
from .artifactory import HttpClient, index_links, select_mmc_source
from .fsutil import (
    ArchiveEntry, clear_workspace, list_archive, run_7zip, validate_archive,
)

MMC_VERSION_PATTERN = re.compile(r"mmc_pkg_(\d+\.\d+\.\d+)_")
FV_RAW_DEF_GLOB = "Trunk_*/dsc/FvRawDefPath.dsc"
MMC_BUILD_FLAVOR = "DMR_ZCODE_DebugSigned_linux"


def select_mmc_version_dir(html: str, branch_url: str, version: str) -> str:
    """Find MMC_FW.<version>/ or MMC_FW.<PREFIX>.<version>/ in a branch index.

    Branches name the folder differently (zcode_dmr_ap_dev: MMC_FW.DMR_AP.0.999.0,
    zcode_dmr_ap1_beta_rel: MMC_FW.0.915.2); the prefix must start with a letter
    so a version such as 1.0.1022.0 can never satisfy 0.1022.0.
    """
    pattern = re.compile(
        rf"MMC_FW\.(?:[A-Za-z][A-Za-z0-9_]*\.)?{re.escape(version)}"
    )
    matches: set[str] = set()
    for href in index_links(html):
        path = unquote(urlsplit(href).path)
        if path.endswith("/") and pattern.fullmatch(path.rstrip("/").rsplit("/", 1)[-1]):
            matches.add(urljoin(branch_url, href))
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one MMC_FW[.<prefix>].{version}/ folder in {branch_url}; "
            f"found {len(matches)}: {sorted(matches)}"
        )
    return matches.pop()


def read_mmc_version(bios_archive: Path, seven_zip: Path) -> str:
    if not bios_archive.is_file() or bios_archive.stat().st_size == 0:
        raise ValueError(f"BIOS archive is missing or empty: {bios_archive}")
    content = run_7zip(
        seven_zip,
        [
            "x", "-so", "-bso0", "-bsp0", "-bse2", "--", str(bios_archive),
            FV_RAW_DEF_GLOB,
        ],
    )
    references = re.findall(
        r"^\s*DEFINE\s+MMC[12]\s*=\s*(.+)$", content, flags=re.MULTILINE
    )
    if not references:
        raise ValueError("No MMC1/MMC2 references found in BIOS FvRawDefPath.dsc files.")
    versions: set[str] = set()
    for reference in references:
        match = MMC_VERSION_PATTERN.search(reference)
        if not match:
            raise ValueError(f"MMC reference has no recognizable version: {reference}")
        versions.add(match[1])
    if len(versions) != 1:
        raise ValueError(f"Conflicting MMC versions in BIOS archive: {sorted(versions)}")
    version = versions.pop()
    LOGGER.info("BIOS references MMC FW %s in %s definitions.", version, len(references))
    return version


def mmc_file_manifest(
    entries: list[ArchiveEntry], version: str, *, packaged: bool
) -> dict[PureWindowsPath, tuple[int, str]]:
    root = PureWindowsPath(f"MMCFW_{version}")
    prefix = root / "binaries" if packaged else PureWindowsPath("binaries")
    files: dict[PureWindowsPath, tuple[int, str]] = {}
    versions: set[str] = set()
    for entry in entries:
        if packaged and entry.path == root and entry.directory:
            continue
        if entry.path != prefix and prefix not in entry.path.parents:
            if packaged:
                raise ValueError(f"Unexpected file outside MMC binaries: {entry.path}")
            continue
        if entry.link:
            raise ValueError(f"Linked MMC binaries member is not allowed: {entry.path}")
        if entry.directory:
            continue
        relative = entry.path.relative_to(root) if packaged else entry.path
        if not entry.crc or relative in files:
            raise ValueError(f"Missing CRC or duplicate MMC member: {entry.path}")
        files[relative] = (entry.size, entry.crc)
        match = MMC_VERSION_PATTERN.match(entry.path.name)
        if match and entry.path.suffix.lower() == ".bin":
            versions.add(match[1])
    if not files or versions != {version}:
        raise ValueError(
            f"MMC binaries do not match BIOS version {version}: {sorted(versions)}"
        )
    return files


def package_mmc_fw(
    bios_archive: Path,
    *,
    branch_url: str,
    output_dir: Path,
    http: HttpClient,
    seven_zip: Path,
    dry_run: bool,
    version: str | None = None,
) -> Path:
    bios_archive = bios_archive.resolve()
    output_dir = output_dir.resolve()
    version = version or read_mmc_version(bios_archive, seven_zip)
    package_name = f"MMCFW_{version}"
    destination = output_dir / f"{package_name}.7z"
    if destination == bios_archive:
        raise ValueError("MMC output must not overwrite the input BIOS archive.")
    branch_url = branch_url.rstrip("/") + "/"
    version_url = select_mmc_version_dir(http.get_text(branch_url), branch_url, version)
    flavors = {
        unquote(urlsplit(href).path).rstrip("/").rsplit("/", 1)[-1]
        for href in index_links(http.get_text(version_url))
    }
    if MMC_BUILD_FLAVOR not in flavors:
        raise ValueError(
            f"{version_url} has no {MMC_BUILD_FLAVOR}/ build; "
            "try another --mmc-branch."
        )
    directory_url = f"{version_url}{MMC_BUILD_FLAVOR}/"
    source_url = select_mmc_source(http.get_text(directory_url), directory_url)
    LOGGER.info("Matching MMC source: %s; final archive: %s", source_url, destination)
    if dry_run:
        LOGGER.info("Dry run; no MMC download, extraction or output changes.")
        return destination

    output_dir.mkdir(parents=True, exist_ok=True)
    # Stage on the output volume so a verified archive can be published atomically.
    workspace = Path(tempfile.mkdtemp(prefix="mmc-fw-", dir=output_dir))
    try:
        source = workspace / "BuildPkg.7z"
        http.download(source_url, source)
        expected = mmc_file_manifest(
            list_archive(source, seven_zip), version, packaged=False
        )
        package_root = workspace / package_name
        package_root.mkdir()
        run_7zip(
            seven_zip,
            [
                "x", "-y", "-bso0", "-bsp0", f"-o{package_root}", "--",
                str(source), "binaries", "binaries/*",
            ],
        )
        archive = workspace / destination.name
        run_7zip(
            seven_zip,
            [
                "a", "-t7z", "-y", "-bso0", "-bsp0", "--", str(archive),
                package_name,
            ],
            cwd=workspace,
        )
        validate_archive(archive, seven_zip)
        actual = mmc_file_manifest(
            list_archive(archive, seven_zip), version, packaged=True
        )
        if actual != expected:
            raise ValueError("Packaged MMC files/sizes/CRCs differ from source binaries.")
        archive.replace(destination)
    finally:
        clear_workspace(workspace, remove_directory=True)
    LOGGER.info("MMC packaging completed (%s files): %s", len(expected), destination)
    return destination
