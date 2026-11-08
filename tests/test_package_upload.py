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
REPO = "DEG-IFWI-LOCAL"


class FakeArtifactory(http.server.BaseHTTPRequestHandler):
    """Minimal Artifactory storage API: folders exist, files live in `stored`."""

    stored: dict[str, bytes] = {}
    requests: list[tuple[str, str]] = []
    status_override: dict[tuple[str, str], int] = {}
    tamper_metadata: set[str] = set()
    corrupt_downloads: set[str] = set()
    folder_repo = REPO

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _strip(self) -> None:
        self.path = self.path.removeprefix("/artifactory")

    def do_GET(self) -> None:  # noqa: N802
        self._strip()
        cls = type(self)
        cls.requests.append(("GET", self.path))
        if (override := cls.status_override.get(("GET", self.path))) is not None:
            return self._reply(override, b"{}")
        if not self._authorized():
            return self._reply(401, b"{}")
        raw_prefix = f"/{REPO}/"
        if self.path.startswith(raw_prefix):
            data = cls.stored.get(self.path[len(raw_prefix):])
            if data is None:
                return self._reply(404, b"{}")
            if self.path[len(raw_prefix):] in cls.corrupt_downloads:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            return self._reply(200, data)
        prefix = f"/api/storage/{REPO}/"
        if not self.path.startswith(prefix):
            return self._reply(404)
        remote = self.path[len(prefix):]
        if remote in (upload.DEFAULT_BIOS_PATH, upload.DEFAULT_MMC_PATH, "Custom/BIOS", "Custom/MMC"):
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
        self._strip()
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
        FakeArtifactory.corrupt_downloads = set()
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

    def target(self, **paths: str) -> upload.UploadTarget:
        return upload.parse_upload_target(f"{self.root_url}/artifactory/{REPO}", **paths)

    def run_upload(self, **kwargs: object) -> str:
        kwargs.setdefault("target", self.target())
        return upload.upload_archives(self.bios, self.mmc, **kwargs)

    def remote(self, folder: str, path: Path) -> str:
        return f"{folder}/{path.name}"

    def puts(self) -> list[str]:
        return [path for method, path in FakeArtifactory.requests if method == "PUT"]

    def test_uploads_and_verifies_both_exact_files(self) -> None:
        with self.assertLogs("bios_packager", level="INFO") as logged:
            self.assertEqual(self.run_upload(), "completed")
        self.assertEqual(
            FakeArtifactory.stored[self.remote(upload.DEFAULT_BIOS_PATH, self.bios)],
            self.bios.read_bytes(),
        )
        self.assertEqual(
            FakeArtifactory.stored[self.remote(upload.DEFAULT_MMC_PATH, self.mmc)],
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
        for folder, path in ((upload.DEFAULT_BIOS_PATH, self.bios), (upload.DEFAULT_MMC_PATH, self.mmc)):
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
                    ("GET", f"/api/storage/{REPO}/{upload.DEFAULT_BIOS_PATH}"): status
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
        FakeArtifactory.tamper_metadata = {self.remote(upload.DEFAULT_BIOS_PATH, self.bios)}
        original = FakeArtifactory.do_GET
        checks = 0

        def tamper_after_upload(handler: FakeArtifactory) -> None:
            nonlocal checks
            remote = handler.path.split(f"/api/storage/{REPO}/", 1)[-1]
            if remote == self.remote(upload.DEFAULT_BIOS_PATH, self.bios):
                checks += 1
                if checks == 1:  # preflight: not present yet
                    FakeArtifactory.requests.append(("GET", handler.path))
                    return handler._reply(404, b"{}")
            original(handler)

        with mock.patch.object(FakeArtifactory, "do_GET", tamper_after_upload):
            with self.assertRaisesRegex(RuntimeError, "SHA-256 differs"):
                self.run_upload()

    def test_custom_destination_folders_are_used_and_verified(self) -> None:
        target = self.target(bios_path="Custom/BIOS", mmc_path="Custom/MMC")
        self.assertEqual(self.run_upload(target=target), "completed")
        self.assertEqual(
            sorted(FakeArtifactory.stored),
            [f"Custom/BIOS/{self.bios.name}", f"Custom/MMC/{self.mmc.name}"],
        )

    def test_single_archive_upload_when_the_other_is_already_published(self) -> None:
        for bios, mmc, expected in (
            (self.bios, None, f"{upload.DEFAULT_BIOS_PATH}/{self.bios.name}"),
            (None, self.mmc, f"{upload.DEFAULT_MMC_PATH}/{self.mmc.name}"),
        ):
            with self.subTest(expected=expected):
                FakeArtifactory.stored = {}
                self.assertEqual(
                    upload.upload_archives(bios, mmc, target=self.target()), "completed"
                )
                self.assertEqual(list(FakeArtifactory.stored), [expected])
        with self.assertRaisesRegex(ValueError, "Nothing to upload"):
            upload.upload_archives(None, None, target=self.target())

    def repository(self) -> upload.TargetRepository:
        return upload.TargetRepository.connect(self.target())

    def test_stat_reports_published_archive_or_none(self) -> None:
        remote = self.remote(upload.DEFAULT_BIOS_PATH, self.bios)
        FakeArtifactory.stored = {remote: self.bios.read_bytes()}
        repo = self.repository()
        found = repo.stat("BIOS", self.bios.name)
        self.assertEqual(found.size, self.bios.stat().st_size)
        self.assertEqual(found.sha256, hashlib.sha256(self.bios.read_bytes()).hexdigest())
        self.assertIsNone(repo.stat("MMC", self.mmc.name))
        self.assertEqual(self.puts(), [])

    def test_stat_errors_are_not_treated_as_absent(self) -> None:
        path = f"/api/storage/{REPO}/{upload.DEFAULT_BIOS_PATH}/{self.bios.name}"
        FakeArtifactory.status_override = {("GET", path): 403}
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            self.repository().stat("BIOS", self.bios.name)

    def test_connect_requires_token(self) -> None:
        with mock.patch.dict("os.environ", {upload.TOKEN_ENV: ""}):
            with self.assertRaisesRegex(ValueError, "is not set"):
                self.repository()
        self.assertEqual(FakeArtifactory.requests, [])

    def test_obtain_reuses_identical_local_copy_without_downloading(self) -> None:
        remote_path = self.remote(upload.DEFAULT_BIOS_PATH, self.bios)
        FakeArtifactory.stored = {remote_path: self.bios.read_bytes()}
        repo = self.repository()
        published = repo.stat("BIOS", self.bios.name)
        FakeArtifactory.requests = []
        self.assertEqual(repo.obtain(published, self.bios), self.bios)
        self.assertEqual(FakeArtifactory.requests, [])

    def test_obtain_downloads_and_replaces_missing_or_different_local_copy(self) -> None:
        published_bytes = b"P" * 4096
        remote_path = f"{upload.DEFAULT_BIOS_PATH}/{self.bios.name}"
        FakeArtifactory.stored = {remote_path: published_bytes}
        repo = self.repository()
        published = repo.stat("BIOS", self.bios.name)
        for prepare in (lambda: self.bios.unlink(), lambda: self.bios.write_bytes(b"other")):
            with self.subTest(prepare=prepare):
                prepare()
                self.assertEqual(repo.obtain(published, self.bios), self.bios)
                self.assertEqual(self.bios.read_bytes(), published_bytes)
        self.assertEqual(
            [p.name for p in self.source.iterdir() if p.name.startswith(".")], []
        )

    def test_corrupt_download_never_replaces_local_file(self) -> None:
        remote_path = f"{upload.DEFAULT_BIOS_PATH}/{self.bios.name}"
        FakeArtifactory.stored = {remote_path: b"P" * 4096}
        FakeArtifactory.corrupt_downloads = {remote_path}
        repo = self.repository()
        published = repo.stat("BIOS", self.bios.name)
        original = self.bios.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            repo.obtain(published, self.bios)
        self.assertEqual(self.bios.read_bytes(), original)
        self.assertEqual(
            [p.name for p in self.source.iterdir() if p.name.startswith(".")], []
        )

    def test_failed_second_upload_keeps_verified_bios_and_retry_skips_it(self) -> None:
        mmc_put = f"/{REPO}/{self.remote(upload.DEFAULT_MMC_PATH, self.mmc)}"
        FakeArtifactory.status_override = {("PUT", mmc_put): 500}
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            self.run_upload()
        self.assertIn(self.remote(upload.DEFAULT_BIOS_PATH, self.bios), FakeArtifactory.stored)
        FakeArtifactory.status_override = {}
        FakeArtifactory.requests = []
        self.run_upload()
        self.assertEqual(self.puts(), [mmc_put])


class UploadTargetTests(unittest.TestCase):
    def test_defaults_match_the_fiv_portal_destination(self) -> None:
        target = upload.parse_upload_target()
        self.assertEqual(target.root, "https://af01p-sc.devtools.intel.com/artifactory")
        self.assertEqual(target.repository, "DEG-IFWI-LOCAL")
        self.assertEqual(
            (target.bios_folder, target.mmc_folder),
            ("FIV_Portal/IFWI_CI/BIOSID", "FIV_Portal/IFWI_CI/MMC"),
        )

    def test_custom_url_and_folders_are_normalized(self) -> None:
        target = upload.parse_upload_target(
            "https://host.example/artifactory/MY-REPO/", "/a/b/", "a\c"
        )
        self.assertEqual((target.root, target.repository), (
            "https://host.example/artifactory", "MY-REPO"))
        self.assertEqual((target.bios_folder, target.mmc_folder), ("a/b", "a/c"))

    def test_invalid_url_or_folder_is_rejected(self) -> None:
        for url, bios, message in (
            ("https://host/DEG-IFWI-LOCAL", "x", "--upload-url"),
            ("https://host/artifactory/", "x", "--upload-url"),
            ("https://host/artifactory/REPO/sub", "x", "--upload-url"),
            ("ftp://host/artifactory/REPO", "x", "--upload-url"),
            ("https://host/artifactory/REPO", "a/../b", "--upload-bios-path"),
            ("https://host/artifactory/REPO", "", "--upload-bios-path"),
        ):
            with self.subTest(url=url, bios=bios):
                with self.assertRaisesRegex(ValueError, message):
                    upload.parse_upload_target(url, bios, "MMC")


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
