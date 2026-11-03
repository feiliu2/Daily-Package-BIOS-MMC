from __future__ import annotations

import configparser
import json
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import package_latest_bios as packager


def index_page(*names: str) -> str:
    return "\n".join(f'<a href="{name}">{name}</a>' for name in names)


class BuildSelectionTests(unittest.TestCase):
    def test_selects_highest_numeric_revision_not_last_link_or_lexical_max(self) -> None:
        html = index_page(
            "OAKSTRM.0.RPB.0040.D.100/",
            "OAKSTRM.0.RPB.0040.D.79/",
            "OAKSTRM.0.RPB.0040.D.99/",
        )
        self.assertEqual(
            packager.parse_latest_build(html), packager.BiosBuild("0040", "100")
        )

    def test_new_release_takes_precedence_over_old_release_revision(self) -> None:
        html = index_page(
            "OAKSTRM.0.RPB.0039.D.999/", "OAKSTRM.0.RPB.0040.D.01/"
        )
        build = packager.parse_latest_build(html)
        self.assertEqual(build.bios_id, "0040D01")
        self.assertEqual(build.package_name, "0040.D01.7z")

    def test_ignores_non_build_directories_files_and_other_projects(self) -> None:
        html = index_page(
            "../", "mmc_fw.0.408.0/", "deadbeef/",
            "OAKSTRM.0.RPB.9999.D.99",
            "OAKSTRM.0.RPB.9999.D.99-extra/",
            "OTHER.0.RPB.9999.D.99/", "OAKSTRM.0.RPB.0040.D.77/",
        )
        self.assertEqual(packager.parse_latest_build(html).bios_id, "0040D77")

    def test_handles_absolute_encoded_links_and_html_attribute_case(self) -> None:
        html = (
            "<A HREF='https://example.test/main/"
            "OAKSTRM%2E0%2ERPB%2E0040%2ED%2E79/?list=1'>build</A>"
        )
        self.assertEqual(packager.parse_latest_build(html).bios_id, "0040D79")

    def test_login_or_empty_page_fails_instead_of_using_an_old_build(self) -> None:
        with self.assertRaisesRegex(ValueError, "No OAKSTRM"):
            packager.parse_latest_build("<html>Please sign in</html>")

    @mock.patch.object(packager, "urlopen")
    def test_fetch_decodes_listing_and_uses_a_timeout(self, urlopen: mock.Mock) -> None:
        response = urlopen.return_value.__enter__.return_value
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = index_page(
            "OAKSTRM.0.RPB.0040.D.79/"
        ).encode("utf-8")
        build = packager.fetch_latest_build("https://example.test/main/")
        self.assertEqual(build.bios_id, "0040D79")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="bios-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source with spaces"
        (self.source / "Config").mkdir(parents=True)
        (self.source / "7-Zip").mkdir()
        (self.source / "restructure_new.py").touch()
        (self.source / "7-Zip" / "7z.exe").touch()
        self.original_workspace = self.root / "manual-work"
        self.original_workspace.mkdir()
        self.marker = self.original_workspace / "keep.txt"
        self.marker.write_text("manual build", encoding="utf-8")
        self.config_file = self.source / "Config" / "config_oak_ap.ini"
        self.original_config = (
            f"[work_space]\nwork_space = {self.original_workspace}\n"
            "excel_file = inputs.xlsx\ndownload_retry = 3\n"
            "[Options]\nPreserveCase = original\n"
        )
        self.config_file.write_text(self.original_config, encoding="utf-8")
        self.output = self.root / "output"
        self.logs = self.root / "logs"
        self.legacy_cache = self.root / "ATF"
        self.cached_build = self.legacy_cache / "0040D77"
        self.cached_build.mkdir(parents=True)
        self.cached_file = self.cached_build / "download.7z"
        self.cached_file.write_bytes(b"old download")
        cache_patch = mock.patch.object(
            packager, "LEGACY_CACHE_DIR", self.legacy_cache
        )
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.build = packager.BiosBuild("0040", "79")
        fetch_patch = mock.patch.object(
            packager, "fetch_latest_build", return_value=self.build
        )
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    def package(self, *, dry_run: bool = False) -> Path:
        return packager.package_latest(
            artifactory_url="https://example.test/main/",
            repackaging_dir=self.source,
            output_dir=self.output,
            config_ini="config_oak_ap.ini",
            log_dir=self.logs,
            dry_run=dry_run,
        )

    def fake_packager(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.assertEqual(kwargs["cwd"], self.source)
        self.assertEqual(command[1:3], ["-u", str(self.source / "restructure_new.py")])
        options = dict(argument[2:].split("=", 1) for argument in command[3:])
        self.assertEqual(options["bios"], "0040D79")
        self.assertEqual(options["package_fdr_file"], "0040.D79.7z")
        config_path = Path(options["config_ini"])
        self.assertTrue(config_path.is_absolute())
        config = configparser.ConfigParser()
        config.read(config_path, encoding="utf-8")
        workspace = Path(config["work_space"]["work_space"])
        staging = Path(options["file_path"])
        downloaded_archive = workspace / self.build.bios_id / "download.7z"
        legacy_extraction_path = Path(str(downloaded_archive).split(".")[0])
        self.assertEqual(
            legacy_extraction_path, downloaded_archive.with_suffix("")
        )
        self.assertNotEqual(workspace, self.original_workspace)
        self.assertNotEqual(staging, self.output)
        self.assertEqual(workspace.parent, staging.parent)
        self.assertEqual(config["Options"]["PreserveCase"], "original")
        self.assertTrue(staging.is_dir())
        (staging / self.build.package_name).write_bytes(b"packaged BIOS")
        return subprocess.CompletedProcess(command, 0)

    @mock.patch.object(packager.subprocess, "run")
    def test_dry_run_does_not_run_child_or_create_output(self, run: mock.Mock) -> None:
        destination = self.package(dry_run=True)
        self.assertEqual(destination, self.output / "0040.D79.7z")
        self.assertFalse(self.output.exists())
        self.assertEqual(self.cached_file.read_bytes(), b"old download")
        run.assert_not_called()

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_success_isolates_destructive_paths_and_preserves_old_data(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output.mkdir()
        old_archive = self.output / "0040.D77.7z"
        old_archive.write_bytes(b"previous BIOS")
        run.side_effect = self.fake_packager
        destination = self.package()
        self.assertEqual(destination.read_bytes(), b"packaged BIOS")
        self.assertEqual(old_archive.read_bytes(), b"previous BIOS")
        self.assertEqual(self.marker.read_text(), "manual build")
        self.assertEqual(self.config_file.read_text(), self.original_config)
        validate.assert_called_once()
        self.assertFalse(list(self.output.glob("packaging-*")))
        self.assertFalse(self.legacy_cache.exists())

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_valid_existing_archive_is_verified_and_skipped(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output.mkdir()
        destination = self.output / self.build.package_name
        destination.write_bytes(b"existing BIOS")
        self.assertEqual(self.package(), destination)
        validate.assert_called_once_with(
            destination, self.source / "7-Zip" / "7z.exe"
        )
        run.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"existing BIOS")
        self.assertFalse(self.legacy_cache.exists())

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_invalid_existing_archive_is_not_overwritten(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output.mkdir()
        destination = self.output / self.build.package_name
        destination.write_bytes(b"corrupt")
        validate.side_effect = RuntimeError("CRC failed")
        with self.assertRaisesRegex(RuntimeError, "CRC failed"):
            self.package()
        run.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"corrupt")
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager.subprocess, "run")
    def test_child_failure_publishes_nothing_and_cleans_only_its_job(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = subprocess.CalledProcessError(2, ["fake packager"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.marker.read_text(), "manual build")
        self.assertEqual(self.config_file.read_text(), self.original_config)
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_new_invalid_archive_is_not_published(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        run.side_effect = self.fake_packager
        validate.side_effect = RuntimeError("CRC failed")
        with self.assertRaisesRegex(RuntimeError, "CRC failed"):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager.subprocess, "run")
    def test_success_exit_without_an_archive_is_a_failure(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0)
        with self.assertRaisesRegex(ValueError, "missing or empty"):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager.subprocess, "run")
    def test_missing_tool_fails_before_network_or_packaging(self, run: mock.Mock) -> None:
        (self.source / "7-Zip" / "7z.exe").unlink()
        with self.assertRaises(FileNotFoundError):
            self.package()
        self.fetch.assert_not_called()
        run.assert_not_called()

    @mock.patch.object(packager.subprocess, "run")
    def test_workspace_with_spaces_is_rejected_before_child(self, run: mock.Mock) -> None:
        self.output = self.root / "output with spaces"
        with self.assertRaisesRegex(ValueError, "without whitespace"):
            self.package()
        run.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    @mock.patch.object(packager.subprocess, "run")
    def test_workspace_with_dots_is_rejected_before_child(self, run: mock.Mock) -> None:
        self.output = self.root / "output.with.dots"
        self.output.mkdir()
        old_archive = self.output / "0040.D77.7z"
        old_archive.write_bytes(b"previous BIOS")
        with self.assertRaisesRegex(ValueError, "without whitespace or periods"):
            self.package()
        run.assert_not_called()
        self.assertEqual(old_archive.read_bytes(), b"previous BIOS")
        self.assertFalse(list(self.output.glob("packaging-*")))

    def test_cleanup_removes_loose_files_and_build_cache_but_not_archives(self) -> None:
        (self.legacy_cache / "old.log").write_text("old log", encoding="utf-8")
        self.output.mkdir()
        archive = self.output / self.build.package_name
        archive.write_bytes(b"verified archive")
        packager.cleanup_legacy_cache(archive, self.source, self.logs)
        self.assertFalse(self.legacy_cache.exists())
        self.assertEqual(archive.read_bytes(), b"verified archive")
        self.assertEqual(self.marker.read_text(), "manual build")

    def test_cleanup_missing_cache_is_idempotent(self) -> None:
        self.cached_file.unlink()
        self.cached_build.rmdir()
        self.legacy_cache.rmdir()
        packager.cleanup_legacy_cache(
            self.output / self.build.package_name, self.source, self.logs
        )
        self.assertFalse(self.legacy_cache.exists())

    def test_cleanup_refuses_cache_containing_protected_paths(self) -> None:
        for protected in ("archive", "repackager", "logs"):
            with self.subTest(protected=protected):
                archive = self.output / self.build.package_name
                source = self.source
                logs = self.logs
                if protected == "archive":
                    archive = self.legacy_cache / self.build.package_name
                elif protected == "repackager":
                    source = self.legacy_cache / "source"
                else:
                    logs = self.legacy_cache / "logs"
                with self.assertRaisesRegex(ValueError, "protected BIOS cache"):
                    packager.cleanup_legacy_cache(archive, source, logs)
                self.assertEqual(self.cached_file.read_bytes(), b"old download")

    def test_cleanup_refuses_linked_cache(self) -> None:
        metadata = mock.Mock(
            st_mode=stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )
        with mock.patch.object(Path, "lstat", return_value=metadata):
            with self.assertRaisesRegex(ValueError, "linked BIOS cache"):
                packager.cleanup_legacy_cache(
                    self.output / self.build.package_name, self.source, self.logs
                )
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    def test_cleanup_reports_permission_errors(self) -> None:
        with mock.patch.object(
            packager.shutil, "rmtree", side_effect=PermissionError("cache in use")
        ):
            with self.assertRaisesRegex(PermissionError, "cache in use"):
                packager.cleanup_legacy_cache(
                    self.output / self.build.package_name, self.source, self.logs
                )
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager, "cleanup_legacy_cache")
    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_cleanup_failure_keeps_published_archive_for_retry(
        self, run: mock.Mock, validate: mock.Mock, cleanup: mock.Mock
    ) -> None:
        run.side_effect = self.fake_packager
        cleanup.side_effect = PermissionError("cache in use")
        with self.assertRaisesRegex(PermissionError, "cache in use"):
            self.package()
        self.assertEqual(
            (self.output / self.build.package_name).read_bytes(), b"packaged BIOS"
        )
        self.assertEqual(self.cached_file.read_bytes(), b"old download")
        self.assertFalse(list(self.output.glob("packaging-*")))
        cleanup.side_effect = None
        run.reset_mock()
        self.package()
        run.assert_not_called()
        self.assertEqual(cleanup.call_count, 2)

    @unittest.skipUnless(
        all(
            (packager.DEFAULT_REPACKAGING_DIR / "7-Zip" / name).is_file()
            for name in ("7z.exe", "7z.dll")
        ),
        "Integration test requires the existing repackager's bundled 7-Zip.",
    )
    def test_real_python_child_and_7zip_publish_a_verified_archive(self) -> None:
        for name in ("7z.exe", "7z.dll"):
            shutil.copyfile(
                packager.DEFAULT_REPACKAGING_DIR / "7-Zip" / name,
                self.source / "7-Zip" / name,
            )
        fake_legacy_script = textwrap.dedent(
            """\
            import argparse
            import configparser
            import json
            import os
            import shutil
            import subprocess
            from pathlib import Path

            parser = argparse.ArgumentParser()
            for name in ("bios", "package_fdr_file", "file_path", "config_ini"):
                parser.add_argument("--" + name, required=True)
            args = parser.parse_args()
            config_path = os.path.join(
                Path(__file__).parent, "Config", args.config_ini
            )
            config = configparser.ConfigParser()
            config.read(config_path, encoding="utf-8")
            workspace = Path(config["work_space"]["work_space"])
            output = Path(args.file_path)
            for folder in (workspace, output):
                if folder.exists():
                    shutil.rmtree(folder)
                folder.mkdir(parents=True)
            payload = workspace / "build.json"
            payload.write_text(
                json.dumps({
                    "bios": args.bios,
                    "archive": args.package_fdr_file,
                    "cwd": os.getcwd(),
                    "workspace": str(workspace),
                    "config": config_path,
                }),
                encoding="utf-8",
            )
            tool = Path(__file__).parent / "7-Zip" / "7z.exe"
            download = workspace / "download.7z"
            subprocess.run(
                [str(tool), "a", "-t7z", str(download), str(payload)],
                check=True,
                stdin=subprocess.DEVNULL,
            )
            # Match the existing unpacker's whole-path first-dot truncation.
            unpacked = Path(str(download).split(".")[0])
            if unpacked.is_dir():
                shutil.rmtree(unpacked)
            unpacked.mkdir()
            subprocess.run(
                [str(tool), "x", str(download), "-o" + str(unpacked)],
                check=True,
                stdin=subprocess.DEVNULL,
            )
            subprocess.run(
                [str(tool), "a", "-t7z",
                 str(output / args.package_fdr_file),
                 str(unpacked / "build.json")],
                check=True,
                stdin=subprocess.DEVNULL,
            )
            """
        )
        (self.source / "restructure_new.py").write_text(
            fake_legacy_script, encoding="utf-8"
        )
        self.output.mkdir()
        old_archive = self.output / "0040.D77.7z"
        old_archive.write_bytes(b"previous BIOS")
        destination = self.package()
        extracted = subprocess.run(
            [
                str(self.source / "7-Zip" / "7z.exe"),
                "x", "-so", "-bso0", "-bsp0", "-bse2",
                str(destination), "build.json",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        payload = json.loads(extracted.stdout)
        self.assertEqual(payload["bios"], "0040D79")
        self.assertEqual(payload["archive"], "0040.D79.7z")
        self.assertEqual(Path(payload["cwd"]), self.source)
        self.assertNotEqual(Path(payload["workspace"]), self.original_workspace)
        self.assertFalse(Path(payload["config"]).exists())
        self.assertEqual(old_archive.read_bytes(), b"previous BIOS")
        self.assertEqual(self.marker.read_text(), "manual build")
        self.assertEqual(self.config_file.read_text(), self.original_config)
        self.assertFalse(list(self.output.glob("packaging-*")))
        self.assertFalse(self.legacy_cache.exists())


class TemporaryCleanupTests(unittest.TestCase):
    @mock.patch.object(packager.time, "sleep")
    def test_transient_windows_sharing_violation_is_retried(
        self, sleep: mock.Mock
    ) -> None:
        temporary = mock.Mock(spec=tempfile.TemporaryDirectory)
        temporary.name = "temporary workspace"
        error = PermissionError("file is in use")
        error.winerror = 32
        temporary.cleanup.side_effect = [error, None]
        with self.assertLogs("bios_packager", level="WARNING") as logged:
            packager.cleanup_temporary_workspace(temporary)
        self.assertEqual(temporary.cleanup.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertIn("retrying cleanup (1/5)", "\n".join(logged.output))

    @mock.patch.object(packager.time, "sleep")
    def test_persistent_windows_lock_is_reported_after_bounded_retries(
        self, sleep: mock.Mock
    ) -> None:
        temporary = mock.Mock(spec=tempfile.TemporaryDirectory)
        temporary.name = "temporary workspace"
        error = PermissionError("file remains in use")
        error.winerror = 33
        temporary.cleanup.side_effect = error
        with self.assertLogs("bios_packager", level="WARNING"):
            with self.assertRaisesRegex(PermissionError, "remains in use"):
                packager.cleanup_temporary_workspace(temporary)
        self.assertEqual(temporary.cleanup.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    @mock.patch.object(packager.time, "sleep")
    def test_other_permission_errors_are_not_retried_or_hidden(
        self, sleep: mock.Mock
    ) -> None:
        temporary = mock.Mock(spec=tempfile.TemporaryDirectory)
        error = PermissionError("access denied")
        error.winerror = 5
        temporary.cleanup.side_effect = error
        with self.assertRaisesRegex(PermissionError, "access denied"):
            packager.cleanup_temporary_workspace(temporary)
        temporary.cleanup.assert_called_once()
        sleep.assert_not_called()


class ValidationAndExitTests(unittest.TestCase):
    @mock.patch.object(packager.subprocess, "run")
    def test_archive_test_checks_all_data_and_reports_crc_errors(
        self, run: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "BIOS.7z"
            archive.write_bytes(b"invalid archive")
            run.return_value = subprocess.CompletedProcess([], 2, "CRC failed")
            with self.assertRaisesRegex(RuntimeError, "CRC failed"):
                packager.validate_archive(archive, Path("7z.exe"))
            self.assertEqual(
                run.call_args.args[0],
                ["7z.exe", "t", "-bso0", "-bsp0", "-y", "--", str(archive)],
            )
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    @mock.patch.object(packager, "configure_logging")
    @mock.patch.object(packager, "package_latest")
    def test_network_failure_is_logged_and_returns_nonzero(
        self, package: mock.Mock, configure_logging: mock.Mock
    ) -> None:
        package.side_effect = URLError("Intel network unavailable")
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(packager.main(["--dry-run"]), 1)
        self.assertIn("Intel network unavailable", "\n".join(logged.output))

    @mock.patch.object(packager, "configure_logging")
    @mock.patch.object(packager, "package_latest")
    def test_child_failure_is_logged_and_returns_nonzero(
        self, package: mock.Mock, configure_logging: mock.Mock
    ) -> None:
        package.side_effect = subprocess.CalledProcessError(2, ["packager"])
        with self.assertLogs("bios_packager", level="ERROR"):
            self.assertEqual(packager.main([]), 1)


if __name__ == "__main__":
    unittest.main()
