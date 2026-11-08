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
from .artifactory import BiosBuild, HttpClient, parse_bios_id, select_build
from .bios import package_bios
from .fsutil import ensure_output_dir, find_seven_zip
from .mapping import DEFAULT_MAPPING_REF, GITHUB_TOKEN_ENV, default_mapping_url, load_mapping
from .mmc import package_mmc_fw, read_mmc_version
from .report import RunReport
from .upload import (
    DEFAULT_BIOS_PATH, DEFAULT_MMC_PATH, DEFAULT_UPLOAD_URL, TOKEN_ENV,
    TargetRepository, UploadTarget, optional_token, parse_upload_target,
    select_archives, upload_archives,
)

REPOSITORY_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BIOS_BASE_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/OakStreamRp/"
)
DEFAULT_BIOS_BRANCH = "main"
DEFAULT_MMC_BASE_URL = (
    "https://af01p-or.devtools.intel.com/artifactory/"
    "server-bios-staging-local/Daily/ZCodeFW/"
)
DEFAULT_MMC_BRANCH = "zcode_dmr_ap_dev"
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
        "--result-json", type=Path, metavar="PATH",
        help="Write a machine-readable result (BIOS ID, MMC version, archive paths, "
             "SHA-256, skipped/uploaded status) to PATH, on success and on failure.",
    )
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
    group.add_argument(
        "--bios-base-url", default=DEFAULT_BIOS_BASE_URL,
        help="Artifactory folder that contains the BIOS branch folders.",
    )
    group.add_argument(
        "--bios-branch", default=DEFAULT_BIOS_BRANCH,
        help=f"BIOS branch folder under --bios-base-url (default: {DEFAULT_BIOS_BRANCH}).",
    )
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
    group = parser.add_argument_group("MMC packaging")
    group.add_argument(
        "--mmc-base-url", default=DEFAULT_MMC_BASE_URL,
        help="Artifactory folder that contains the MMC (ZCodeFW) branch folders.",
    )
    group.add_argument(
        "--mmc-branch", default=DEFAULT_MMC_BRANCH,
        help=f"MMC branch folder under --mmc-base-url (default: {DEFAULT_MMC_BRANCH}).",
    )


def add_upload_target(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("upload destination")
    group.add_argument(
        "--upload-url", default=DEFAULT_UPLOAD_URL,
        help="https://<host>/artifactory/<repository> to upload into.",
    )
    group.add_argument(
        "--upload-bios-path", default=DEFAULT_BIOS_PATH,
        help=f"Folder in the repository for BIOS archives (default: {DEFAULT_BIOS_PATH}).",
    )
    group.add_argument(
        "--upload-mmc-path", default=DEFAULT_MMC_PATH,
        help=f"Folder in the repository for MMC archives (default: {DEFAULT_MMC_PATH}).",
    )


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
    add_upload_target(parser)
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
    add_upload_target(parser)
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
    add_upload_target(parser)
    add_common(parser)
    return parser


def branch_url_for(base_url: str, branch: str, option: str) -> str:
    parts = [part for part in branch.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid {option}: {branch!r}")
    return f"{base_url.rstrip('/')}/{'/'.join(parts)}/"


def upload_target(args: argparse.Namespace) -> UploadTarget:
    return parse_upload_target(args.upload_url, args.upload_bios_path, args.upload_mmc_path)


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


def connect_target(args: argparse.Namespace) -> TargetRepository:
    """Open the upload destination read-only; requires the Artifactory token."""
    return TargetRepository.connect(upload_target(args), ca_bundle=args.ca_bundle)


def select_build_step(args: argparse.Namespace, context: Context) -> tuple[BiosBuild, str]:
    if args.jobs < 1 or args.download_retry < 1:
        raise ValueError("--jobs and --download-retry must be at least 1.")
    branch_url = branch_url_for(args.bios_base_url, args.bios_branch, "--bios-branch")
    LOGGER.info("Querying Artifactory: %s", branch_url)
    requested = parse_bios_id(args.bios) if args.bios else None
    build = select_build(context.http.get_text(branch_url), requested)
    LOGGER.info("Selected BIOS %s (%s).", build.bios_id, build.package_name)
    return build, branch_url


def package_bios_step(
    args: argparse.Namespace, context: Context, build: BiosBuild, branch_url: str,
) -> Path:
    components = load_mapping(
        local_path=args.mapping_xlsx,
        url=args.mapping_url or default_mapping_url(args.mapping_ref),
        token=os.environ.get(GITHUB_TOKEN_ENV),
        http=context.http,
    )
    return package_bios(
        build,
        build_url=f"{branch_url}{build.directory_name}/",
        components=components,
        output_dir=context.output_dir,
        http=context.http,
        seven_zip=context.seven_zip,
        jobs=args.jobs,
        retries=args.download_retry,
        dry_run=args.dry_run,
    )


def record_bios(args: argparse.Namespace, report: RunReport, path: Path) -> None:
    if args.dry_run:
        report.planned(report.bios, path)
    else:
        report.local_file(report.bios, path, "packaged")


def bios_step(args: argparse.Namespace, context: Context, report: RunReport) -> Path:
    build, branch_url = select_build_step(args, context)
    report.bios_id = build.bios_id
    path = package_bios_step(args, context, build, branch_url)
    record_bios(args, report, path)
    return path


def mmc_step(
    args: argparse.Namespace, context: Context, bios_archive: Path,
    report: RunReport, remote: TargetRepository | None = None,
) -> Path | None:
    """Package the matching MMC; None when the target already has it."""
    version = read_mmc_version(bios_archive.resolve(), context.seven_zip)
    report.mmc_version = version
    report.mmc.name = f"MMCFW_{version}.7z"
    published = remote.stat("MMC", report.mmc.name) if remote is not None else None
    if published:
        LOGGER.info("MMC %s is already published; not repackaging it.", version)
        report.published(report.mmc, published)
        return None
    path = package_mmc_fw(
        bios_archive,
        branch_url=branch_url_for(args.mmc_base_url, args.mmc_branch, "--mmc-branch"),
        output_dir=context.output_dir,
        http=context.http,
        seven_zip=context.seven_zip,
        dry_run=args.dry_run,
        version=version,
    )
    if args.dry_run:
        report.planned(report.mmc, path)
    else:
        report.local_file(report.mmc, path, "packaged")
    return path


def record_upload(
    report: RunReport, results: dict[str, dict[str, str]], failed: bool = False,
) -> None:
    for kind, artifact in (("BIOS", report.bios), ("MMC", report.mmc)):
        if artifact.skipped:
            continue
        outcome = results.get(kind)
        if outcome is None:
            artifact.upload = "failed" if failed else artifact.upload
            continue
        artifact.remote_url = outcome["url"]
        uploaded = outcome["upload"] in ("uploaded", "already_present")
        artifact.upload = "failed" if failed and not uploaded else outcome["upload"]


def mark_not_requested(report: RunReport) -> None:
    for artifact in (report.bios, report.mmc):
        if artifact.upload == "not_run" and artifact.name:
            artifact.upload = "not_requested"


def upload_step(
    args: argparse.Namespace, report: RunReport,
    bios_archive: Path | None, mmc_archive: Path | None,
) -> int:
    if not args.upload:
        mark_not_requested(report)
        return 0
    if args.dry_run:
        for artifact in (report.bios, report.mmc):
            if not artifact.skipped and artifact.name:
                artifact.upload = "preview"
        return 0
    if bios_archive is None and mmc_archive is None:
        LOGGER.info("BIOS and MMC are both already published; nothing to upload.")
        return 0
    results: dict[str, dict[str, str]] = {}
    try:
        upload_archives(
            bios_archive, mmc_archive, target=upload_target(args),
            ca_bundle=args.ca_bundle, results=results,
        )
        record_upload(report, results)
    except HANDLED_ERRORS as error:
        report.fail(error)
        record_upload(report, results, failed=True)
        LOGGER.exception(
            "Artifactory upload failed; verified local BIOS/MMC archives are retained. "
            f"Retry with: {PROG} upload --bios-archive %s --mmc-archive %s",
            bios_archive, mmc_archive,
        )
        return 1
    return 0


# --- Subcommand runners ---------------------------------------------------------

def run_all(args: argparse.Namespace, report: RunReport) -> int:
    try:
        context = prepare(args)
        # With --upload, check the destination first (this also fails fast when
        # the token is missing) so already-published archives are not rebuilt.
        remote = connect_target(args) if args.upload else None
        if remote is not None:
            report.upload_target = f"{remote.target.root}/{remote.target.repository}"
        build, branch_url = select_build_step(args, context)
        report.bios_id = build.bios_id
        published = remote.stat("BIOS", build.package_name) if remote else None
        if published is not None:
            LOGGER.info("BIOS %s is already published; not repackaging it.", build.bios_id)
            report.published(report.bios, published)
            if args.dry_run:
                LOGGER.info(
                    "Dry run; the published BIOS would be reused (local copy if "
                    "identical, otherwise downloaded) to check its MMC."
                )
                return 0
            bios_archive = remote.obtain(published, context.output_dir / build.package_name)
            report.bios.path = str(bios_archive.resolve())
        else:
            bios_archive = package_bios_step(args, context, build, branch_url)
            record_bios(args, report, bios_archive)
            if args.dry_run:
                LOGGER.info(
                    "MMC version will be read after BIOS packaging; "
                    "no version or MMC build was guessed."
                )
                upload_step(args, report, None, None)
                return 0
        mmc_archive = mmc_step(args, context, bios_archive, report, remote)
    except HANDLED_ERRORS as error:
        report.fail(error)
        LOGGER.exception("BIOS/MMC packaging failed.")
        return 1
    return upload_step(args, report, None if published else bios_archive, mmc_archive)


def run_bios(args: argparse.Namespace, report: RunReport) -> int:
    try:
        bios_step(args, prepare(args), report)
    except HANDLED_ERRORS as error:
        report.fail(error)
        LOGGER.exception("BIOS packaging failed.")
        return 1
    return 0


def record_bios_id(report: RunReport, archive: Path) -> None:
    try:
        report.bios_id = parse_bios_id(archive.name.removesuffix(".7z")).bios_id
    except ValueError:
        report.bios_id = None


def run_mmc(args: argparse.Namespace, report: RunReport) -> int:
    try:
        context = prepare(args)
        remote = connect_target(args) if args.upload else None
        if remote is not None:
            report.upload_target = f"{remote.target.root}/{remote.target.repository}"
        record_bios_id(report, args.bios_archive)
        report.local_file(report.bios, args.bios_archive, "provided")
        mmc_archive = mmc_step(args, context, args.bios_archive, report, remote)
    except HANDLED_ERRORS as error:
        report.fail(error)
        LOGGER.exception("MMC packaging failed.")
        return 1
    return upload_step(args, report, args.bios_archive, mmc_archive)


def run_upload(args: argparse.Namespace, report: RunReport) -> int:
    results: dict[str, dict[str, str]] = {}
    try:
        target = upload_target(args)
        report.upload_target = f"{target.root}/{target.repository}"
        bios, mmc = select_archives(args.source_dir, args.bios_archive, args.mmc_archive)
        record_bios_id(report, bios)
        report.local_file(report.bios, bios, "provided")
        report.local_file(report.mmc, mmc, "provided")
        upload_archives(
            bios, mmc, target=target, dry_run=args.dry_run,
            ca_bundle=args.ca_bundle, results=results,
        )
        record_upload(report, results)
    except HANDLED_ERRORS as error:
        report.fail(error)
        record_upload(report, results, failed=True)
        LOGGER.exception("Artifactory upload failed; local archives retained.")
        return 1
    return 0


Runner = Callable[[argparse.Namespace, RunReport], int]
SUBCOMMANDS: dict[str, tuple[Callable[[], argparse.ArgumentParser], Runner]] = {
    "bios": (bios_parser, run_bios),
    "mmc": (mmc_parser, run_mmc),
    "upload": (upload_parser, run_upload),
}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in SUBCOMMANDS:
        command = arguments[0]
        make_parser, runner = SUBCOMMANDS[command]
        arguments = arguments[1:]
    else:
        command, make_parser, runner = "all", all_parser, run_all
    args = make_parser().parse_args(arguments)
    configure_logging(args.log_dir)
    report = RunReport(command, output=args.result_json, dry_run=args.dry_run)
    exit_code = 1
    try:
        exit_code = runner(args, report)
    finally:
        try:
            report.finish(exit_code)
        except OSError:
            LOGGER.exception("Could not write --result-json %s", args.result_json)
            exit_code = 1
    return exit_code
