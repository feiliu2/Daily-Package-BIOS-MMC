from __future__ import annotations

import hashlib
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import seven_zip_support  # noqa: F401  (adds the repository root to sys.path)

from bios_mmc import upload

TOKEN = "AKCp8fixture-token_value.1"
REPO = upload.UPLOAD_REPOSITORY


class FakeArtifactory(http.server.BaseHTTPRequestHandler):
    """Minimal Artifactory storage API: folders exist, files live in `stored`."""

    stored: dict[str, bytes] = {}
    requests: list[tuple[str, str]] = []
    status_override: dict[tuple[str, str], int] = {}
    tamper_metadata: set[str] = set()
    folder_repo = REPO

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self) -> None:  # noqa: N802
        cls = type(self)
        cls.requests.append(("GET", self.path))
        if (override := cls.status_override.get(("GET", self.path))) is not None:
            return self._reply(override, b"{}")
        if not self._authorized():
            return self._reply(401, b"{}")
        prefix = f"/api/storage/{REPO}/"
        if not self.path.startswith(prefix):
            return self._reply(404)
        remote = self.path[len(prefix):]
        if remote in (upload.BIOS_FOLDER, upload.MMC_FOLDER):
            body = {"repo": cls.folder_repo, "path": f"/{remote}"}
            return self._reply(200, json.dumps(body).encode())
        if remote not in cls.stored:
            return self._reply(404, b"{}")
        data = cls.stored[remote]
        sha = hashlib.sha256(data).hexdigest()
        if remote in cls.tamper_metadata:
            sha = "0" * 64
        body = {
            "repo": REPO, "path": f"/{remote}", "size": str(len(data)),
            "checksums": {"sha256": sha},
        }
        self._reply(200, json.dumps(body).encode())

    def do_PUT(self) -> None:  # noqa: N802
        cls = type(self)
        cls.requests.append(("PUT", self.path))
        length = int(self.headers["Content-Length"])
        data = self.rfile.read(length)
        if (override := cls.status_override.get(("PUT", self.path))) is not None:
            return self._reply(override, b"{}")
        if not self._authorized():
            return self._reply(401, b"{}")
        if self.headers.get("X-Checksum-Sha256") != hashlib.sha256(data).hexdigest():
            return self._reply(409, b"checksum mismatch")
        cls.stored[self.path[len(f"/{REPO}/"):]] = data
        self._reply(201, b"{}")

    def log_message(self, *args: object) -> None:
        pass


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeArtifactory.stored = {}
        FakeArtifactory.requests = []
        FakeArtifactory.status_override = {}
        FakeArtifactory.tamper_metadata = set()
        FakeArtifactory.folder_repo = REPO
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeArtifactory)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.root_url = f"http://127.0.0.1:{self.server.server_port}"
        temporary = tempfile.TemporaryDirectory(prefix="upload tests [x] ")
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.bios = self.source / "0040.D82.7z"
        self.mmc = self.source / "MMCFW_0.1022.0.7z"
        self.bios.write_bytes(b"B" * (2 * 1024 * 1024 + 3))
        self.mmc.write_bytes(b"M" * 2100)
        environment = mock.patch.dict("os.environ", {upload.TOKEN_ENV: TOKEN})
        environment.start()
        self.addCleanup(environment.stop)

    def run_upload(self, **kwargs: object) -> str:
        return upload.upload_archives(self.bios, self.mmc, root=self.root_url, **kwargs)

    def remote(self, folder: str, path: Path) -> str:
        return f"{folder}/{path.name}"

    def puts(self) -> list[str]:
        return [path for method, path in FakeArtifactory.requests if method == "PUT"]

    def test_uploads_and_verifies_both_exact_files(self) -> None:
        with self.assertLogs("bios_packager", level="INFO") as logged:
            self.assertEqual(self.run_upload(), "completed")
        self.assertEqual(
            FakeArtifactory.stored[self.remote(upload.BIOS_FOLDER, self.bios)],
            self.bios.read_bytes(),
        )
        self.assertEqual(
            FakeArtifactory.stored[self.remote(upload.MMC_FOLDER, self.mmc)],
            self.mmc.read_bytes(),
        )
        log = "\n".join(logged.output)
        self.assertIn("VERIFIED BIOS", log)
        self.assertIn("uploaded=2, identical/skipped=0", log)
        self.assertNotIn(TOKEN, log)

    def test_identical_existing_files_are_verified_and_skipped(self) -> None:
        self.run_upload()
        FakeArtifactory.requests = []
        with self.assertLogs("bios_packager", level="INFO") as logged:
            self.run_upload()
        self.assertEqual(self.puts(), [])
        self.assertIn("uploaded=0, identical/skipped=2", "\n".join(logged.output))

    def test_any_existing_conflict_stops_before_uploading_either_file(self) -> None:
        for folder, path in ((upload.BIOS_FOLDER, self.bios), (upload.MMC_FOLDER, self.mmc)):
            with self.subTest(conflict=path.name):
                FakeArtifactory.stored = {self.remote(folder, path): b"different"}
                FakeArtifactory.requests = []
                with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                    self.run_upload()
                self.assertEqual(self.puts(), [])

    def test_missing_or_malformed_token_makes_no_requests(self) -> None:
        for value, message in (("", "is not set"), ("Bearer abc", "Invalid")):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {upload.TOKEN_ENV: value}):
                    with self.assertRaisesRegex(ValueError, message):
                        self.run_upload()
        self.assertEqual(FakeArtifactory.requests, [])

    def test_dry_run_needs_no_token_and_makes_no_requests(self) -> None:
        with mock.patch.dict("os.environ", {upload.TOKEN_ENV: ""}):
            self.assertEqual(self.run_upload(dry_run=True), "preview")
        self.assertEqual(FakeArtifactory.requests, [])

    def test_authentication_or_permission_failure_never_uploads(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                FakeArtifactory.status_override = {
                    ("GET", f"/api/storage/{REPO}/{upload.BIOS_FOLDER}"): status
                }
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    self.run_upload()
                self.assertEqual(self.puts(), [])

    def test_unexpected_destination_metadata_never_uploads(self) -> None:
        FakeArtifactory.folder_repo = "OTHER-REPO"
        with self.assertRaisesRegex(RuntimeError, "destination folder metadata"):
            self.run_upload()
        self.assertEqual(self.puts(), [])

    def test_wrong_remote_checksum_after_put_is_not_reported_as_success(self) -> None:
        FakeArtifactory.tamper_metadata = {self.remote(upload.BIOS_FOLDER, self.bios)}
        original = FakeArtifactory.do_GET
        checks = 0

        def tamper_after_upload(handler: FakeArtifactory) -> None:
            nonlocal checks
            remote = handler.path.split(f"/api/storage/{REPO}/", 1)[-1]
            if remote == self.remote(upload.BIOS_FOLDER, self.bios):
                checks += 1
                if checks == 1:  # preflight: not present yet
                    FakeArtifactory.requests.append(("GET", handler.path))
                    return handler._reply(404, b"{}")
            original(handler)

        with mock.patch.object(FakeArtifactory, "do_GET", tamper_after_upload):
            with self.assertRaisesRegex(RuntimeError, "SHA-256 differs"):
                self.run_upload()

    def test_failed_second_upload_keeps_verified_bios_and_retry_skips_it(self) -> None:
        mmc_put = f"/{REPO}/{self.remote(upload.MMC_FOLDER, self.mmc)}"
        FakeArtifactory.status_override = {("PUT", mmc_put): 500}
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            self.run_upload()
        self.assertIn(self.remote(upload.BIOS_FOLDER, self.bios), FakeArtifactory.stored)
        FakeArtifactory.status_override = {}
        FakeArtifactory.requests = []
        self.run_upload()
        self.assertEqual(self.puts(), [mmc_put])


class SelectionTests(unittest.TestCase):
    def test_directory_selection_requires_exactly_one_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "0040.D82.7z").write_bytes(b"b")
            with self.assertRaisesRegex(ValueError, "exactly one BIOS and one MMC"):
                upload.select_archives(source, None, None)
            (source / "MMCFW_0.1022.0.7z").write_bytes(b"m")
            bios, mmc = upload.select_archives(source, None, None)
            self.assertEqual((bios.name, mmc.name), ("0040.D82.7z", "MMCFW_0.1022.0.7z"))
            (source / "0040.D83.7z").write_bytes(b"b")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                upload.select_archives(source, None, None)
            explicit = upload.select_archives(
                source, source / "0040.D83.7z", source / "MMCFW_0.1022.0.7z"
            )
            self.assertEqual(explicit[0].name, "0040.D83.7z")
            with self.assertRaisesRegex(ValueError, "both"):
                upload.select_archives(source, source / "0040.D83.7z", None)
            (source / "bios.7z").write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "four-digit"):
                upload.select_archives(
                    source, source / "bios.7z", source / "MMCFW_0.1022.0.7z"
                )


if __name__ == "__main__":
    unittest.main()
