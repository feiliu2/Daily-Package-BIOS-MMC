"""Command-line entry: dispatch the full flow or one of the bios/mmc/upload steps.

    python main.py [options]            full flow: BIOS ID -> matching MMC (-> upload)
    python main.py bios [options]       package only the BIOS ID archive
    python main.py mmc [options]        package only the MMC matching a BIOS archive
    python main.py upload [options]     upload an existing BIOS/MMC pair

`python -m bios_mmc ...` is equivalent to `python main.py ...`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import URLError

from . import LOGGER
from .artifactory import HttpClient, parse_bios_id, select_build
from .bios import package_bios
from .fsutil import ensure_output_dir, find_seven_zip
from .mapping import DEFAULT_MAPPING_REF, GITHUB_TOKEN_ENV, default_mapping_url, load_mapping
from .mmc import package_mmc_fw
from .upload import TOKEN_ENV, optional_token, select_archives, upload_archives

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACTORY_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/OakStreamRp/main/"
)
DEFAULT_MMC_ARTIFACTORY_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/ZCodeFW/zcode_dmr_ap_dev/"
)
DEFAULT_OUTPUT_DIR = (
    Path(r"C:\LocalPath\Bios") if os.name == "nt"
    else Path.home() / "LocalPath" / "Bios"
)
DEFAULT_LOG_DIR = REPOSITORY_DIR / "logs"
DEFAULT_DOWNLOAD_RETRY = 3
HANDLED_ERRORS = (OSError, ValueError, RuntimeError, URLError)
PROG = "main.py"


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
        force=True,
    )


# --- Shared option groups (each defined once, reused by the subcommands) ------

def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument(
        "--ca-bundle", type=Path,
        help="PEM CA bundle for HTTPS (default: system store / SSL_CERT_FILE).",
    )


def add_packaging(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seven-zip", type=Path, help="7-Zip executable path.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve sources and print the plan without downloading firmware.",
    )


def add_bios_source(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("BIOS ID packaging")
    group.add_argument("--artifactory-url", default=DEFAULT_ARTIFACTORY_URL)
    group.add_argument(
        "--bios", help="BIOS ID to package (e.g. 0040D79); default: newest build.",
    )
    group.add_argument(
        "--mapping-ref", default=DEFAULT_MAPPING_REF,
        help=f"IFWI repository branch/tag/commit for biosid_oak_ap.xlsx "
             f"(default: {DEFAULT_MAPPING_REF}).",
    )
    group.add_argument("--mapping-url", help="Full GitHub contents API URL override.")
    group.add_argument(
        "--mapping-xlsx", type=Path,
        help="Use a local biosid_oak_ap.xlsx instead of downloading it.",
    )
    group.add_argument(
        "--download-retry", type=int, default=DEFAULT_DOWNLOAD_RETRY,
        help="Attempts per component package download/extraction.",
    )
    group.add_argument(
        "--jobs", type=int, default=os.cpu_count() or 1,
        help="Parallel component downloads (default: CPU count).",
    )


def add_mmc_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mmc-artifactory-url", default=DEFAULT_MMC_ARTIFACTORY_URL)


def add_upload_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--upload", action="store_true",
        help=f"After packaging, upload the BIOS/MMC pair using ${TOKEN_ENV}.",
    )


# --- Parsers --------------------------------------------------------------------

def all_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Package the newest OakStream BIOS ID archive and its matching "
                    "MMC firmware, optionally uploading both.",
        epilog="subcommands (run '<subcommand> --help' for details):\n"
               "  bios     package only the BIOS ID archive\n"
               "  mmc      package only the MMC matching an existing BIOS archive\n"
               "  upload   upload an existing BIOS/MMC pair",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_packaging(parser)
    add_bios_source(parser)
    add_mmc_source(parser)
    add_upload_flag(parser)
    add_common(parser)
    return parser


def bios_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} bios", description="Package only the BIOS ID archive.",
    )
    add_packaging(parser)
    add_bios_source(parser)
    add_common(parser)
    return parser


def mmc_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} mmc",
        description="Package only the MMC firmware referenced by an existing BIOS archive.",
    )
    parser.add_argument("--bios-archive", type=Path, required=True)
    add_packaging(parser)
    add_mmc_source(parser)
    add_upload_flag(parser)
    add_common(parser)
    return parser


def upload_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} upload",
        description=f"Upload an existing BIOS/MMC pair using ${TOKEN_ENV}.",
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--bios-archive", type=Path)
    parser.add_argument("--mmc-archive", type=Path)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show the upload plan without reading the token or contacting servers.",
    )
    add_common(parser)
    return parser


# --- Steps ----------------------------------------------------------------------

@dataclass
class Context:
    http: HttpClient
    output_dir: Path
    seven_zip: Path


def prepare(args: argparse.Namespace) -> Context:
    # One Artifactory token for both servers: sent on downloads when set
    # (anonymous otherwise), and required for uploads.
    http = HttpClient(token=optional_token(), ca_bundle=args.ca_bundle)
    output_dir = ensure_output_dir(args.output_dir, (REPOSITORY_DIR,))
    seven_zip = find_seven_zip(args.seven_zip)
    LOGGER.info("Using 7-Zip: %s", seven_zip)
    return Context(http, output_dir, seven_zip)


def bios_step(args: argparse.Namespace, context: Context) -> Path:
    if args.jobs < 1 or args.download_retry < 1:
        raise ValueError("--jobs and --download-retry must be at least 1.")
    components = load_mapping(
        local_path=args.mapping_xlsx,
        url=args.mapping_url or default_mapping_url(args.mapping_ref),
        token=os.environ.get(GITHUB_TOKEN_ENV),
        http=context.http,
    )
    LOGGER.info("Querying Artifactory: %s", args.artifactory_url)
    requested = parse_bios_id(args.bios) if args.bios else None
    build = select_build(context.http.get_text(args.artifactory_url), requested)
    return package_bios(
        build,
        build_url=f"{args.artifactory_url.rstrip('/')}/{build.directory_name}/",
        components=components,
        output_dir=context.output_dir,
        http=context.http,
        seven_zip=context.seven_zip,
        jobs=args.jobs,
        retries=args.download_retry,
        dry_run=args.dry_run,
    )


def mmc_step(args: argparse.Namespace, context: Context, bios_archive: Path) -> Path:
    return package_mmc_fw(
        bios_archive,
        artifactory_url=args.mmc_artifactory_url,
        output_dir=context.output_dir,
        http=context.http,
        seven_zip=context.seven_zip,
        dry_run=args.dry_run,
    )


def upload_step(args: argparse.Namespace, bios_archive: Path, mmc_archive: Path) -> int:
    if not args.upload or args.dry_run:
        return 0
    try:
        upload_archives(bios_archive, mmc_archive, ca_bundle=args.ca_bundle)
    except HANDLED_ERRORS:
        LOGGER.exception(
            "Artifactory upload failed; verified local BIOS/MMC archives are retained. "
            f"Retry with: {PROG} upload --bios-archive %s --mmc-archive %s",
            bios_archive, mmc_archive,
        )
        return 1
    return 0


# --- Subcommand runners ---------------------------------------------------------

def run_all(args: argparse.Namespace) -> int:
    try:
        context = prepare(args)
        bios_archive = bios_step(args, context)
        if args.dry_run:
            LOGGER.info(
                "MMC version will be read after BIOS packaging; "
                "no version or MMC build was guessed."
            )
            return 0
        mmc_archive = mmc_step(args, context, bios_archive)
    except HANDLED_ERRORS:
        LOGGER.exception("BIOS/MMC packaging failed.")
        return 1
    return upload_step(args, bios_archive, mmc_archive)


def run_bios(args: argparse.Namespace) -> int:
    try:
        bios_step(args, prepare(args))
    except HANDLED_ERRORS:
        LOGGER.exception("BIOS packaging failed.")
        return 1
    return 0


def run_mmc(args: argparse.Namespace) -> int:
    try:
        mmc_archive = mmc_step(args, prepare(args), args.bios_archive)
    except HANDLED_ERRORS:
        LOGGER.exception("MMC packaging failed.")
        return 1
    return upload_step(args, args.bios_archive, mmc_archive)


def run_upload(args: argparse.Namespace) -> int:
    try:
        bios, mmc = select_archives(args.source_dir, args.bios_archive, args.mmc_archive)
        upload_archives(bios, mmc, dry_run=args.dry_run, ca_bundle=args.ca_bundle)
    except HANDLED_ERRORS:
        LOGGER.exception("Artifactory upload failed; local archives retained.")
        return 1
    return 0


SUBCOMMANDS: dict[
    str, tuple[Callable[[], argparse.ArgumentParser], Callable[[argparse.Namespace], int]]
] = {
    "bios": (bios_parser, run_bios),
    "mmc": (mmc_parser, run_mmc),
    "upload": (upload_parser, run_upload),
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in SUBCOMMANDS:
        make_parser, runner = SUBCOMMANDS[arguments[0]]
        arguments = arguments[1:]
    else:
        make_parser, runner = all_parser, run_all
    args = make_parser().parse_args(arguments)
    configure_logging(args.log_dir)
    return runner(args)
