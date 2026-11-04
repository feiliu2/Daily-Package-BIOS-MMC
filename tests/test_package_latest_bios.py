from __future__ import annotations

import configparser
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from io import TextIOBase
from pathlib import Path
from unittest import mock
from urllib.error import URLError

import package_latest_bios as packager
from seven_zip_support import SEVEN_ZIP_DIRECTORY, copy_seven_zip


def index_page(*names: str) -> str:
    return "\n".join(f'<a href="{name}">{name}</a>' for name in names)


def summary_text(total: int = 3, missing: tuple[str, ...] = ()) -> str:
    messages = [
        "=======Restructure info=======",
        *(f"To be packaged file==> {{'{index}': ['fixture-{index}', ['target']]}}"
          for index in range(total)),
        "=======Restructure Package Done=======",
        "Not Found Files As Blow:",
        *missing,
        f"Total not packaged files:[{len(missing)}]",
        "Spend total time:12.5",
    ]
    return "".join(
        f"[2026-11-04 09:00:00,000] - restructure_new - [line:370] "
        f"- INFO -->> {message}\n" for message in messages
    )


class PackagerSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="bios-summary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log_file = self.root / "restructure.log"

    def test_successful_report_shows_component_counts_and_original_elapsed_time(self) -> None:
        self.log_file.write_text(summary_text(10), encoding="utf-8")
        with self.assertLogs("bios_packager", level="INFO") as logged:
            packager.log_packager_summary(self.log_file)
        text = "\n".join(logged.output)
        self.assertIn("total=10, packaged=10, not packaged=0", text)
        self.assertIn("Total not packaged files:[0]", text)
        self.assertIn("Spend total time:12.5", text)
        self.assertNotIn("To be packaged file==>", text)
        self.assertTrue(all(record.levelname == "INFO" for record in logged.records))

    def test_missing_packages_are_named_and_reported_as_warnings(self) -> None:
        self.log_file.write_text(summary_text(3, ("missing_BuildPkg",)), encoding="utf-8")
        with self.assertLogs("bios_packager", level="WARNING") as logged:
            packager.log_packager_summary(self.log_file)
        text = "\n".join(logged.output)
        self.assertIn("total=3, packaged=2, not packaged=1", text)
        self.assertIn("missing_BuildPkg", text)

    def test_missing_final_report_does_not_invent_successful_counts(self) -> None:
        for content in (
            "", "Traceback: packaging failed\n",
            "=======Restructure info=======\nTo be packaged file==> pending\n",
        ):
            with self.subTest(content=content):
                self.log_file.write_text(content, encoding="utf-8")
                with self.assertLogs("bios_packager", level="WARNING") as logged:
                    packager.log_packager_summary(self.log_file)
                text = "\n".join(logged.output)
                self.assertIn("unavailable or incomplete", text)
                self.assertNotIn("total=", text)

    def test_incomplete_or_inconsistent_counts_preserve_original_report(self) -> None:
        for content in (
            summary_text(1, ("missing-one", "missing-two")),
            summary_text(1).replace("Total not packaged files:[0]", "counter missing"),
            summary_text(1).replace("=======Restructure info=======", "header missing"),
            summary_text(0),
        ):
            with self.subTest(content=content):
                self.log_file.write_text(content, encoding="utf-8")
                with self.assertLogs("bios_packager", level="WARNING") as logged:
                    packager.log_packager_summary(self.log_file)
                text = "\n".join(logged.output)
                self.assertIn("Original BIOS packager final summary", text)
                self.assertIn("counts are unavailable or inconsistent", text)
                self.assertNotIn("total=", text)

    def test_plain_unprefixed_report_is_supported(self) -> None:
        content = "\n".join(
            line.partition(" -->> ")[2] for line in summary_text(2).splitlines()
        )
        self.log_file.write_text(content, encoding="utf-8")
        with self.assertLogs("bios_packager", level="INFO") as logged:
            packager.log_packager_summary(self.log_file)
        self.assertIn("total=2, packaged=2, not packaged=0", "\n".join(logged.output))

    def test_report_reaches_both_console_and_daily_log_in_a_real_process(self) -> None:
        self.log_file.write_text(summary_text(2), encoding="utf-8")
        logs = self.root / "logs"
        probe = (
            "import sys; from pathlib import Path; import package_latest_bios as p; "
            "p.configure_logging(Path(sys.argv[1])); "
            "p.log_packager_summary(Path(sys.argv[2]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe, str(logs), str(self.log_file)],
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
            self.assertIn("total=2, packaged=2, not packaged=0", output)
            self.assertIn("Total not packaged files:[0]", output)


class DeploymentPathTests(unittest.TestCase):
    def test_relocated_script_uses_its_directory_not_the_callers_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bios-deployment-") as temporary:
            root = Path(temporary)
            deployed = root / "deployed tools"
            deployed.mkdir()
            source = Path(packager.__file__).resolve()
            shutil.copyfile(source, deployed / source.name)
            caller = root / "unrelated working directory"
            caller.mkdir()
            probe = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import package_latest_bios; "
                "print(package_latest_bios.DEFAULT_REPACKAGING_DIR); "
                "print(package_latest_bios.UPLOAD_SCRIPT)"
            )
            result = subprocess.run(
                [sys.executable, "-c", probe, str(deployed)],
                cwd=caller,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=True,
            )
            self.assertEqual(result.stdout.splitlines(), [
                str(deployed.resolve()),
                str(deployed.resolve() / "Upload-BiosMmc.ps1"),
            ])


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
        self.workspace = self.root / "ATF"
        self.config_file = self.source / "Config" / "config_oak_ap.ini"
        self.original_config = (
            f"[work_space]\nwork_space = {self.workspace}\n"
            "excel_file = inputs.xlsx\ndownload_retry = 3\n"
            "[Options]\nPreserveCase = original\n"
        )
        self.config_file.write_text(self.original_config, encoding="utf-8")
        self.output = self.root / "output"
        self.logs = self.root / "logs"
        self.cached_build = self.workspace / "0040D77"
        self.cached_build.mkdir(parents=True)
        self.cached_file = self.cached_build / "download.7z"
        self.cached_file.write_bytes(b"old download")
        cache_patch = mock.patch.object(
            packager, "WORKSPACE_DIR", self.workspace
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
        self.assertEqual(options["config_ini"], "config_oak_ap.ini")
        self.assertEqual(Path(options["file_path"]), self.output)
        config = configparser.ConfigParser()
        config.read(self.config_file, encoding="utf-8")
        workspace = Path(config["work_space"]["work_space"])
        downloaded_archive = workspace / self.build.bios_id / "download.7z"
        legacy_extraction_path = Path(str(downloaded_archive).split(".")[0])
        self.assertEqual(
            legacy_extraction_path, downloaded_archive.with_suffix("")
        )
        self.assertEqual(workspace, self.workspace)
        self.assertEqual(config["Options"]["PreserveCase"], "original")
        self.assertEqual(self.config_file.read_text(), self.original_config)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "build.tmp").write_bytes(b"working data")
        if self.output.exists():
            shutil.rmtree(self.output)
        self.output.mkdir()
        (self.output / self.build.package_name).write_bytes(b"packaged BIOS")
        output = kwargs["stdout"]
        assert isinstance(output, TextIOBase)
        output.write(summary_text(1))
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
    def test_success_uses_original_paths_and_keeps_only_latest_archive(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output.mkdir()
        old_archive = self.output / "0040.D77.7z"
        old_archive.write_bytes(b"previous BIOS")
        run.side_effect = self.fake_packager
        destination = self.package()
        self.assertEqual(destination.read_bytes(), b"packaged BIOS")
        self.assertFalse(old_archive.exists())
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(self.config_file.read_text(), self.original_config)
        validate.assert_called_once()
        self.assertEqual(list(self.output.iterdir()), [destination])

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_missing_child_log_is_reported_without_inventing_a_summary(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        run.side_effect = self.fake_packager
        original_is_file = Path.is_file

        def log_unavailable(path: Path) -> bool:
            return False if path.parent == self.logs else original_is_file(path)

        with mock.patch.object(Path, "is_file", log_unavailable):
            with self.assertLogs("bios_packager", level="WARNING") as logged:
                self.package()
        self.assertIn("log is missing; summary unavailable", "\n".join(logged.output))
        validate.assert_called_once()
        self.assertEqual(list(self.workspace.iterdir()), [])

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_existing_archive_still_runs_original_command(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output.mkdir()
        destination = self.output / self.build.package_name
        destination.write_bytes(b"existing BIOS")
        run.side_effect = self.fake_packager
        self.assertEqual(self.package(), destination)
        validate.assert_called_once_with(
            destination, self.source / "7-Zip" / "7z.exe"
        )
        run.assert_called_once()
        self.assertEqual(destination.read_bytes(), b"packaged BIOS")
        self.assertEqual(list(self.workspace.iterdir()), [])

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_invalid_existing_archive_is_replaced_by_original_command(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output.mkdir()
        destination = self.output / self.build.package_name
        destination.write_bytes(b"corrupt")
        run.side_effect = self.fake_packager
        self.assertEqual(self.package(), destination)
        run.assert_called_once()
        validate.assert_called_once()
        self.assertEqual(destination.read_bytes(), b"packaged BIOS")
        self.assertEqual(list(self.workspace.iterdir()), [])

    @mock.patch.object(packager.subprocess, "run")
    def test_child_failure_does_not_clear_workspace(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = subprocess.CalledProcessError(2, ["fake packager"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.config_file.read_text(), self.original_config)
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_child_errors_still_show_summary_without_validating_or_cleaning(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        for error in (
            subprocess.CalledProcessError(2, ["fake packager"]),
            subprocess.TimeoutExpired(["fake packager"], 1),
        ):
            with self.subTest(error=type(error).__name__):
                def fail_after_report(
                    command: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    self.fake_packager(command, **kwargs)
                    raise error

                run.side_effect = fail_after_report
                with self.assertLogs("bios_packager", level="INFO") as logged:
                    with self.assertRaises(type(error)):
                        self.package()
                self.assertIn(
                    "total=1, packaged=1, not packaged=0", "\n".join(logged.output)
                )
                self.assertEqual(self.cached_file.read_bytes(), b"old download")
                validate.assert_not_called()

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_invalid_new_archive_does_not_trigger_workspace_cleanup(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        run.side_effect = self.fake_packager
        validate.side_effect = RuntimeError("CRC failed")
        with self.assertRaisesRegex(RuntimeError, "CRC failed"):
            self.package()
        self.assertTrue((self.output / self.build.package_name).is_file())
        self.assertEqual((self.workspace / "build.tmp").read_bytes(), b"working data")
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

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_output_with_spaces_still_uses_original_workspace(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output = self.root / "output with spaces"
        run.side_effect = self.fake_packager
        self.assertTrue(self.package().is_file())
        run.assert_called_once()
        validate.assert_called_once()
        self.assertEqual(list(self.workspace.iterdir()), [])

    @mock.patch.object(packager, "validate_archive")
    @mock.patch.object(packager.subprocess, "run")
    def test_output_with_dots_still_uses_original_workspace(
        self, run: mock.Mock, validate: mock.Mock
    ) -> None:
        self.output = self.root / "output.with.dots"
        self.output.mkdir()
        old_archive = self.output / "0040.D77.7z"
        old_archive.write_bytes(b"previous BIOS")
        run.side_effect = self.fake_packager
        self.assertTrue(self.package().is_file())
        run.assert_called_once()
        validate.assert_called_once()
        self.assertFalse(old_archive.exists())
        self.assertEqual(list(self.workspace.iterdir()), [])

    @mock.patch.object(packager.subprocess, "run")
    def test_mismatched_original_workspace_is_not_rewritten(
        self, run: mock.Mock
    ) -> None:
        changed_config = self.original_config.replace(
            str(self.workspace), str(self.root / "unrelated")
        )
        self.config_file.write_text(changed_config, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must use work_space"):
            self.package()
        run.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self.config_file.read_text(), changed_config)
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    def test_cleanup_empties_workspace_but_keeps_directory_and_archive(self) -> None:
        (self.workspace / "old.log").write_text("old log", encoding="utf-8")
        self.output.mkdir()
        archive = self.output / self.build.package_name
        archive.write_bytes(b"verified archive")
        packager.clear_workspace(self.workspace)
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(archive.read_bytes(), b"verified archive")
        self.assertEqual(self.config_file.read_text(), self.original_config)

    def test_cleanup_missing_cache_is_idempotent(self) -> None:
        self.cached_file.unlink()
        self.cached_build.rmdir()
        self.workspace.rmdir()
        packager.clear_workspace(self.workspace)
        self.assertFalse(self.workspace.exists())

    def test_preflight_refuses_overlapping_and_protected_paths(self) -> None:
        for protected in ("output", "repackager", "logs"):
            with self.subTest(protected=protected):
                output = self.output
                source = self.source
                logs = self.logs
                if protected == "output":
                    output = self.workspace / "Bios"
                elif protected == "repackager":
                    source = self.workspace / "source"
                else:
                    logs = self.workspace / "logs"
                with self.assertRaisesRegex(ValueError, "protected|overlap"):
                    packager.validate_packaging_paths(
                        output, source, logs, self.config_file
                    )
                self.assertEqual(self.cached_file.read_bytes(), b"old download")

    def test_preflight_refuses_linked_workspace(self) -> None:
        metadata = mock.Mock(
            st_mode=stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )
        with mock.patch.object(Path, "lstat", return_value=metadata):
            with self.assertRaisesRegex(ValueError, "linked packaging directory"):
                packager.validate_packaging_paths(
                    self.output, self.source, self.logs, self.config_file
                )
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    def test_cleanup_reports_permission_errors(self) -> None:
        with mock.patch.object(
            packager.shutil, "rmtree", side_effect=PermissionError("cache in use")
        ):
            with self.assertRaisesRegex(PermissionError, "cache in use"):
                packager.clear_workspace(self.workspace)
        self.assertEqual(self.cached_file.read_bytes(), b"old download")

    @mock.patch.object(packager, "clear_workspace", wraps=packager.clear_workspace)
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
        cleanup.side_effect = None
        run.reset_mock()
        self.package()
        run.assert_called_once()
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(list(self.workspace.iterdir()), [])

    @unittest.skipUnless(
        SEVEN_ZIP_DIRECTORY is not None,
        "Integration test requires a bundled or installed native 7-Zip.",
    )
    def test_real_python_child_and_7zip_publish_a_verified_archive(self) -> None:
        copy_seven_zip(self.source / "7-Zip")
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
        fake_legacy_script += f"\nprint({summary_text(1)!r}, end='')\n"
        (self.source / "restructure_new.py").write_text(
            fake_legacy_script, encoding="utf-8"
        )
        self.output.mkdir()
        old_archive = self.output / "0040.D77.7z"
        old_archive.write_bytes(b"previous BIOS")
        with self.assertLogs("bios_packager", level="INFO") as logged:
            destination = self.package()
        self.assertIn("total=1, packaged=1, not packaged=0", "\n".join(logged.output))
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
        self.assertEqual(Path(payload["workspace"]), self.workspace)
        self.assertEqual(Path(payload["config"]), self.config_file)
        self.assertFalse(old_archive.exists())
        self.assertEqual(self.config_file.read_text(), self.original_config)
        self.assertEqual(list(self.output.iterdir()), [destination])
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(list(self.workspace.iterdir()), [])


class WorkspaceCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="bios-cleanup-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "ATF"
        self.build = self.workspace / "build"
        self.build.mkdir(parents=True)
        self.cached_file = self.build / "download.7z"
        self.cached_file.write_bytes(b"download")

    @mock.patch.object(packager.time, "sleep")
    def test_transient_windows_sharing_violation_is_retried(
        self, sleep: mock.Mock
    ) -> None:
        error = PermissionError("file is in use")
        error.winerror = 32
        original_rmtree = shutil.rmtree
        attempts = 0

        def remove_after_unlock(path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error
            original_rmtree(path)

        with mock.patch.object(
            packager.shutil, "rmtree", side_effect=remove_after_unlock
        ) as remove:
            with self.assertLogs("bios_packager", level="WARNING") as logged:
                packager.clear_workspace(self.workspace)
        self.assertEqual(remove.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertIn("retrying cleanup (1/5)", "\n".join(logged.output))
        self.assertEqual(list(self.workspace.iterdir()), [])

    @mock.patch.object(packager.time, "sleep")
    def test_persistent_windows_lock_is_reported_after_bounded_retries(
        self, sleep: mock.Mock
    ) -> None:
        error = PermissionError("file remains in use")
        error.winerror = 33
        with mock.patch.object(
            packager.shutil, "rmtree", side_effect=error
        ) as remove:
            with self.assertLogs("bios_packager", level="WARNING"):
                with self.assertRaisesRegex(PermissionError, "remains in use"):
                    packager.clear_workspace(self.workspace)
        self.assertEqual(remove.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertTrue(self.cached_file.is_file())

    @mock.patch.object(packager.time, "sleep")
    def test_other_permission_errors_are_not_retried_or_hidden(
        self, sleep: mock.Mock
    ) -> None:
        error = PermissionError("access denied")
        error.winerror = 5
        with mock.patch.object(
            packager.shutil, "rmtree", side_effect=error
        ) as remove:
            with self.assertRaisesRegex(PermissionError, "access denied"):
                packager.clear_workspace(self.workspace)
        remove.assert_called_once()
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
