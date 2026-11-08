from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import seven_zip_support  # noqa: F401  (adds the repository root to sys.path)

from bios_mmc import cli
from bios_mmc import artifactory, fsutil
from bios_mmc.artifactory import BiosBuild
from bios_mmc.upload import RemoteArchive

REPOSITORY = cli.REPOSITORY_DIR


def index_page(*names: str) -> str:
    return "\n".join(f'<a href="{name}">{name}</a>' for name in names)


class BuildSelectionTests(unittest.TestCase):
    def test_selects_highest_numeric_revision_not_last_link_or_lexical_max(self) -> None:
        html = index_page(
            "OAKSTRM.0.RPB.0040.D.100/", "OAKSTRM.0.RPB.0040.D.79/",
            "OAKSTRM.0.RPB.0040.D.99/",
        )
        self.assertEqual(artifactory.parse_latest_build(html), BiosBuild("0040", "100"))

    def test_new_release_takes_precedence_over_old_release_revision(self) -> None:
        html = index_page("OAKSTRM.0.RPB.0039.D.999/", "OAKSTRM.0.RPB.0040.D.01/")
        build = artifactory.parse_latest_build(html)
        self.assertEqual((build.bios_id, build.package_name), ("0040D01", "0040.D01.7z"))

    def test_ignores_non_build_directories_files_and_other_projects(self) -> None:
        html = index_page(
            "../", "mmc_fw.0.408.0/", "deadbeef/", "OAKSTRM.0.RPB.9999.D.99",
            "OAKSTRM.0.RPB.9999.D.99-extra/", "OTHER.0.RPB.9999.D.99/",
            "OAKSTRM.0.RPB.0040.D.77/",
        )
        self.assertEqual(artifactory.parse_latest_build(html).bios_id, "0040D77")

    def test_handles_absolute_encoded_links_and_html_attribute_case(self) -> None:
        html = (
            "<A HREF='https://example.test/main/"
            "OAKSTRM%2E0%2ERPB%2E0040%2ED%2E79/?list=1'>build</A>"
        )
        self.assertEqual(artifactory.parse_latest_build(html).bios_id, "0040D79")

    def test_login_or_empty_page_fails_instead_of_using_an_old_build(self) -> None:
        with self.assertRaisesRegex(ValueError, "No OAKSTRM"):
            artifactory.parse_latest_build("<html>Please sign in</html>")

    @mock.patch.object(artifactory, "urlopen")
    def test_fetch_decodes_listing_and_uses_a_timeout(self, urlopen: mock.Mock) -> None:
        response = urlopen.return_value.__enter__.return_value
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = index_page("OAKSTRM.0.RPB.0040.D.79/").encode()
        html = artifactory.HttpClient().get_text("https://example.test/main/")
        self.assertEqual(artifactory.parse_latest_build(html).bios_id, "0040D79")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)
        request = urlopen.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))


class SevenZipDiscoveryTests(unittest.TestCase):
    def test_explicit_path_must_exist(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "7-Zip executable not found"):
            fsutil.find_seven_zip(Path("missing-7z-binary"))

    def test_path_lookup_prefers_7zz_then_7z_then_7za(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tool = Path(temporary) / "7z"
            tool.write_bytes(b"")
            with mock.patch.object(
                fsutil.shutil, "which",
                side_effect=lambda name: str(tool) if name == "7z" else None,
            ):
                self.assertEqual(fsutil.find_seven_zip(), tool.resolve())

    def test_missing_tool_explains_how_to_install(self) -> None:
        with mock.patch.object(fsutil.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {
                    "ProgramFiles": "Z:\\none", "ProgramW6432": "Z:\\none",
                    "ProgramFiles(x86)": "Z:\\none",
                }):
            with self.assertRaisesRegex(FileNotFoundError, "p7zip-full"):
                fsutil.find_seven_zip()


class OutputDirectoryTests(unittest.TestCase):
    def test_refuses_root_home_ancestors_and_protected_directories(self) -> None:
        home = Path.home().resolve()
        for directory in (Path(home.anchor), home, REPOSITORY):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(ValueError, "protected"):
                    fsutil.ensure_output_dir(directory, (REPOSITORY,))

    def test_accepts_nested_new_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "new dir.with dots"
            self.assertEqual(fsutil.ensure_output_dir(target), target.resolve())

    def test_refuses_file_and_linked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "file"
            file.write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "not a directory"):
                fsutil.ensure_output_dir(file)
            link = Path(temporary) / "link"
            try:
                link.symlink_to(Path(temporary), target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is not permitted here.")
            with self.assertRaisesRegex(ValueError, "linked"):
                fsutil.ensure_output_dir(link)


class CliFixture(unittest.TestCase):
    """Shared mocks for CLI orchestration tests (defines no tests itself)."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cli-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "out"
        self.common = ["--output-dir", str(self.output), "--log-dir", str(self.root / "logs")]
        patches = {
            "configure_logging": mock.patch.object(cli, "configure_logging"),
            "find_seven_zip": mock.patch.object(
                cli, "find_seven_zip", return_value=Path("7z")
            ),
            "load_mapping": mock.patch.object(cli, "load_mapping", return_value=["c"]),
            "package_bios": mock.patch.object(
                cli, "package_bios", return_value=self.output / "0040.D79.7z"
            ),
            "package_mmc_fw": mock.patch.object(
                cli, "package_mmc_fw", return_value=self.output / "MMCFW_0.1022.0.7z"
            ),
            "upload_archives": mock.patch.object(
                cli, "upload_archives", return_value="completed"
            ),
            "read_mmc_version": mock.patch.object(
                cli, "read_mmc_version", return_value="0.1022.0"
            ),
            "connect_target": mock.patch.object(cli, "connect_target"),
            "get_text": mock.patch.object(
                artifactory.HttpClient, "get_text",
                return_value=index_page(
                    "OAKSTRM.0.RPB.0040.D.79/", "OAKSTRM.0.RPB.0040.D.78/"
                ),
            ),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.remote = self.mocks["connect_target"].return_value
        self.published: dict[str, object] = {}
        self.remote.stat.side_effect = lambda kind, name: self.published.get(name)
        self.remote.obtain.side_effect = lambda remote, destination: destination
        environment = mock.patch.dict(os.environ, {"IFWI_GITHUB_TOKEN": "gh-token"})
        environment.start()
        self.addCleanup(environment.stop)


class OrchestrationTests(CliFixture):
    def test_default_run_packages_newest_bios_then_mmc_without_upload(self) -> None:
        self.assertEqual(cli.main(self.common), 0)
        bios_call = self.mocks["package_bios"].call_args
        self.assertEqual(bios_call.args[0], BiosBuild("0040", "79"))
        self.assertEqual(
            bios_call.kwargs["build_url"],
            cli.DEFAULT_BIOS_BASE_URL + "main/OAKSTRM.0.RPB.0040.D.79/",
        )
        self.assertEqual(bios_call.kwargs["retries"], cli.DEFAULT_DOWNLOAD_RETRY)
        mapping_call = self.mocks["load_mapping"].call_args.kwargs
        self.assertEqual(mapping_call["token"], "gh-token")
        self.assertIn("ref=release%2Fap.post-silicon", mapping_call["url"])
        self.assertEqual(
            self.mocks["package_mmc_fw"].call_args.args[0], self.output / "0040.D79.7z"
        )
        self.mocks["upload_archives"].assert_not_called()

    def test_single_artifactory_token_is_used_for_downloads(self) -> None:
        for value, expected in (("AKCp8token", "AKCp8token"), ("", None)):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"IFWI_ARTIFACTORY_TOKEN": value}):
                    self.assertEqual(cli.main(self.common), 0)
                http = self.mocks["package_bios"].call_args.kwargs["http"]
                self.assertEqual(http.token, expected)

    def test_malformed_artifactory_token_fails_before_any_request(self) -> None:
        with mock.patch.dict(os.environ, {"IFWI_ARTIFACTORY_TOKEN": "Bearer x"}):
            with self.assertLogs("bios_packager", level="ERROR"):
                self.assertEqual(cli.main(self.common), 1)
        self.mocks["get_text"].assert_not_called()
        self.mocks["package_bios"].assert_not_called()

    def test_bios_and_mmc_branches_build_urls_under_base(self) -> None:
        self.assertEqual(cli.main([
            *self.common, "--bios-branch", "oks_milestone_rel",
            "--mmc-branch", "zcode_dmr_ap1_beta_rel",
            "--mmc-base-url", "https://mirror.test/ZCodeFW",
        ]), 0)
        self.assertEqual(
            self.mocks["get_text"].call_args.args[0],
            cli.DEFAULT_BIOS_BASE_URL + "oks_milestone_rel/",
        )
        self.assertEqual(
            self.mocks["package_bios"].call_args.kwargs["build_url"],
            cli.DEFAULT_BIOS_BASE_URL + "oks_milestone_rel/OAKSTRM.0.RPB.0040.D.79/",
        )
        self.assertEqual(
            self.mocks["package_mmc_fw"].call_args.kwargs["branch_url"],
            "https://mirror.test/ZCodeFW/zcode_dmr_ap1_beta_rel/",
        )

    def test_default_mmc_branch(self) -> None:
        self.assertEqual(cli.main(self.common), 0)
        self.assertEqual(
            self.mocks["package_mmc_fw"].call_args.kwargs["branch_url"],
            cli.DEFAULT_MMC_BASE_URL + "zcode_dmr_ap_dev/",
        )

    def test_invalid_branch_fails_before_packaging(self) -> None:
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(cli.main([*self.common, "--bios-branch", "../x"]), 1)
        self.assertIn("--bios-branch", "\n".join(logged.output))
        self.mocks["package_bios"].assert_not_called()

    def test_upload_destination_options_reach_every_upload_path(self) -> None:
        destination = [
            "--upload-url", "https://up.test/artifactory/REPO",
            "--upload-bios-path", "B/ios", "--upload-mmc-path", "M/mc",
        ]
        bios = self.root / "0040.D82.7z"
        mmc = self.root / "MMCFW_0.1022.0.7z"
        bios.write_bytes(b"b")
        mmc.write_bytes(b"m")
        for arguments in (
            [*self.common, "--upload"],
            ["mmc", *self.common, "--bios-archive", str(bios), "--upload"],
            ["upload", "--bios-archive", str(bios), "--mmc-archive", str(mmc),
             "--log-dir", str(self.root / "logs")],
        ):
            with self.subTest(arguments=arguments[:1]):
                self.mocks["upload_archives"].reset_mock()
                self.assertEqual(cli.main([*arguments, *destination]), 0)
                target = self.mocks["upload_archives"].call_args.kwargs["target"]
                self.assertEqual(
                    (target.root, target.repository, target.bios_folder, target.mmc_folder),
                    ("https://up.test/artifactory", "REPO", "B/ios", "M/mc"),
                )

    def test_default_upload_destination(self) -> None:
        self.assertEqual(cli.main([*self.common, "--upload"]), 0)
        target = self.mocks["upload_archives"].call_args.kwargs["target"]
        self.assertEqual(target.repository, "DEG-IFWI-LOCAL")
        self.assertEqual(target.bios_folder, "FIV_Portal/IFWI_CI/BIOSID")

    def test_bad_upload_url_is_reported_without_uploading(self) -> None:
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(
                cli.main([*self.common, "--upload", "--upload-url", "https://x/REPO"]), 1
            )
        self.assertIn("--upload-url", "\n".join(logged.output))
        self.mocks["upload_archives"].assert_not_called()

    def test_explicit_bios_ref_and_local_mapping_are_forwarded(self) -> None:
        xlsx = self.root / "map.xlsx"
        self.assertEqual(cli.main([
            *self.common, "--bios", "0040.D.78", "--mapping-ref", "dev",
            "--mapping-xlsx", str(xlsx), "--jobs", "3", "--download-retry", "5",
        ]), 0)
        self.assertEqual(self.mocks["package_bios"].call_args.args[0].revision, "78")
        self.assertEqual(self.mocks["package_bios"].call_args.kwargs["jobs"], 3)
        self.assertEqual(self.mocks["package_bios"].call_args.kwargs["retries"], 5)
        mapping_call = self.mocks["load_mapping"].call_args.kwargs
        self.assertEqual(mapping_call["local_path"], xlsx)
        self.assertIn("ref=dev", mapping_call["url"])

    def test_upload_flag_uploads_exact_pair_after_packaging(self) -> None:
        self.assertEqual(cli.main([*self.common, "--upload"]), 0)
        self.assertEqual(
            self.mocks["upload_archives"].call_args.args,
            (self.output / "0040.D79.7z", self.output / "MMCFW_0.1022.0.7z"),
        )

    def test_dry_run_stops_after_bios_plan_and_never_uploads(self) -> None:
        self.assertEqual(cli.main([*self.common, "--dry-run", "--upload"]), 0)
        self.assertTrue(self.mocks["package_bios"].call_args.kwargs["dry_run"])
        self.mocks["package_mmc_fw"].assert_not_called()
        self.mocks["upload_archives"].assert_not_called()

    def test_bios_subcommand_packages_only_bios(self) -> None:
        self.assertEqual(cli.main(["bios", *self.common, "--bios", "0040D78"]), 0)
        self.assertEqual(self.mocks["package_bios"].call_args.args[0].revision, "78")
        self.mocks["package_mmc_fw"].assert_not_called()
        self.mocks["upload_archives"].assert_not_called()

    def test_bios_subcommand_failure_returns_nonzero(self) -> None:
        self.mocks["package_bios"].side_effect = ValueError("BIOS failed")
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(cli.main(["bios", *self.common]), 1)
        self.assertIn("BIOS packaging failed", "\n".join(logged.output))

    def test_mmc_subcommand_skips_mapping_and_bios_packaging(self) -> None:
        existing = self.root / "0040.D70.7z"
        self.assertEqual(
            cli.main(["mmc", *self.common, "--bios-archive", str(existing)]), 0
        )
        self.mocks["load_mapping"].assert_not_called()
        self.mocks["package_bios"].assert_not_called()
        self.assertEqual(self.mocks["package_mmc_fw"].call_args.args[0], existing)
        self.mocks["upload_archives"].assert_not_called()

    def test_mmc_subcommand_can_upload_the_given_bios_with_new_mmc(self) -> None:
        existing = self.root / "0040.D70.7z"
        self.assertEqual(cli.main(
            ["mmc", *self.common, "--bios-archive", str(existing), "--upload"]
        ), 0)
        self.assertEqual(
            self.mocks["upload_archives"].call_args.args,
            (existing, self.output / "MMCFW_0.1022.0.7z"),
        )

    def test_mmc_subcommand_requires_bios_archive(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            cli.main(["mmc", *self.common])
        self.mocks["package_mmc_fw"].assert_not_called()

    def test_options_belong_to_their_subcommand(self) -> None:
        for arguments in (
            ["bios", "--upload"], ["bios", "--bios-archive", "x.7z"],
            ["mmc", "--bios-archive", "x.7z", "--mapping-ref", "dev"],
            ["--bios-archive", "x.7z"],
            ["bios", "--upload-url", "https://x/artifactory/R"],
            ["mmc", "--bios-archive", "x.7z", "--bios-branch", "main"],
            ["bios", "--mmc-branch", "x"],
            ["--artifactory-url", "https://old"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                    cli.main([*arguments, *self.common])

    def test_failures_are_logged_and_return_nonzero(self) -> None:
        for target, message in (
            ("load_mapping", "IFWI_GITHUB_TOKEN is not set"),
            ("package_bios", "BIOS failed"),
            ("package_mmc_fw", "MMC source missing"),
        ):
            with self.subTest(target=target):
                self.mocks[target].side_effect = ValueError(message)
                with self.assertLogs("bios_packager", level="ERROR") as logged:
                    self.assertEqual(cli.main([*self.common, "--upload"]), 1)
                self.assertIn(message, "\n".join(logged.output))
                self.mocks["upload_archives"].assert_not_called()
                self.mocks[target].side_effect = None

    def test_upload_failure_is_distinct_from_packaging_failure(self) -> None:
        self.mocks["upload_archives"].side_effect = RuntimeError("verification failed")
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(cli.main([*self.common, "--upload"]), 1)
        log = "\n".join(logged.output)
        self.assertIn("verified local BIOS/MMC archives are retained", log)
        self.assertNotIn("BIOS/MMC packaging failed", log)

    def test_invalid_counts_are_rejected(self) -> None:
        with self.assertLogs("bios_packager", level="ERROR"):
            self.assertEqual(cli.main([*self.common, "--jobs", "0"]), 1)
        self.mocks["package_bios"].assert_not_called()

    def test_upload_subcommand_forwards_explicit_pair(self) -> None:
        bios = self.root / "0040.D82.7z"
        mmc = self.root / "MMCFW_0.1022.0.7z"
        bios.write_bytes(b"b")
        mmc.write_bytes(b"m")
        self.assertEqual(cli.main([
            "upload", "--bios-archive", str(bios), "--mmc-archive", str(mmc),
            "--dry-run", "--log-dir", str(self.root / "logs"),
        ]), 0)
        call = self.mocks["upload_archives"].call_args
        self.assertEqual(call.args, (bios.resolve(), mmc.resolve()))
        self.assertTrue(call.kwargs["dry_run"])
        self.mocks["package_bios"].assert_not_called()


class TargetPreCheckTests(CliFixture):
    """Pre-check of the upload destination before packaging (--upload only)."""

    BIOS = "0040.D79.7z"
    MMC = "MMCFW_0.1022.0.7z"
    BIOS_META = RemoteArchive("BIOS", BIOS, "https://up.test/BIOSID/" + BIOS, 10, "a" * 64)
    MMC_META = RemoteArchive("MMC", MMC, "https://up.test/MMC/" + MMC, 5, "b" * 64)

    def test_without_upload_the_target_is_never_contacted(self) -> None:
        self.assertEqual(cli.main(self.common), 0)
        self.mocks["connect_target"].assert_not_called()
        self.mocks["package_bios"].assert_called_once()

    def test_nothing_published_packages_and_uploads_both(self) -> None:
        self.assertEqual(cli.main([*self.common, "--upload"]), 0)
        self.assertEqual(
            [c.args for c in self.remote.stat.call_args_list],
            [("BIOS", self.BIOS), ("MMC", self.MMC)],
        )
        self.mocks["package_bios"].assert_called_once()
        self.mocks["package_mmc_fw"].assert_called_once()
        self.assertEqual(
            self.mocks["upload_archives"].call_args.args,
            (self.output / self.BIOS, self.output / self.MMC),
        )

    def test_both_published_skips_packaging_and_upload(self) -> None:
        self.published = {self.BIOS: self.BIOS_META, self.MMC: self.MMC_META}
        with self.assertLogs("bios_packager", level="INFO") as logged:
            self.assertEqual(cli.main([*self.common, "--upload"]), 0)
        self.mocks["load_mapping"].assert_not_called()
        self.mocks["package_bios"].assert_not_called()
        self.mocks["package_mmc_fw"].assert_not_called()
        self.mocks["upload_archives"].assert_not_called()
        self.remote.obtain.assert_called_once_with(self.BIOS_META, self.output / self.BIOS)
        self.mocks["read_mmc_version"].assert_called_once()
        self.assertIn("both already published", "\n".join(logged.output))

    def test_published_bios_missing_mmc_packages_and_uploads_only_mmc(self) -> None:
        self.published = {self.BIOS: self.BIOS_META}
        self.assertEqual(cli.main([*self.common, "--upload"]), 0)
        self.mocks["package_bios"].assert_not_called()
        self.assertEqual(
            self.mocks["package_mmc_fw"].call_args.args[0], self.output / self.BIOS
        )
        self.assertEqual(self.mocks["package_mmc_fw"].call_args.kwargs["version"], "0.1022.0")
        self.assertEqual(
            self.mocks["upload_archives"].call_args.args, (None, self.output / self.MMC)
        )

    def test_new_bios_with_published_mmc_uploads_only_bios(self) -> None:
        self.published = {self.MMC: self.MMC_META}
        self.assertEqual(cli.main([*self.common, "--upload"]), 0)
        self.mocks["package_bios"].assert_called_once()
        self.mocks["package_mmc_fw"].assert_not_called()
        self.assertEqual(
            self.mocks["upload_archives"].call_args.args, (self.output / self.BIOS, None)
        )

    def test_dry_run_with_published_bios_reports_without_downloading(self) -> None:
        self.published = {self.BIOS: self.BIOS_META}
        self.assertEqual(cli.main([*self.common, "--upload", "--dry-run"]), 0)
        self.remote.obtain.assert_not_called()
        self.mocks["package_bios"].assert_not_called()
        self.mocks["upload_archives"].assert_not_called()

    def test_missing_token_fails_before_any_packaging(self) -> None:
        self.mocks["connect_target"].side_effect = ValueError(
            "IFWI_ARTIFACTORY_TOKEN is not set"
        )
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(cli.main([*self.common, "--upload"]), 1)
        self.assertIn("IFWI_ARTIFACTORY_TOKEN is not set", "\n".join(logged.output))
        self.mocks["get_text"].assert_not_called()
        self.mocks["package_bios"].assert_not_called()

    def test_target_check_failure_stops_before_packaging(self) -> None:
        self.remote.stat.side_effect = RuntimeError("Cannot check BIOS (HTTP 403)")
        with self.assertLogs("bios_packager", level="ERROR"):
            self.assertEqual(cli.main([*self.common, "--upload"]), 1)
        self.mocks["package_bios"].assert_not_called()
        self.mocks["upload_archives"].assert_not_called()

    def test_mmc_subcommand_skips_published_mmc_and_uploads_only_bios(self) -> None:
        existing = self.root / "0040.D70.7z"
        self.published = {self.MMC: self.MMC_META}
        self.assertEqual(cli.main(
            ["mmc", *self.common, "--bios-archive", str(existing), "--upload"]
        ), 0)
        self.mocks["package_mmc_fw"].assert_not_called()
        self.assertEqual(self.mocks["upload_archives"].call_args.args, (existing, None))

    def test_mmc_subcommand_without_upload_never_contacts_target(self) -> None:
        existing = self.root / "0040.D70.7z"
        self.assertEqual(cli.main(["mmc", *self.common, "--bios-archive", str(existing)]), 0)
        self.mocks["connect_target"].assert_not_called()
        self.mocks["package_mmc_fw"].assert_called_once()


class ProcessTests(unittest.TestCase):
    def test_help_runs_from_another_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entries = (
                [sys.executable, str(REPOSITORY / "main.py")],
                [sys.executable, "-m", "bios_mmc"],
            )
            environment = {**os.environ, "PYTHONPATH": str(REPOSITORY)}
            for entry in entries:
                for arguments in ([], ["bios"], ["mmc"], ["upload"]):
                    with self.subTest(entry=entry[1:], arguments=arguments):
                        result = subprocess.run(
                            [*entry, *arguments, "--help"], cwd=temporary,
                            env=environment, capture_output=True, text=True, timeout=60,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("--log-dir", result.stdout)


if __name__ == "__main__":
    unittest.main()
