from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import package_latest_bios as packager


POWERSHELL = shutil.which("powershell.exe")
CURL = shutil.which("curl.exe")
UPLOAD_SCRIPT = packager.UPLOAD_SCRIPT
TOKEN = "fixture-token-not-a-secret"
REPOSITORY = "DEG-IFWI-LOCAL"
BIOS_FOLDER = "FIV_Portal/IFWI_CI/BIOSID"
MMC_FOLDER = "FIV_Portal/IFWI_CI/MMC"
BIOS_NAME = "0040.D82.7z"
MMC_NAME = "MMCFW_0.1022.0.7z"
BIOS_PATH = f"{BIOS_FOLDER}/{BIOS_NAME}"
MMC_PATH = f"{MMC_FOLDER}/{MMC_NAME}"


def ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL and CURL, "Windows PowerShell and curl.exe required")
class NativeUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="upload-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source's [archive] folder"
        self.source.mkdir()
        self.bios = self.source / BIOS_NAME
        self.mmc = self.source / MMC_NAME
        self.contents = {
            BIOS_PATH: bytes(range(256)) * 8192,
            MMC_PATH: b"synthetic MMC archive" * 100,
        }
        self.bios.write_bytes(self.contents[BIOS_PATH])
        self.mmc.write_bytes(self.contents[MMC_PATH])
        self.remote: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.auth_headers: list[str | None] = []
        self.checksum_headers: list[tuple[str | None, str]] = []
        self.auth_status = 200
        self.failed_put: str | None = None
        self.corrupt_verified_hash = False
        self.bad_folder_path = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def respond(self, status: int, data: object) -> None:
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def authorized(self) -> bool:
                auth = self.headers.get("Authorization")
                owner.auth_headers.append(auth)
                status = owner.auth_status
                if auth != f"Bearer {TOKEN}":
                    status = 401
                if status != 200:
                    self.respond(status, {"errors": [{"message": "Access denied"}]})
                    return False
                return True

            def do_GET(self) -> None:
                owner.requests.append(("GET", self.path))
                if not self.authorized():
                    return
                prefix = f"/artifactory/api/storage/{REPOSITORY}/"
                if not self.path.startswith(prefix):
                    self.respond(400, {"errors": [{"message": "Unexpected URL"}]})
                    return
                path = self.path.removeprefix(prefix)
                if path in (BIOS_FOLDER, MMC_FOLDER):
                    self.respond(200, {
                        "repo": REPOSITORY,
                        "path": "/wrong-folder" if owner.bad_folder_path else f"/{path}",
                        "children": [],
                    })
                elif path in owner.remote:
                    data = owner.remote[path]
                    checksum = hashlib.sha256(data).hexdigest()
                    if owner.corrupt_verified_hash:
                        checksum = "0" * 64
                    self.respond(200, {
                        "repo": REPOSITORY,
                        "path": f"/{path}",
                        "size": str(len(data)),
                        "checksums": {"sha256": checksum},
                    })
                else:
                    self.respond(404, {"errors": [{"message": "Not found"}]})

            def do_PUT(self) -> None:
                owner.requests.append(("PUT", self.path))
                if not self.authorized():
                    return
                prefix = f"/artifactory/{REPOSITORY}/"
                path = self.path.removeprefix(prefix)
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                digest = hashlib.sha256(data).hexdigest()
                checksum = self.headers.get("X-Checksum-Sha256")
                owner.checksum_headers.append((checksum, digest))
                if path == owner.failed_put:
                    self.respond(500, {"errors": [{"message": "Synthetic upload failure"}]})
                elif not self.path.startswith(prefix) or path not in owner.contents:
                    self.respond(400, {"errors": [{"message": "Wrong destination"}]})
                elif checksum is None or checksum.lower() != digest:
                    self.respond(409, {"errors": [{"message": "Wrong checksum"}]})
                else:
                    owner.remote[path] = data
                    self.respond(201, {"repo": REPOSITORY, "path": f"/{path}"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def run_upload(
        self,
        *,
        approve: bool = True,
        token: str | None = TOKEN,
        what_if: bool = False,
        arguments: str = "",
        confirmation_extra: str = "",
    ) -> subprocess.CompletedProcess[str]:
        assert POWERSHELL is not None
        port = self.server.server_address[1]
        token_code = (
            f"return (ConvertTo-SecureString {ps_quote(token)} -AsPlainText -Force)"
            if token is not None else "return $null"
        )
        code = f"""
. {ps_quote(UPLOAD_SCRIPT)}
$realRequest = ${{function:Invoke-ArtifactoryRequest}}
function Confirm-ArtifactoryUpload {{
    param([object[]]$Plans)
    [Console]::WriteLine('EVENT:confirm')
    {confirmation_extra}
    return ${str(approve).lower()}
}}
function Read-ArtifactoryToken {{
    [Console]::WriteLine('EVENT:token')
    {token_code}
}}
function Invoke-ArtifactoryRequest {{
    param([string]$Uri, [string]$AuthConfig, [System.IO.FileInfo]$File, [string]$Sha256)
    [Console]::WriteLine('EVENT:request')
    $localUri = 'http://127.0.0.1:{port}' + ([uri]$Uri).PathAndQuery
    & $realRequest -Uri $localUri -AuthConfig $AuthConfig -File $File -Sha256 $Sha256
}}
Invoke-BiosMmcUpload -SourceDirectory {ps_quote(self.source)} {arguments} {'-WhatIf' if what_if else ''}
foreach ($path in @({ps_quote(self.bios)}, {ps_quote(self.mmc)})) {{
    $handle = [System.IO.File]::Open($path, 'Open', 'ReadWrite', 'None')
    $handle.Dispose()
}}
[Console]::WriteLine('SOURCE_HANDLES_RELEASED')
"""
        environment = os.environ.copy()
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        result = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=60,
            check=False,
        )
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        return result

    def assert_sources_unchanged(self) -> None:
        self.assertEqual(self.bios.read_bytes(), self.contents[BIOS_PATH])
        self.assertEqual(self.mmc.read_bytes(), self.contents[MMC_PATH])
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), [BIOS_NAME, MMC_NAME])

    def test_preview_has_no_confirmation_token_or_http_requests(self) -> None:
        result = self.run_upload(what_if=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UPLOAD_RESULT=preview", result.stdout)
        for path in (BIOS_PATH, MMC_PATH):
            self.assertIn(path, result.stdout)
        self.assertNotIn("EVENT:", result.stdout)
        self.assertIn("SOURCE_HANDLES_RELEASED", result.stdout)
        self.assertEqual(self.requests, [])
        self.assert_sources_unchanged()

    def test_declining_precedes_credentials_and_all_network_activity(self) -> None:
        result = self.run_upload(approve=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EVENT:confirm", result.stdout)
        self.assertNotIn("EVENT:token", result.stdout)
        self.assertNotIn("EVENT:request", result.stdout)
        self.assertIn("UPLOAD_RESULT=cancelled", result.stdout)
        self.assertIn("SOURCE_HANDLES_RELEASED", result.stdout)
        self.assertEqual(self.requests, [])
        self.assert_sources_unchanged()

    def test_cancelled_token_does_not_make_http_requests(self) -> None:
        result = self.run_upload(token=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EVENT:token", result.stdout)
        self.assertIn("UPLOAD_RESULT=cancelled", result.stdout)
        self.assertNotIn("EVENT:request", result.stdout)
        self.assertEqual(self.requests, [])

    def test_native_curl_uploads_and_verifies_both_exact_files_with_one_token(self) -> None:
        result = self.run_upload()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("EVENT:confirm"), 1)
        self.assertEqual(result.stdout.count("EVENT:token"), 1)
        self.assertLess(result.stdout.index("EVENT:confirm"), result.stdout.index("EVENT:token"))
        self.assertLess(result.stdout.index("EVENT:token"), result.stdout.index("EVENT:request"))
        self.assertIn("uploaded=2, identical/skipped=0, verified=2", result.stdout)
        self.assertIn("UPLOAD_RESULT=completed", result.stdout)
        self.assertEqual(self.remote, self.contents)
        puts = [path for method, path in self.requests if method == "PUT"]
        self.assertEqual(puts, [
            f"/artifactory/{REPOSITORY}/{BIOS_PATH}",
            f"/artifactory/{REPOSITORY}/{MMC_PATH}",
        ])
        self.assertTrue(all(header == f"Bearer {TOKEN}" for header in self.auth_headers))
        self.assertTrue(all(header and header.lower() == digest for header, digest in self.checksum_headers))
        self.assertIn("SOURCE_HANDLES_RELEASED", result.stdout)
        self.assert_sources_unchanged()

    def test_identical_existing_files_are_verified_and_skipped(self) -> None:
        self.remote.update(self.contents)
        result = self.run_upload()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uploaded=0, identical/skipped=2, verified=2", result.stdout)
        self.assertFalse(any(method == "PUT" for method, _ in self.requests))
        self.assert_sources_unchanged()

    def test_any_existing_conflict_stops_before_uploading_either_file(self) -> None:
        for conflict in (BIOS_PATH, MMC_PATH):
            with self.subTest(conflict=conflict):
                self.requests.clear()
                different = b"x" * len(self.contents[conflict])
                self.remote = {conflict: different}
                result = self.run_upload()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to overwrite", result.stdout + result.stderr)
                self.assertNotIn("UPLOAD_RESULT=completed", result.stdout)
                self.assertFalse(any(method == "PUT" for method, _ in self.requests))
                self.assertEqual(self.remote, {conflict: different})
        self.assert_sources_unchanged()

    def test_authentication_or_permission_failure_never_uploads(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                self.auth_status = status
                result = self.run_upload()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"HTTP {status}", result.stdout + result.stderr)
                self.assertNotIn("UPLOAD_RESULT=completed", result.stdout)
        self.assertFalse(any(method == "PUT" for method, _ in self.requests))
        self.assert_sources_unchanged()

    def test_failed_second_upload_keeps_verified_bios_and_retry_skips_it(self) -> None:
        self.failed_put = MMC_PATH
        first = self.run_upload()
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("HTTP 500", first.stdout + first.stderr)
        self.assertNotIn("UPLOAD_RESULT=completed", first.stdout)
        self.assertEqual(self.remote, {BIOS_PATH: self.contents[BIOS_PATH]})
        self.failed_put = None
        self.requests.clear()
        retry = self.run_upload()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("uploaded=1, identical/skipped=1, verified=2", retry.stdout)
        self.assertEqual(self.remote, self.contents)
        self.assertEqual([path for method, path in self.requests if method == "PUT"], [
            f"/artifactory/{REPOSITORY}/{MMC_PATH}"
        ])
        self.assert_sources_unchanged()

    def test_wrong_remote_checksum_after_put_is_not_reported_as_success(self) -> None:
        self.corrupt_verified_hash = True
        result = self.run_upload()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-256 differs", result.stdout + result.stderr)
        self.assertNotIn("UPLOAD_RESULT=completed", result.stdout)
        self.assertNotIn(MMC_PATH, self.remote)
        self.assert_sources_unchanged()

    def test_unexpected_destination_metadata_never_uploads(self) -> None:
        self.bad_folder_path = True
        result = self.run_upload()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination folder metadata", result.stdout + result.stderr)
        self.assertFalse(any(method == "PUT" for method, _ in self.requests))

    def test_ambiguous_directory_fails_without_confirmation_or_network(self) -> None:
        extra = self.source / "0040.D81.7z"
        extra.write_bytes(b"old BIOS")
        result = self.run_upload()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly one BIOS and one MMC", result.stdout + result.stderr)
        self.assertNotIn("EVENT:", result.stdout)
        self.assertEqual(self.requests, [])

    def test_explicit_pair_works_even_with_other_archives(self) -> None:
        (self.source / "0040.D81.7z").write_bytes(b"old BIOS")
        result = self.run_upload(
            arguments=f"-BiosArchive {ps_quote(self.bios)} -MmcArchive {ps_quote(self.mmc)}"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote, self.contents)

    def test_only_one_explicit_path_is_rejected(self) -> None:
        result = self.run_upload(arguments=f"-BiosArchive {ps_quote(self.bios)}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Specify both", result.stdout + result.stderr)
        self.assertNotIn("EVENT:", result.stdout)
        self.assertEqual(self.requests, [])

    def test_empty_archive_is_rejected_before_confirmation(self) -> None:
        self.mmc.write_bytes(b"")
        result = self.run_upload()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Archive is empty", result.stdout + result.stderr)
        self.assertNotIn("EVENT:", result.stdout)
        self.assertEqual(self.requests, [])

    def test_invalid_token_is_rejected_without_disclosure_or_requests(self) -> None:
        invalid = "invalid token with whitespace"
        result = self.run_upload(token=invalid)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid token format", result.stdout + result.stderr)
        self.assertNotIn(invalid, result.stdout + result.stderr)
        self.assertEqual(self.requests, [])

    def test_sources_are_locked_during_confirmation_and_released_after_declining(self) -> None:
        extra = """
foreach ($plan in $Plans) {
    try {
        $handle = [System.IO.File]::Open($plan.File.FullName, 'Open', 'Write', 'ReadWrite')
        $handle.Dispose()
        throw 'Source was unexpectedly writable.'
    } catch [System.IO.IOException] {
        [Console]::WriteLine('SOURCE_WRITE_BLOCKED')
    }
}
"""
        result = self.run_upload(approve=False, confirmation_extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("SOURCE_WRITE_BLOCKED"), 2)
        self.assertIn("SOURCE_HANDLES_RELEASED", result.stdout)
        self.assert_sources_unchanged()


@unittest.skipUnless(POWERSHELL, "Windows PowerShell required")
class UploadLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="upload-launcher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "\u56fa\u4ef6 tools [fixture]"
        self.root.mkdir()
        self.bios = self.root / BIOS_NAME
        self.mmc = self.root / MMC_NAME
        self.script = self.root / "Upload-BiosMmc.ps1"
        self.bios.write_bytes(b"BIOS")
        self.mmc.write_bytes(b"MMC")
        patch = mock.patch.object(packager, "UPLOAD_SCRIPT", self.script)
        patch.start()
        self.addCleanup(patch.stop)

    def write_script(self, tail: str) -> None:
        self.script.write_text(
            "param([string]$BiosArchive, [string]$MmcArchive)\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "Write-Output ('BIOS_PATH=' + $BiosArchive)\n"
            "Write-Output ('MMC_PATH=' + $MmcArchive)\n" + tail,
            encoding="ascii",
        )

    def test_real_child_receives_exact_pair_and_logs_result(self) -> None:
        self.write_script("Write-Output 'UPLOAD_RESULT=completed'\n")
        with self.assertLogs("bios_packager", level="INFO") as logged:
            self.assertTrue(packager.upload_archives(self.bios, self.mmc))
        text = "\n".join(logged.output)
        self.assertIn(f"BIOS_PATH={self.bios}", text)
        self.assertIn(f"MMC_PATH={self.mmc}", text)
        self.assertIn("upload/verification completed", text)

    def test_cancelled_child_is_distinct_from_completed_upload(self) -> None:
        self.write_script("Write-Output 'UPLOAD_RESULT=cancelled'\n")
        with self.assertLogs("bios_packager", level="INFO") as logged:
            self.assertFalse(packager.upload_archives(self.bios, self.mmc))
        self.assertIn("Upload cancelled", "\n".join(logged.output))
        self.assertNotIn("upload/verification completed", "\n".join(logged.output))

    def test_nonzero_child_is_not_success_even_with_a_completed_marker(self) -> None:
        self.write_script("Write-Output 'UPLOAD_RESULT=completed'\nexit 1\n")
        with self.assertRaises(subprocess.CalledProcessError):
            packager.upload_archives(self.bios, self.mmc)

    def test_missing_unknown_and_duplicate_results_are_rejected(self) -> None:
        for tail in (
            "",
            "Write-Output 'UPLOAD_RESULT=preview'\n",
            "Write-Output 'UPLOAD_RESULT=completed'\nWrite-Output 'UPLOAD_RESULT=cancelled'\n",
        ):
            with self.subTest(tail=tail):
                self.write_script(tail)
                with self.assertRaisesRegex(RuntimeError, "one valid"):
                    packager.upload_archives(self.bios, self.mmc)

    def test_missing_adjacent_upload_script_is_an_explicit_error(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "Upload script not found"):
            packager.upload_archives(self.bios, self.mmc)

    def test_child_output_reaches_console_and_daily_log_in_a_real_python_process(self) -> None:
        self.write_script(
            "Write-Output 'VERIFIED fixture BIOS/MMC'\n"
            "Write-Output 'UPLOAD_RESULT=completed'\n"
        )
        logs = self.root / "logs"
        probe = (
            "import sys; from pathlib import Path; import package_latest_bios as p; "
            "p.UPLOAD_SCRIPT = Path(sys.argv[1]); "
            "p.configure_logging(Path(sys.argv[2])); "
            "p.upload_archives(Path(sys.argv[3]), Path(sys.argv[4]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe, str(self.script), str(logs), str(self.bios), str(self.mmc)],
            cwd=Path(packager.__file__).resolve().parent,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        daily_log = next(logs.glob("package-bios-*.log")).read_text(encoding="utf-8")
        for output in (result.stderr, daily_log):
            self.assertIn("VERIFIED fixture BIOS/MMC", output)
            self.assertIn(f"BIOS_PATH={self.bios}", output)


if __name__ == "__main__":
    unittest.main()
