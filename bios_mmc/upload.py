"""Verified BIOS/MMC upload to Artifactory using a token from the environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import LOGGER
from .artifactory import USER_AGENT, HttpClient

DEFAULT_UPLOAD_URL = "https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL"
DEFAULT_BIOS_PATH = "FIV_Portal/IFWI_CI/BIOSID"
DEFAULT_MMC_PATH = "FIV_Portal/IFWI_CI/MMC"
TOKEN_ENV = "IFWI_ARTIFACTORY_TOKEN"
BIOS_NAME = re.compile(r"^\d{4}\.D\d+\.7z$")
MMC_NAME = re.compile(r"^MMCFW_\d+\.\d+\.\d+\.7z$")
TOKEN_FORMAT = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")
METADATA_TIMEOUT = 60
UPLOAD_TIMEOUT = 1800


@dataclass(frozen=True)
class UploadTarget:
    """Artifactory base (…/artifactory), repository and the two destination folders."""

    root: str
    repository: str
    bios_folder: str
    mmc_folder: str


def _clean_folder(value: str, option: str) -> str:
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid {option}: {value!r}")
    return "/".join(parts)


def parse_upload_target(
    url: str = DEFAULT_UPLOAD_URL,
    bios_path: str = DEFAULT_BIOS_PATH,
    mmc_path: str = DEFAULT_MMC_PATH,
) -> UploadTarget:
    """Split https://host/artifactory/<repository> into API root and repository."""
    base, marker, rest = url.rstrip("/").partition("/artifactory/")
    repository = rest.strip("/")
    if not marker or not base.startswith(("https://", "http://")) or not repository             or "/" in repository:
        raise ValueError(
            "--upload-url must look like https://<host>/artifactory/<repository> "
            f"(got {url!r}); put sub-folders in --upload-bios-path/--upload-mmc-path."
        )
    return UploadTarget(
        root=f"{base}/artifactory",
        repository=repository,
        bios_folder=_clean_folder(bios_path, "--upload-bios-path"),
        mmc_folder=_clean_folder(mmc_path, "--upload-mmc-path"),
    )


@dataclass
class UploadPlan:
    kind: str
    file: Path
    folder: str
    length: int
    sha256: str
    root: str
    repository: str
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


def build_plans(
    bios: Path | None, mmc: Path | None, target: UploadTarget,
) -> list[UploadPlan]:
    if bios is None and mmc is None:
        raise ValueError("Nothing to upload: no BIOS or MMC archive given.")
    plans: list[UploadPlan] = []
    for kind, path, folder in (
        ("BIOS", bios, target.bios_folder), ("MMC", mmc, target.mmc_folder),
    ):
        if path is None:
            continue
        length = path.stat().st_size
        if length <= 0:
            raise ValueError(f"Archive is empty: {path}")
        plan = UploadPlan(
            kind, path, folder, length, sha256_file(path),
            root=target.root, repository=target.repository,
        )
        LOGGER.info(
            "UPLOAD_PLAN %s: %s -> %s; bytes=%s; SHA256=%s",
            kind, path, plan.upload_url, length, plan.sha256,
        )
        plans.append(plan)
    return plans


def upload_archives(
    bios: Path | None, mmc: Path | None, *, target: UploadTarget | None = None,
    dry_run: bool = False, ca_bundle: Path | None = None,
) -> str:
    """Upload the given BIOS and/or MMC archive; returns 'preview' or 'completed'.

    Either archive may be None when it is already published in the target.
    """
    plans = build_plans(bios, mmc, target or parse_upload_target())
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


# --- Target repository lookups (pre-check before packaging) -------------------

@dataclass(frozen=True)
class RemoteArchive:
    kind: str
    name: str
    url: str
    size: int
    sha256: str


class TargetRepository:
    """Read-only view of the upload destination, used before packaging."""

    def __init__(self, target: UploadTarget, client: HttpClient) -> None:
        self.target = target
        self.client = client

    @classmethod
    def connect(
        cls, target: UploadTarget, ca_bundle: Path | None = None,
    ) -> "TargetRepository":
        return cls(target, HttpClient(token=read_token(), ca_bundle=ca_bundle))

    def _folder(self, kind: str) -> str:
        return self.target.bios_folder if kind == "BIOS" else self.target.mmc_folder

    def stat(self, kind: str, name: str) -> RemoteArchive | None:
        """Return the published archive's size/SHA-256, or None if it is absent."""
        remote_path = f"{self._folder(kind)}/{name}"
        root, repository = self.target.root, self.target.repository
        status, body = _call(self.client, f"{root}/api/storage/{repository}/{remote_path}")
        if status == 404:
            LOGGER.info("Target check: %s %s is not published yet.", kind, name)
            return None
        if status != 200:
            raise RuntimeError(
                f"Cannot check {kind} {name} in the upload target (HTTP {status})."
            )
        metadata = json.loads(body)
        sha256 = str((metadata.get("checksums") or {}).get("sha256", "")).lower()
        if (
            metadata.get("repo") != repository
            or metadata.get("path") != f"/{remote_path}"
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not str(metadata.get("size", "")).isdigit()
        ):
            raise RuntimeError(f"Unexpected metadata for published {kind} {name}.")
        remote = RemoteArchive(
            kind, name, f"{root}/{repository}/{remote_path}",
            int(metadata["size"]), sha256,
        )
        LOGGER.info(
            "Target check: %s %s is already published (bytes=%s, SHA256=%s).",
            kind, name, remote.size, remote.sha256,
        )
        return remote

    def obtain(self, remote: RemoteArchive, destination: Path) -> Path:
        """Ensure destination holds exactly the published archive.

        A matching local copy is reused; otherwise the published file is
        downloaded, verified against size and SHA-256, then moved into place.
        """
        if (
            destination.is_file()
            and destination.stat().st_size == remote.size
            and sha256_file(destination) == remote.sha256
        ):
            LOGGER.info("Using local %s identical to the published one: %s",
                        remote.kind, destination)
            return destination
        if destination.exists():
            LOGGER.warning(
                "Local %s differs from the published one and will be replaced: %s",
                remote.kind, destination,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Downloading published %s: %s", remote.kind, remote.url)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{remote.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(handle, "wb") as output, urlopen(
                self.client.request(remote.url), timeout=UPLOAD_TIMEOUT,
                context=self.client.ssl_context(),
            ) as response:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    output.write(block)
                    digest.update(block)
                    size += len(block)
            if size != remote.size or digest.hexdigest() != remote.sha256:
                raise RuntimeError(
                    f"Downloaded {remote.kind} {remote.name} does not match the "
                    "published size/SHA-256; nothing was replaced."
                )
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        LOGGER.info("Verified published %s saved to %s", remote.kind, destination)
        return destination
