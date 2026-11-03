from __future__ import annotations

import argparse
import configparser
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen


DEFAULT_ARTIFACTORY_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/OakStreamRp/main/"
)
DEFAULT_MMC_ARTIFACTORY_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/ZCodeFW/zcode_dmr_ap_dev/"
)
DEFAULT_REPACKAGING_DIR = Path(
    r"C:\OKS\OakStreamAPIfwi\Ifwi\oak_release"
    r"\BuildScripts\CommonLib\BIOS_Repackaging"
)
DEFAULT_OUTPUT_DIR = Path(r"C:\LocalPath\Bios")
WORKSPACE_DIR = Path(r"C:\ATF")
DEFAULT_CONFIG_INI = "config_oak_ap.ini"
DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
BUILD_PATTERN = re.compile(
    r"OAKSTRM\.0\.RPB\.(?P<release>\d{4})\.D\.(?P<revision>\d+)"
)
MMC_VERSION_PATTERN = re.compile(r"mmc_pkg_(\d+\.\d+\.\d+)_")
LOGGER = logging.getLogger("bios_packager")


@dataclass(frozen=True)
class BiosBuild:
    release: str
    revision: str

    @property
    def bios_id(self) -> str:
        return f"{self.release}D{self.revision}"

    @property
    def package_name(self) -> str:
        return f"{self.release}.D{self.revision}.7z"


@dataclass(frozen=True)
class ArchiveEntry:
    path: PureWindowsPath
    size: int
    crc: str
    directory: bool
    link: bool


class DirectoryIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def parse_latest_build(html: str) -> BiosBuild:
    parser = DirectoryIndexParser()
    parser.feed(html)
    parser.close()
    builds: list[BiosBuild] = []
    for href in parser.links:
        path = unquote(urlsplit(href).path)
        if not path.endswith("/"):
            continue
        name = path.rstrip("/").rsplit("/", 1)[-1]
        match = BUILD_PATTERN.fullmatch(name)
        if match:
            builds.append(BiosBuild(match["release"], match["revision"]))
    if not builds:
        raise ValueError(
            "No OAKSTRM.0.RPB.<release>.D.<revision>/ directories found. "
            "Check the Artifactory URL, Intel network access and permissions."
        )
    return max(
        builds,
        key=lambda build: (int(build.release), int(build.revision)),
    )


def fetch_index(url: str) -> str:
    request = Request(url, headers={"User-Agent": "OakStream-BIOS-Packager/1.0"})
    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def fetch_latest_build(url: str) -> BiosBuild:
    return parse_latest_build(fetch_index(url))


def make_command(
    build: BiosBuild,
    repackaging_dir: Path,
    output_dir: Path,
    config_ini: str,
) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(repackaging_dir / "restructure_new.py"),
        f"--bios={build.bios_id}",
        f"--package_fdr_file={build.package_name}",
        f"--file_path={output_dir}",
        f"--config_ini={config_ini}",
    ]


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
        timeout=1800,
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


def read_mmc_version(bios_archive: Path, seven_zip: Path) -> str:
    if not bios_archive.is_file() or bios_archive.stat().st_size == 0:
        raise ValueError(f"BIOS archive is missing or empty: {bios_archive}")
    content = run_7zip(
        seven_zip,
        [
            "x", "-so", "-bso0", "-bsp0", "-bse2", "--", str(bios_archive),
            r"Trunk_*\dsc\FvRawDefPath.dsc",
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


def select_mmc_source(html: str, directory_url: str) -> str:
    parser = DirectoryIndexParser()
    parser.feed(html)
    parser.close()
    base = urlsplit(directory_url)
    base_path = unquote(base.path).rstrip("/") + "/"
    candidates: set[str] = set()
    for href in parser.links:
        url = urljoin(directory_url, href)
        target = urlsplit(url)
        path = unquote(target.path)
        if (
            (target.scheme, target.netloc) != (base.scheme, base.netloc)
            or not path.startswith(base_path)
        ):
            continue
        filename = path[len(base_path):]
        if (
            "/" not in filename and "\\" not in filename
            and filename.endswith("_BuildPkg.7z")
        ):
            candidates.add(url)
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one MMC _BuildPkg.7z at {directory_url}; "
            f"found {len(candidates)}: {sorted(candidates)}"
        )
    return candidates.pop()


def download_file(url: str, destination: Path) -> None:
    LOGGER.info("Downloading MMC source: %s", url)
    request = Request(url, headers={"User-Agent": "OakStream-BIOS-Packager/1.0"})
    with urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)
        length = response.headers.get("Content-Length")
        if length is not None and output.tell() != int(length):
            raise ValueError(f"Incomplete MMC source download: {url}")
    if destination.stat().st_size == 0:
        raise ValueError(f"Empty MMC source download: {url}")


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
    artifactory_url: str,
    repackaging_dir: Path,
    output_dir: Path,
    dry_run: bool,
) -> Path:
    bios_archive = bios_archive.resolve()
    output_dir = output_dir.resolve()
    seven_zip = repackaging_dir.resolve() / "7-Zip" / "7z.exe"
    if not seven_zip.is_file():
        raise FileNotFoundError(f"Required file not found: {seven_zip}")
    version = read_mmc_version(bios_archive, seven_zip)
    package_name = f"MMCFW_{version}"
    destination = output_dir / f"{package_name}.7z"
    if destination == bios_archive:
        raise ValueError("MMC output must not overwrite the input BIOS archive.")
    directory_url = (
        f"{artifactory_url.rstrip('/')}/MMC_FW.DMR_AP.{version}/"
        "DMR_ZCODE_DebugSigned_linux/"
    )
    source_url = select_mmc_source(fetch_index(directory_url), directory_url)
    LOGGER.info("Matching MMC source: %s; final archive: %s", source_url, destination)
    if dry_run:
        LOGGER.info("Dry run; no MMC download, extraction or output changes.")
        return destination

    output_dir.mkdir(parents=True, exist_ok=True)
    # Stage on the output volume so a verified archive can be published atomically.
    workspace = Path(tempfile.mkdtemp(prefix="mmc-fw-", dir=output_dir))
    try:
        source = workspace / "BuildPkg.7z"
        download_file(source_url, source)
        expected = mmc_file_manifest(
            list_archive(source, seven_zip), version, packaged=False
        )
        package_root = workspace / package_name
        package_root.mkdir()
        run_7zip(
            seven_zip,
            [
                "x", "-y", "-bso0", "-bsp0", f"-o{package_root}", "--",
                str(source), "binaries", r"binaries\*",
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


def validate_packaging_paths(
    output_dir: Path, repackaging_dir: Path, log_dir: Path, config_file: Path
) -> Path:
    workspace = WORKSPACE_DIR.absolute()
    protected_paths = (
        repackaging_dir.resolve(), log_dir.resolve(), config_file.resolve(),
        Path(__file__).resolve().parent, Path.cwd().resolve(),
        Path.home().resolve(),
    )
    for directory in (workspace, output_dir.absolute()):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError(f"Refusing a linked packaging directory: {directory}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"Packaging path is not a directory: {directory}")
        directory = directory.resolve()
        if directory == Path(directory.anchor) or any(
            directory == path or directory in path.parents
            for path in protected_paths
        ):
            raise ValueError(f"Refusing a protected packaging directory: {directory}")

    workspace = workspace.resolve()
    output_dir = output_dir.resolve()
    if (
        workspace == output_dir
        or workspace in output_dir.parents
        or output_dir in workspace.parents
    ):
        raise ValueError("BIOS workspace and output directory must not overlap.")
    if "." in str(workspace) or any(
        character.isspace() for character in str(workspace)
    ):
        raise ValueError("The original workspace path must have no whitespace or periods.")
    return workspace


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


def package_latest(
    *,
    artifactory_url: str,
    repackaging_dir: Path,
    output_dir: Path,
    config_ini: str,
    log_dir: Path,
    dry_run: bool,
) -> Path:
    repackaging_dir = repackaging_dir.resolve()
    output_dir = output_dir.absolute()
    config_file = Path(config_ini)
    if not config_file.is_absolute():
        config_file = repackaging_dir / "Config" / config_file
    seven_zip = repackaging_dir / "7-Zip" / "7z.exe"
    for required in (
        repackaging_dir / "restructure_new.py", config_file, seven_zip
    ):
        if not required.is_file():
            raise FileNotFoundError(f"Required file not found: {required}")

    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    with config_file.open(encoding="utf-8-sig") as source:
        config.read_file(source)
    configured_workspace = Path(config.get("work_space", "work_space")).absolute()
    if configured_workspace != WORKSPACE_DIR.absolute():
        raise ValueError(
            f"The original configuration must use work_space={WORKSPACE_DIR}; "
            "it will not be rewritten."
        )
    validate_packaging_paths(output_dir, repackaging_dir, log_dir, config_file)
    output_dir = output_dir.resolve()

    LOGGER.info("Querying Artifactory: %s", artifactory_url)
    build = fetch_latest_build(artifactory_url)
    destination = output_dir / build.package_name
    LOGGER.info("Latest BIOS: %s; final archive: %s", build.bios_id, destination)
    command = make_command(build, repackaging_dir, output_dir, config_ini)
    LOGGER.info("Working directory: %s", repackaging_dir)
    LOGGER.info("Packager command: %s", subprocess.list2cmdline(command))
    if dry_run:
        LOGGER.info("Dry run; the original command and workspace were not changed.")
        return destination

    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    child_log = log_dir / (
        f"restructure-{build.bios_id}-"
        f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.log"
    )
    LOGGER.info("The original packager will replace the contents of: %s", output_dir)
    LOGGER.info("Packager output log: %s", child_log)
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    with child_log.open("w", encoding="utf-8") as output:
        subprocess.run(
            command,
            cwd=repackaging_dir,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=environment,
            timeout=10800,
            check=True,
        )
    validate_archive(destination, seven_zip)
    workspace = validate_packaging_paths(
        output_dir, repackaging_dir, log_dir, config_file
    )
    clear_workspace(workspace)
    LOGGER.info("BIOS packaging completed: %s", destination)
    return destination


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(
                log_dir / f"package-bios-{datetime.now():%Y%m%d}.log",
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package the newest OakStream BIOS and its matching MMC firmware."
    )
    parser.add_argument("--artifactory-url", default=DEFAULT_ARTIFACTORY_URL)
    parser.add_argument(
        "--mmc-artifactory-url", default=DEFAULT_MMC_ARTIFACTORY_URL
    )
    parser.add_argument(
        "--bios-archive", type=Path,
        help="Package only matching MMC firmware from an existing BIOS archive.",
    )
    parser.add_argument(
        "--repackaging-dir", type=Path, default=DEFAULT_REPACKAGING_DIR
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config-ini", default=DEFAULT_CONFIG_INI)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview source selection without packaging or downloading firmware.",
    )
    args = parser.parse_args(argv)
    try:
        configure_logging(args.log_dir)
        if args.bios_archive is None:
            bios_archive = package_latest(
                artifactory_url=args.artifactory_url,
                repackaging_dir=args.repackaging_dir,
                output_dir=args.output_dir,
                config_ini=args.config_ini,
                log_dir=args.log_dir,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                LOGGER.info(
                    "MMC version will be read after BIOS packaging; "
                    "no version or MMC build was guessed."
                )
                return 0
        else:
            bios_archive = args.bios_archive
        package_mmc_fw(
            bios_archive,
            artifactory_url=args.mmc_artifactory_url,
            repackaging_dir=args.repackaging_dir,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    except (
        OSError, ValueError, RuntimeError, configparser.Error,
        subprocess.CalledProcessError, subprocess.TimeoutExpired,
    ):
        LOGGER.exception("BIOS/MMC packaging failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
