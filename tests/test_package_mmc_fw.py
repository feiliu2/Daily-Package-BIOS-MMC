from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock
from urllib.error import URLError

import package_latest_bios as packager


VERSION = "0.1022.0"
SOURCE_NAME = "ZCodeFW_test_DMR_ZCODE_DebugSigned_linux_309_BuildPkg.7z"
BASE_URL = "https://example.test/mmc/"
DIRECTORY_URL = BASE_URL + f"MMC_FW.DMR_AP.{VERSION}/DMR_ZCODE_DebugSigned_linux/"
BIN_NAME = f"mmc_pkg_{VERSION}_00_sign-test_unencrypted_dmr-ap.bin"


def index_page(*names: str) -> str:
    return "\n".join(f'<a href="{name}">{name}</a>' for name in names)


class MmcVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mmc-version-tests-")
        self.addCleanup(temporary.cleanup)
        self.bios = Path(temporary.name) / "BIOS.7z"
        self.bios.write_bytes(b"fixture")

    @mock.patch.object(packager, "run_7zip")
    def test_reads_all_definitions_and_ignores_unrelated_versions(
        self, run: mock.Mock
    ) -> None:
        run.return_value = (
            f"  DEFINE MMC1 = path\\{BIN_NAME}\n"
            f"  DEFINE MMC2 = other\\{BIN_NAME}\n"
        ) * 20 + "DEFINE OTHER = mmc_pkg_0.1024.0_other.bin\n"
        self.assertEqual(packager.read_mmc_version(self.bios, Path("7z.exe")), VERSION)
        self.assertEqual(
            run.call_args.args[1][-1], r"Trunk_*\dsc\FvRawDefPath.dsc"
        )

    @mock.patch.object(packager, "run_7zip")
    def test_conflicting_versions_fail_instead_of_selecting_latest(
        self, run: mock.Mock
    ) -> None:
        run.return_value = (
            f"DEFINE MMC1 = {BIN_NAME}\n"
            "DEFINE MMC2 = mmc_pkg_0.1024.0_test.bin\n"
        )
        with self.assertRaisesRegex(ValueError, "Conflicting MMC versions"):
            packager.read_mmc_version(self.bios, Path("7z.exe"))

    @mock.patch.object(packager, "run_7zip")
    def test_missing_and_unversioned_references_fail(self, run: mock.Mock) -> None:
        for content in (
            "", f"# DEFINE MMC1 = {BIN_NAME}",
            f"DEFINE MMC1 = {BIN_NAME}\nDEFINE MMC2 = unknown.bin\n",
        ):
            with self.subTest(content=content):
                run.return_value = content
                with self.assertRaisesRegex(ValueError, "No MMC|no recognizable version"):
                    packager.read_mmc_version(self.bios, Path("7z.exe"))

    @mock.patch.object(packager, "run_7zip")
    def test_missing_archive_fails_without_extraction(self, run: mock.Mock) -> None:
        self.bios.unlink()
        with self.assertRaisesRegex(ValueError, "missing or empty"):
            packager.read_mmc_version(self.bios, Path("7z.exe"))
        run.assert_not_called()


class MmcSourceTests(unittest.TestCase):
    def test_selects_only_direct_build_package_and_deduplicates_links(self) -> None:
        html = index_page(
            "../", SOURCE_NAME, SOURCE_NAME,
            f"mmc_pkg_{VERSION}_00_sign-prod-release.bin",
            f"nested/{SOURCE_NAME}", f"../{SOURCE_NAME}",
            f"https://other.test/{SOURCE_NAME}",
        )
        self.assertEqual(
            packager.select_mmc_source(html, DIRECTORY_URL),
            DIRECTORY_URL + SOURCE_NAME,
        )

    def test_missing_or_ambiguous_source_fails(self) -> None:
        for html in (
            "<html>Please sign in</html>",
            index_page("unrelated.7z"),
            index_page(SOURCE_NAME, "another_BuildPkg.7z"),
        ):
            with self.subTest(html=html):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    packager.select_mmc_source(html, DIRECTORY_URL)

    @mock.patch.object(packager, "urlopen")
    def test_download_checks_length_and_rejects_empty_data(
        self, urlopen: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "BuildPkg.7z"
            response = urlopen.return_value.__enter__.return_value
            for content, length, valid in (
                (b"payload", "7", True), (b"payload", None, True),
                (b"payload", "10", False), (b"", "0", False),
            ):
                with self.subTest(length=length, valid=valid):
                    response.read.side_effect = [content, b""]
                    response.headers.get.return_value = length
                    if valid:
                        packager.download_file(DIRECTORY_URL + SOURCE_NAME, destination)
                        self.assertEqual(destination.read_bytes(), content)
                    else:
                        with self.assertRaisesRegex(ValueError, "Incomplete|Empty"):
                            packager.download_file(
                                DIRECTORY_URL + SOURCE_NAME, destination
                            )
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    @mock.patch.object(packager, "run_7zip")
    def test_listing_rejects_unsafe_paths(self, run: mock.Mock) -> None:
        for path in (
            r"..\outside.bin", r"binaries\..\outside.bin",
            r"C:\outside.bin", r"\outside.bin",
            r"\\server\share\outside.bin", r"binaries\file.bin:stream",
        ):
            with self.subTest(path=path):
                run.return_value = (
                    f"Path = {path}\nSize = 1\nAttributes = A\nCRC = 12345678\n"
                )
                with self.assertRaisesRegex(ValueError, "Unsafe archive member"):
                    packager.list_archive(Path("source.7z"), Path("7z.exe"))

    def test_manifest_rejects_links_duplicates_wrong_versions_and_extra_output(self) -> None:
        path = PureWindowsPath("binaries") / BIN_NAME
        valid = packager.ArchiveEntry(path, 10, "12345678", False, False)
        for entries, message in (
            ([packager.ArchiveEntry(path, 10, "12345678", False, True)], "Linked"),
            ([valid, valid], "duplicate"),
            ([packager.ArchiveEntry(path, 10, "", False, False)], "Missing CRC"),
            ([
                packager.ArchiveEntry(
                    PureWindowsPath(r"binaries\mmc_pkg_0.1024.0_other.bin"),
                    10, "12345678", False, False,
                )
            ], "do not match"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    packager.mmc_file_manifest(entries, VERSION, packaged=False)
        root = PureWindowsPath(f"MMCFW_{VERSION}")
        packaged = packager.ArchiveEntry(root / path, 10, "12345678", False, False)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            packager.mmc_file_manifest([packaged, packaged], VERSION, packaged=True)
        with self.assertRaisesRegex(ValueError, "outside MMC binaries"):
            packager.mmc_file_manifest([valid], VERSION, packaged=True)


@unittest.skipUnless(
    (packager.DEFAULT_REPACKAGING_DIR / "7-Zip" / "7z.exe").is_file(),
    "Integration tests require the existing repackager's bundled 7-Zip.",
)
class MmcPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mmc-tests.with.dots-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output with spaces.and.dots"
        self.output.mkdir()
        self.seven_zip = packager.DEFAULT_REPACKAGING_DIR / "7-Zip" / "7z.exe"
        bios_root = self.root / "bios fixtures"
        dsc = bios_root / "Trunk_AP00" / "dsc" / "FvRawDefPath.dsc"
        dsc.parent.mkdir(parents=True)
        dsc.write_text(
            f"DEFINE MMC1 = path\\{BIN_NAME}\nDEFINE MMC2 = path\\{BIN_NAME}\n",
            encoding="utf-8",
        )
        self.bios = self.output / "0040.D79.7z"
        self.create_archive(bios_root, self.bios)
        self.original_bios = self.bios.read_bytes()
        self.binary_files = {
            BIN_NAME: b"\x00\x01synthetic test binary" * 100,
            "LICENSE-test.txt": b"Synthetic fixture license.\n",
            r"nested\details.txt": b"nested binaries content\n",
        }
        self.source = self.root / "BuildPkg.7z"
        self.make_source({
            **{str(PureWindowsPath("binaries") / name): data
               for name, data in self.binary_files.items()},
            r"host_src_pkgs\source.txt": b"must be discarded",
            "ReadMe.txt": b"must be discarded",
            f"mmc_pkg_{VERSION}_loose.bin": b"must not be mixed into binaries",
        })
        fetch_patch = mock.patch.object(
            packager, "fetch_index", return_value=index_page(SOURCE_NAME, "loose.bin")
        )
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        download_patch = mock.patch.object(
            packager, "download_file",
            side_effect=lambda url, destination: shutil.copyfile(self.source, destination),
        )
        self.download = download_patch.start()
        self.addCleanup(download_patch.stop)

    def create_archive(self, source: Path, destination: Path) -> None:
        packager.run_7zip(
            self.seven_zip,
            [
                "a", "-t7z", "-y", "-bso0", "-bsp0", "--", str(destination),
                *(child.name for child in source.iterdir()),
            ],
            cwd=source,
        )

    def make_source(self, files: dict[str, bytes]) -> None:
        source_root = Path(tempfile.mkdtemp(prefix="source-", dir=self.root))
        for name, data in files.items():
            path = source_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.source.unlink(missing_ok=True)
        self.create_archive(source_root, self.source)

    def package(self, *, dry_run: bool = False) -> Path:
        return packager.package_mmc_fw(
            self.bios,
            artifactory_url=BASE_URL,
            repackaging_dir=packager.DEFAULT_REPACKAGING_DIR,
            output_dir=self.output,
            dry_run=dry_run,
        )

    def assert_bios_and_outputs(self, *extra: str) -> None:
        self.assertEqual(self.bios.read_bytes(), self.original_bios)
        self.assertEqual(
            {path.name for path in self.output.iterdir()}, {self.bios.name, *extra}
        )

    def test_real_archives_keep_only_binaries_with_exact_names_and_contents(self) -> None:
        destination = self.package()
        self.assertEqual(destination.name, f"MMCFW_{VERSION}.7z")
        self.assert_bios_and_outputs(destination.name)
        self.fetch.assert_called_once_with(DIRECTORY_URL)
        self.assertEqual(self.download.call_args.args[0], DIRECTORY_URL + SOURCE_NAME)
        unpacked = self.root / "verified result"
        packager.run_7zip(
            self.seven_zip,
            ["x", "-y", "-bso0", "-bsp0", f"-o{unpacked}", "--", str(destination)],
        )
        binaries = unpacked / f"MMCFW_{VERSION}" / "binaries"
        actual = {
            str(path.relative_to(binaries)): path.read_bytes()
            for path in binaries.rglob("*") if path.is_file()
        }
        self.assertEqual(actual, self.binary_files)
        self.assertEqual(
            {entry.name for entry in (unpacked / f"MMCFW_{VERSION}").iterdir()},
            {"binaries"},
        )

    def test_existing_mmc_is_replaced_only_after_validation(self) -> None:
        destination = self.output / f"MMCFW_{VERSION}.7z"
        destination.write_bytes(b"old MMC")
        self.assertEqual(self.package(), destination)
        packager.validate_archive(destination, self.seven_zip)
        self.assert_bios_and_outputs(destination.name)

    def test_dry_run_reads_exact_version_without_download_or_workspace(self) -> None:
        destination = self.package(dry_run=True)
        self.assertEqual(destination.name, f"MMCFW_{VERSION}.7z")
        self.fetch.assert_called_once_with(DIRECTORY_URL)
        self.download.assert_not_called()
        self.assert_bios_and_outputs()

    def test_network_failure_preserves_bios_and_cleans_owned_workspace(self) -> None:
        self.download.side_effect = URLError("network unavailable")
        with self.assertRaisesRegex(URLError, "network unavailable"):
            self.package()
        self.assert_bios_and_outputs()

    def test_corrupt_source_preserves_previous_mmc_and_bios(self) -> None:
        destination = self.output / f"MMCFW_{VERSION}.7z"
        destination.write_bytes(b"previous MMC")
        self.source.write_bytes(b"not an archive")
        with self.assertRaisesRegex(RuntimeError, "7-Zip failed"):
            self.package()
        self.assertEqual(destination.read_bytes(), b"previous MMC")
        self.assert_bios_and_outputs(destination.name)

    def test_missing_or_wrong_version_binaries_are_not_published(self) -> None:
        for files in (
            {"ReadMe.txt": b"no binaries"},
            {r"binaries\mmc_pkg_0.1024.0_other.bin": b"wrong version"},
        ):
            with self.subTest(files=list(files)):
                self.make_source(files)
                with self.assertRaisesRegex(ValueError, "do not match BIOS version"):
                    self.package()
                self.assert_bios_and_outputs()

    def test_extraction_error_is_reported_without_publishing(self) -> None:
        original_run = packager.run_7zip

        def fail_extraction(
            tool: Path, arguments: list[str], *, cwd: Path | None = None
        ) -> str:
            if arguments[0] == "x" and "-so" not in arguments:
                raise RuntimeError("extraction failed")
            return original_run(tool, arguments, cwd=cwd)

        with mock.patch.object(packager, "run_7zip", side_effect=fail_extraction):
            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                self.package()
        self.assert_bios_and_outputs()

    def test_same_size_content_change_is_rejected_by_source_crc_comparison(self) -> None:
        original_run = packager.run_7zip

        def change_extracted_binary(
            tool: Path, arguments: list[str], *, cwd: Path | None = None
        ) -> str:
            result = original_run(tool, arguments, cwd=cwd)
            if arguments[0] == "x" and "-so" not in arguments:
                folder = Path(next(arg[2:] for arg in arguments if arg.startswith("-o")))
                binary = folder / "binaries" / BIN_NAME
                binary.write_bytes(b"X" * binary.stat().st_size)
            return result

        with mock.patch.object(
            packager, "run_7zip", side_effect=change_extracted_binary
        ):
            with self.assertRaisesRegex(ValueError, "sizes/CRCs differ"):
                self.package()
        self.assert_bios_and_outputs()


class MmcOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        logging_patch = mock.patch.object(packager, "configure_logging")
        logging_patch.start()
        self.addCleanup(logging_patch.stop)
        bios_patch = mock.patch.object(
            packager, "package_latest", return_value=Path("0040.D79.7z")
        )
        self.bios = bios_patch.start()
        self.addCleanup(bios_patch.stop)
        mmc_patch = mock.patch.object(packager, "package_mmc_fw")
        self.mmc = mmc_patch.start()
        self.addCleanup(mmc_patch.stop)

    def test_default_run_packages_mmc_after_successful_bios(self) -> None:
        self.mmc.side_effect = lambda *args, **kwargs: self.bios.assert_called_once()
        self.assertEqual(packager.main([]), 0)
        self.bios.assert_called_once()
        self.mmc.assert_called_once_with(
            Path("0040.D79.7z"),
            artifactory_url=packager.DEFAULT_MMC_ARTIFACTORY_URL,
            repackaging_dir=packager.DEFAULT_REPACKAGING_DIR,
            output_dir=packager.DEFAULT_OUTPUT_DIR,
            dry_run=False,
        )

    def test_existing_bios_mode_bypasses_bios_packaging(self) -> None:
        self.assertEqual(packager.main(["--bios-archive", "existing.7z"]), 0)
        self.bios.assert_not_called()
        self.assertEqual(self.mmc.call_args.args[0], Path("existing.7z"))

    def test_default_dry_run_does_not_guess_mmc_from_an_old_archive(self) -> None:
        self.assertEqual(packager.main(["--dry-run"]), 0)
        self.assertTrue(self.bios.call_args.kwargs["dry_run"])
        self.mmc.assert_not_called()

    def test_existing_bios_dry_run_is_forwarded_to_mmc(self) -> None:
        self.assertEqual(
            packager.main(["--bios-archive", "existing.7z", "--dry-run"]), 0
        )
        self.bios.assert_not_called()
        self.assertTrue(self.mmc.call_args.kwargs["dry_run"])

    def test_mmc_failure_is_logged_and_returns_nonzero(self) -> None:
        self.mmc.side_effect = RuntimeError("MMC source missing")
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            self.assertEqual(packager.main([]), 1)
        self.assertIn("MMC source missing", "\n".join(logged.output))

    @mock.patch.object(packager.time, "sleep")
    def test_owned_workspace_removal_retries_directory_lock(self, sleep: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "owned-mmc-job"
            workspace.mkdir()
            (workspace / "download.7z").write_bytes(b"cache")
            original_rmdir = Path.rmdir
            attempts = 0

            def remove_after_unlock(path: Path) -> None:
                nonlocal attempts
                if path == workspace:
                    attempts += 1
                    if attempts == 1:
                        error = PermissionError("directory locked")
                        error.winerror = 32
                        raise error
                original_rmdir(path)

            with mock.patch.object(Path, "rmdir", remove_after_unlock):
                with self.assertLogs("bios_packager", level="WARNING"):
                    packager.clear_workspace(workspace, remove_directory=True)
            self.assertFalse(workspace.exists())
            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
