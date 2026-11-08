"""Machine-readable run result written by --result-json.

Schema (version 1)::

    {
      "schema_version": 1,
      "command": "all" | "bios" | "mmc" | "upload",
      "status": "success" | "failed",
      "exit_code": 0 | 1,
      "dry_run": bool,
      "error": str | null,             # first line of the failure, if any
      "started_at": ISO-8601 UTC, "finished_at": ISO-8601 UTC,
      "bios_id": "0040D90" | null,
      "mmc_version": "0.1027.0" | null,
      "upload_target": "https://<host>/artifactory/<repo>" | null,
      "mapping": {                    # null when no BIOS was packaged in this run
        "kind": "github" | "url" | "local", "location": API URL or path,
        "repo", "path", "ref": str | null,           # set for kind "github"
        "sha256", "component_packages", "file_rows"  # null until it was read
      } | null,
      "bios": Artifact, "mmc": Artifact
    }

    Artifact = {
      "name": file name | null,
      "path": absolute local path | null,   # null when no local file exists
      "sha256": hex | null, "size": bytes | null,
      "packaging": "packaged" | "skipped_published" | "provided" | "planned" | "not_run",
      "upload": "uploaded" | "already_present" | "skipped_published" | "preview"
                | "not_requested" | "failed" | "not_run",
      "skipped": bool,                      # true: already published, not rebuilt
      "remote_url": URL in the upload target | null
    }

For a skipped (already published) archive, sha256/size describe the published
file. A reused published BIOS has its local path and the same sha256.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .upload import RemoteArchive, sha256_file

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Artifact:
    name: str | None = None
    path: str | None = None
    sha256: str | None = None
    size: int | None = None
    packaging: str = "not_run"
    upload: str = "not_run"
    skipped: bool = False
    remote_url: str | None = None


@dataclass
class RunReport:
    command: str
    output: Path | None = None
    dry_run: bool = False
    status: str = "failed"
    exit_code: int = 1
    error: str | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    bios_id: str | None = None
    mmc_version: str | None = None
    upload_target: str | None = None
    mapping: dict[str, object] | None = None
    bios: Artifact = field(default_factory=Artifact)
    mmc: Artifact = field(default_factory=Artifact)

    @property
    def enabled(self) -> bool:
        return self.output is not None

    def local_file(self, artifact: Artifact, path: Path, packaging: str) -> None:
        """Record a local archive; hashes it only when a result file was requested."""
        artifact.name = path.name
        artifact.path = str(path.resolve())
        artifact.packaging = packaging
        if self.enabled and path.is_file():
            artifact.size = path.stat().st_size
            artifact.sha256 = sha256_file(path)

    def planned(self, artifact: Artifact, path: Path) -> None:
        artifact.name = path.name
        artifact.path = str(path.resolve())
        artifact.packaging = "planned"

    def published(self, artifact: Artifact, remote: RemoteArchive) -> None:
        artifact.name = remote.name
        artifact.sha256 = remote.sha256
        artifact.size = remote.size
        artifact.remote_url = remote.url
        artifact.packaging = "skipped_published"
        artifact.upload = "skipped_published"
        artifact.skipped = True

    def fail(self, error: BaseException) -> None:
        text = str(error).strip().splitlines()
        self.error = text[0] if text else type(error).__name__

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("output")
        return {"schema_version": SCHEMA_VERSION, **data}

    def finish(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.status = "success" if exit_code == 0 else "failed"
        self.finished_at = _now()
        if self.output is None:
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{self.output.name}.", dir=self.output.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.to_dict(), stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            Path(temporary).replace(self.output)
        finally:
            Path(temporary).unlink(missing_ok=True)
