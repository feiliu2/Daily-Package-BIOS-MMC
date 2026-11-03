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
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


DEFAULT_ARTIFACTORY_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/OakStreamRp/main/"
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


class BuildIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.builds: list[BiosBuild] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        path = unquote(urlsplit(href).path)
        if not path.endswith("/"):
            return
        name = path.rstrip("/").rsplit("/", 1)[-1]
        match = BUILD_PATTERN.fullmatch(name)
        if match:
            self.builds.append(
                BiosBuild(match["release"], match["revision"])
            )


def parse_latest_build(html: str) -> BiosBuild:
    parser = BuildIndexParser()
    parser.feed(html)
    parser.close()
    if not parser.builds:
        raise ValueError(
            "No OAKSTRM.0.RPB.<release>.D.<revision>/ directories found. "
            "Check the Artifactory URL, Intel network access and permissions."
        )
    return max(
        parser.builds,
        key=lambda build: (int(build.release), int(build.revision)),
    )


def fetch_latest_build(url: str) -> BiosBuild:
    request = Request(url, headers={"User-Agent": "OakStream-BIOS-Packager/1.0"})
    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset)
    return parse_latest_build(html)


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


def validate_archive(archive: Path, seven_zip: Path) -> None:
    if not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError(f"Archive is missing or empty: {archive}")
    result = subprocess.run(
        [str(seven_zip), "t", "-bso0", "-bsp0", "-y", "--", str(archive)],
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
            f"7-Zip validation failed ({result.returncode}): {archive}\n"
            f"{result.stdout}"
        )


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


def clear_workspace(workspace: Path) -> None:
    if not workspace.exists():
        LOGGER.info("BIOS workspace already absent: %s", workspace)
        return
    LOGGER.info("Clearing BIOS workspace after archive verification: %s", workspace)
    for attempt in range(6):
        try:
            for entry in workspace.iterdir():
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            LOGGER.info("BIOS workspace is empty: %s", workspace)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (32, 33) or attempt == 5:
                raise
            LOGGER.warning(
                "BIOS workspace is in use; retrying cleanup (%s/5): %s",
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
        description="Package the newest OakStream daily BIOS with the existing repackager."
    )
    parser.add_argument("--artifactory-url", default=DEFAULT_ARTIFACTORY_URL)
    parser.add_argument(
        "--repackaging-dir", type=Path, default=DEFAULT_REPACKAGING_DIR
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config-ini", default=DEFAULT_CONFIG_INI)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show the latest build and planned command without packaging.",
    )
    args = parser.parse_args(argv)
    try:
        configure_logging(args.log_dir)
        package_latest(
            artifactory_url=args.artifactory_url,
            repackaging_dir=args.repackaging_dir,
            output_dir=args.output_dir,
            config_ini=args.config_ini,
            log_dir=args.log_dir,
            dry_run=args.dry_run,
        )
    except (
        OSError, ValueError, RuntimeError, configparser.Error,
        subprocess.CalledProcessError, subprocess.TimeoutExpired,
    ):
        LOGGER.exception("BIOS packaging failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
