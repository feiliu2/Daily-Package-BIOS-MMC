"""Verified BIOS/MMC upload to Artifactory using a token from the environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import LOGGER
from .artifactory import USER_AGENT, HttpClient

UPLOAD_ROOT = "https://af01p-sc.devtools.intel.com/artifactory"
UPLOAD_REPOSITORY = "DEG-IFWI-LOCAL"
BIOS_FOLDER = "FIV_Portal/IFWI_CI/BIOSID"
MMC_FOLDER = "FIV_Portal/IFWI_CI/MMC"
TOKEN_ENV = "IFWI_ARTIFACTORY_TOKEN"
BIOS_NAME = re.compile(r"^\d{4}\.D\d+\.7z$")
MMC_NAME = re.compile(r"^MMCFW_\d+\.\d+\.\d+\.7z$")
TOKEN_FORMAT = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
METADATA_TIMEOUT = 60
UPLOAD_TIMEOUT = 1800


@dataclass
class UploadPlan:
    kind: str
    file: Path
    folder: str
    length: int
    sha256: str
    root: str = UPLOAD_ROOT
    repository: str = UPLOAD_REPOSITORY
    exists: bool = False

    @property
    def remote_path(self) -> str:
        return f"{self.folder}/{self.file.name}"

    @property
    def upload_url(self) -> str:
        return f"{self.root}/{self.repository}/{self.remote_path}"

    @property
    def metadata_url(self) -> str:
        return f"{self.root}/api/storage/{self.repository}/{self.remote_path}"

    @property
    def folder_url(self) -> str:
        return f"{self.root}/api/storage/{self.repository}/{self.folder}"


def select_archives(
    source_dir: Path, bios: Path | None, mmc: Path | None
) -> tuple[Path, Path]:
    if (bios is None) != (mmc is None):
        raise ValueError("Specify both --bios-archive and --mmc-archive, or neither.")
    if bios is None or mmc is None:
        files = [path for path in source_dir.iterdir() if path.is_file()]
        bios_files = [path for path in files if BIOS_NAME.match(path.name)]
        mmc_files = [path for path in files if MMC_NAME.match(path.name)]
        if len(bios_files) != 1 or len(mmc_files) != 1:
            raise ValueError(
                f"Expected exactly one BIOS and one MMC archive in {source_dir}. "
                "Use --bios-archive and --mmc-archive to select an explicit pair."
            )
        bios, mmc = bios_files[0], mmc_files[0]
    if not bios.is_file() or not BIOS_NAME.match(bios.name):
        raise ValueError(f"BIOS archive must be <four-digit release>.D<revision>.7z: {bios}")
    if not mmc.is_file() or not MMC_NAME.match(mmc.name):
        raise ValueError(f"MMC archive must be MMCFW_<major>.<minor>.<patch>.7z: {mmc}")
    return bios.resolve(), mmc.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def optional_token() -> str | None:
    """The Artifactory token if set (validated), else None for anonymous access."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return None
    if not TOKEN_FORMAT.match(token):
        raise ValueError(
            f"Invalid {TOKEN_ENV} format. Provide only the access token, "
            "without a 'Bearer' prefix."
        )
    return token


def read_token() -> str:
    """The Artifactory token, required for uploads."""
    token = optional_token()
    if token is None:
        raise ValueError(
            f"{TOKEN_ENV} is not set; no upload requests were made. "
            "Export the Artifactory access token in that environment variable."
        )
    return token


class _HashingReader:
    """Stream a file to urllib while hashing exactly the bytes that were sent."""

    def __init__(self, path: Path) -> None:
        self._file = path.open("rb")
        self.digest = hashlib.sha256()
        self.sent = 0

    def read(self, size: int = -1) -> bytes:
        block = self._file.read(1024 * 1024 if size is None or size < 0 else size)
        self.digest.update(block)
        self.sent += len(block)
        return block

    def close(self) -> None:
        self._file.close()


def _call(
    client: HttpClient, url: str, *, method: str = "GET",
    data: _HashingReader | None = None, headers: dict[str, str] | None = None,
    timeout: int = METADATA_TIMEOUT,
) -> tuple[int, str]:
    request = Request(
        url, data=data, method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {client.token}",
            **(headers or {}),
        },
    )
    try:
        with urlopen(request, timeout=timeout, context=client.ssl_context()) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def _assert_remote(status: int, body: str, plan: UploadPlan) -> None:
    if status != 200:
        raise RuntimeError(
            f"Cannot verify {plan.kind} archive (HTTP {status}): {plan.upload_url}"
        )
    metadata = json.loads(body)
    if (
        metadata.get("repo") != plan.repository
        or metadata.get("path") != f"/{plan.remote_path}"
    ):
        raise RuntimeError(f"Unexpected repository or path in {plan.kind} metadata.")
    remote_sha = str((metadata.get("checksums") or {}).get("sha256", ""))
    if (
        str(metadata.get("size")) != str(plan.length)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", remote_sha)
        or remote_sha.lower() != plan.sha256
    ):
        raise RuntimeError(
            f"Remote {plan.kind} size or SHA-256 differs from the local archive. "
            f"Refusing to overwrite or report success: {plan.upload_url}"
        )


def build_plans(bios: Path, mmc: Path, root: str = UPLOAD_ROOT) -> list[UploadPlan]:
    plans: list[UploadPlan] = []
    for kind, path, folder in (("BIOS", bios, BIOS_FOLDER), ("MMC", mmc, MMC_FOLDER)):
        length = path.stat().st_size
        if length <= 0:
            raise ValueError(f"Archive is empty: {path}")
        plan = UploadPlan(kind, path, folder, length, sha256_file(path), root=root)
        LOGGER.info(
            "UPLOAD_PLAN %s: %s -> %s; bytes=%s; SHA256=%s",
            kind, path, plan.upload_url, length, plan.sha256,
        )
        plans.append(plan)
    return plans


def upload_archives(
    bios: Path, mmc: Path, *, dry_run: bool = False, root: str = UPLOAD_ROOT,
    ca_bundle: Path | None = None,
) -> str:
    """Upload the exact BIOS/MMC pair; returns 'preview' or 'completed'."""
    plans = build_plans(bios, mmc, root)
    if dry_run:
        LOGGER.info("Upload preview only; no token read and no HTTP requests made.")
        return "preview"
    client = HttpClient(token=read_token(), ca_bundle=ca_bundle)

    for plan in plans:
        status, body = _call(client, plan.folder_url)
        if status != 200:
            raise RuntimeError(
                f"Cannot access the {plan.kind} destination folder (HTTP {status})."
            )
        metadata = json.loads(body)
        if (
            metadata.get("repo") != plan.repository
            or str(metadata.get("path", "")).rstrip("/") != f"/{plan.folder}"
        ):
            raise RuntimeError(f"Unexpected {plan.kind} destination folder metadata.")
        status, body = _call(client, plan.metadata_url)
        if status == 200:
            _assert_remote(status, body, plan)
            plan.exists = True
        elif status != 404:
            raise RuntimeError(
                f"Cannot check existing {plan.kind} archive (HTTP {status}); "
                "no uploads started."
            )

    uploaded = skipped = 0
    for plan in plans:
        if plan.exists:
            LOGGER.info(
                "SKIPPED %s: identical size and SHA-256 already present: %s",
                plan.kind, plan.upload_url,
            )
            skipped += 1
            continue
        LOGGER.info("UPLOADING %s: %s bytes -> %s", plan.kind, plan.length, plan.upload_url)
        reader = _HashingReader(plan.file)
        started = time.monotonic()
        try:
            status, _ = _call(
                client, plan.upload_url, method="PUT", data=reader,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(plan.length),
                    "X-Checksum-Sha256": plan.sha256,
                },
                timeout=UPLOAD_TIMEOUT,
            )
        finally:
            reader.close()
        if status not in (200, 201):
            raise RuntimeError(
                f"{plan.kind} upload rejected (HTTP {status}); local archives retained."
            )
        if reader.sent != plan.length or reader.digest.hexdigest() != plan.sha256:
            raise RuntimeError(
                f"{plan.kind} archive changed while uploading; refusing to report success."
            )
        status, body = _call(client, plan.metadata_url)
        _assert_remote(status, body, plan)
        LOGGER.info(
            "VERIFIED %s: %s bytes; SHA256=%s; seconds=%.1f",
            plan.kind, plan.length, plan.sha256, time.monotonic() - started,
        )
        uploaded += 1
    LOGGER.info(
        "Upload summary: uploaded=%s, identical/skipped=%s, verified=%s.",
        uploaded, skipped, len(plans),
    )
    return "completed"
