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
LEGACY_CACHE_DIR = Path(r"C:\ATF")
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
    config_file: Path,
) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(repackaging_dir / "restructure_new.py"),
        f"--bios={build.bios_id}",
        f"--package_fdr_file={build.package_name}",
        f"--file_path={output_dir}",
        f"--config_ini={config_file}",
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


def cleanup_legacy_cache(
    archive: Path, repackaging_dir: Path, log_dir: Path
) -> None:
    cache = LEGACY_CACHE_DIR
    try:
        metadata = cache.lstat()
    except FileNotFoundError:
        LOGGER.info("Legacy BIOS cache already absent: %s", cache)
        return
    if stat.S_ISLNK(metadata.st_mode) or (
        getattr(metadata, "st_file_attributes", 0)
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"Refusing to clean a linked BIOS cache: {cache}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"BIOS cache is not a directory: {cache}")

    cache = cache.resolve(strict=True)
    protected_paths = (
        archive.resolve(), repackaging_dir.resolve(), log_dir.resolve(),
        Path(__file__).resolve().parent, Path.cwd().resolve(),
        Path.home().resolve(),
    )
    if cache == Path(cache.anchor) or any(
        cache == path or cache in path.parents for path in protected_paths
    ):
        raise ValueError(f"Refusing to clean a protected BIOS cache: {cache}")

    LOGGER.info("Removing legacy BIOS cache after archive verification: %s", cache)
    for entry in cache.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    cache.rmdir()
    LOGGER.info("Legacy BIOS cache removed: %s", cache)


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
    output_dir = output_dir.resolve()
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
    if not config.has_section("work_space"):
        raise configparser.NoSectionError("work_space")

    LOGGER.info("Querying Artifactory: %s", artifactory_url)
    build = fetch_latest_build(artifactory_url)
    destination = output_dir / build.package_name
    LOGGER.info("Latest BIOS: %s; final archive: %s", build.bios_id, destination)
    if dry_run:
        planned_job = output_dir / f"packaging-{build.bios_id}-DRYRUN"
        LOGGER.info(
            "Dry run; command with illustrative temporary paths: %s",
            subprocess.list2cmdline(
                make_command(
                    build, repackaging_dir, planned_job / "output",
                    planned_job / "config.ini",
                )
            ),
        )
        return destination

    if destination.exists():
        validate_archive(destination, seven_zip)
        cleanup_legacy_cache(destination, repackaging_dir, log_dir)
        LOGGER.info("Already packaged and verified; skipping: %s", destination)
        return destination

    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    child_log = log_dir / (
        f"restructure-{build.bios_id}-"
        f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}.log"
    )
    with tempfile.TemporaryDirectory(
        prefix=f"packaging-{build.bios_id}-", dir=output_dir
    ) as temporary:
        job_dir = Path(temporary)
        workspace = job_dir / "work"
        # Legacy extraction truncates the whole download path at its first dot.
        if "." in str(workspace) or any(
            character.isspace() for character in str(workspace)
        ):
            raise ValueError(
                "The existing repackager requires a workspace path without "
                "whitespace or periods. Use an --output-dir without "
                "whitespace or periods."
            )
        staging_output = job_dir / "output"
        staging_output.mkdir()
        isolated_config = job_dir / "config.ini"
        # The existing packager recursively deletes both of these directories.
        config.set("work_space", "work_space", str(workspace))
        with isolated_config.open("w", encoding="utf-8") as target:
            config.write(target)
        command = make_command(
            build, repackaging_dir, staging_output, isolated_config
        )
        LOGGER.info("Packager command: %s", subprocess.list2cmdline(command))
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
        staged_archive = staging_output / build.package_name
        validate_archive(staged_archive, seven_zip)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite: {destination}")
        staged_archive.rename(destination)
    cleanup_legacy_cache(destination, repackaging_dir, log_dir)
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
