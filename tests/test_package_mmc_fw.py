from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock
from urllib.error import URLError

from seven_zip_support import SEVEN_ZIP

from bios_mmc import artifactory, fsutil, mmc
from bios_mmc.fsutil import ArchiveEntry


VERSION = "0.1022.0"
SOURCE_NAME = "ZCodeFW_test_DMR_ZCODE_DebugSigned_linux_309_BuildPkg.7z"
BASE_URL = "https://example.test/mmc/"  # branch folder
VERSION_URL = BASE_URL + f"MMC_FW.DMR_AP.{VERSION}/"
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

    @mock.patch.object(mmc, "run_7zip")
    def test_reads_all_definitions_and_ignores_unrelated_versions(
        self, run: mock.Mock
    ) -> None:
        run.return_value = (
            f"  DEFINE MMC1 = path\\{BIN_NAME}\n"
            f"  DEFINE MMC2 = other\\{BIN_NAME}\n"
        ) * 20 + "DEFINE OTHER = mmc_pkg_0.1024.0_other.bin\n"
        self.assertEqual(mmc.read_mmc_version(self.bios, Path("7z")), VERSION)
        self.assertEqual(run.call_args.args[1][-1], "Trunk_*/dsc/FvRawDefPath.dsc")

    @mock.patch.object(mmc, "run_7zip")
    def test_conflicting_versions_fail_instead_of_selecting_latest(
        self, run: mock.Mock
    ) -> None:
        run.return_value = (
            f"DEFINE MMC1 = {BIN_NAME}\n"
            "DEFINE MMC2 = mmc_pkg_0.1024.0_test.bin\n"
        )
        with self.assertRaisesRegex(ValueError, "Conflicting MMC versions"):
            mmc.read_mmc_version(self.bios, Path("7z"))

    @mock.patch.object(mmc, "run_7zip")
    def test_missing_and_unversioned_references_fail(self, run: mock.Mock) -> None:
        for content in (
            "", f"# DEFINE MMC1 = {BIN_NAME}",
            f"DEFINE MMC1 = {BIN_NAME}\nDEFINE MMC2 = unknown.bin\n",
        ):
            with self.subTest(content=content):
                run.return_value = content
                with self.assertRaisesRegex(ValueError, "No MMC|no recognizable version"):
                    mmc.read_mmc_version(self.bios, Path("7z"))

    @mock.patch.object(mmc, "run_7zip")
    def test_missing_archive_fails_without_extraction(self, run: mock.Mock) -> None:
        self.bios.unlink()
        with self.assertRaisesRegex(ValueError, "missing or empty"):
            mmc.read_mmc_version(self.bios, Path("7z"))
        run.assert_not_called()


class MmcVersionFolderTests(unittest.TestCase):
    def test_prefixed_and_plain_version_folders_are_found(self) -> None:
        for folder in (f"MMC_FW.DMR_AP.{VERSION}/", f"MMC_FW.{VERSION}/"):
            with self.subTest(folder=folder):
                html = index_page("../", folder, "MMC_FW.DMR_AP.0.1024.0/", "SNCU.1/")
                self.assertEqual(
                    mmc.select_mmc_version_dir(html, BASE_URL, VERSION), BASE_URL + folder
                )

    def test_other_versions_and_files_never_match(self) -> None:
        for html in (
            index_page("MMC_FW.DMR_AP.0.1022.01/", "MMC_FW.1.0.1022.0/",
                       f"MMC_FW.DMR_AP.{VERSION}", "MMC_FW.DMR_AP.0x1022x0/"),
            "<html>Please sign in</html>",
        ):
            with self.subTest(html=html):
                with self.assertRaisesRegex(ValueError, "found 0"):
                    mmc.select_mmc_version_dir(html, BASE_URL, VERSION)

    def test_ambiguous_version_folders_fail(self) -> None:
        html = index_page(f"MMC_FW.DMR_AP.{VERSION}/", f"MMC_FW.{VERSION}/")
        with self.assertRaisesRegex(ValueError, "found 2"):
            mmc.select_mmc_version_dir(html, BASE_URL, VERSION)


class MmcSourceTests(unittest.TestCase):
    def test_selects_only_direct_build_package_and_deduplicates_links(self) -> None:
        html = index_page(
            "../", SOURCE_NAME, SOURCE_NAME,
            f"mmc_pkg_{VERSION}_00_sign-prod-release.bin",
            f"nested/{SOURCE_NAME}", f"../{SOURCE_NAME}",
            f"https://other.test/{SOURCE_NAME}",
        )
        self.assertEqual(
            artifactory.select_mmc_source(html, DIRECTORY_URL),
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
                    artifactory.select_mmc_source(html, DIRECTORY_URL)

    @mock.patch.object(artifactory, "urlopen")
    def test_download_checks_length_and_rejects_empty_data(
        self, urlopen: mock.Mock
    ) -> None:
        http = artifactory.HttpClient()
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
                        http.download(DIRECTORY_URL + SOURCE_NAME, destination)
                        self.assertEqual(destination.read_bytes(), content)
                    else:
                        with self.assertRaisesRegex(ValueError, "Incomplete|Empty"):
                            http.download(DIRECTORY_URL + SOURCE_NAME, destination)
            self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)

    @mock.patch.object(fsutil, "run_7zip")
    def test_listing_rejects_unsafe_paths(self, run: mock.Mock) -> None:
        for path in (
            r"..\outside.bin", r"binaries\..\outside.bin",
            r"C:\outside.bin", r"\outside.bin", "/outside.bin",
            r"\\server\share\outside.bin", r"binaries\file.bin:stream",
        ):
            with self.subTest(path=path):
                run.return_value = (
                    f"Path = {path}\nSize = 1\nAttributes = A\nCRC = 12345678\n"
                )
                with self.assertRaisesRegex(ValueError, "Unsafe archive member"):
                    fsutil.list_archive(Path("source.7z"), Path("7z"))

    def test_manifest_rejects_links_duplicates_wrong_versions_and_extra_output(self) -> None:
        path = PureWindowsPath("binaries") / BIN_NAME
        valid = ArchiveEntry(path, 10, "12345678", False, False)
        for entries, message in (
            ([ArchiveEntry(path, 10, "12345678", False, True)], "Linked"),
            ([valid, valid], "duplicate"),
            ([ArchiveEntry(path, 10, "", False, False)], "Missing CRC"),
            ([
                ArchiveEntry(
                    PureWindowsPath(r"binaries\mmc_pkg_0.1024.0_other.bin"),
                    10, "12345678", False, False,
                )
            ], "do not match"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    mmc.mmc_file_manifest(entries, VERSION, packaged=False)
        root = PureWindowsPath(f"MMCFW_{VERSION}")
        packaged = ArchiveEntry(root / path, 10, "12345678", False, False)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            mmc.mmc_file_manifest([packaged, packaged], VERSION, packaged=True)
        with self.assertRaisesRegex(ValueError, "outside MMC binaries"):
            mmc.mmc_file_manifest([valid], VERSION, packaged=True)


@unittest.skipUnless(SEVEN_ZIP is not None, "Integration tests require native 7-Zip.")
class MmcPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mmc-tests.with.dots-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output with spaces.and.dots"
        self.output.mkdir()
        self.seven_zip = SEVEN_ZIP
        bios_root = self.root / "bios fixtures"
        dsc = bios_root / "Trunk_AP00" / "dsc" / "FvRawDefPath.dsc"
        dsc.parent.mkdir(parents=True)
        dsc.write_text(
            f"DEFINE MMC1 = path\\{BIN_NAME}\nDEFINE MMC2 = path\\{BIN_NAME}\n",
            encoding="utf-8",
        )
        self.bios = self.output / "0040.D79.7z"
        fsutil.create_archive(bios_root, self.bios, self.seven_zip)
        self.original_bios = self.bios.read_bytes()
        self.binary_files = {
            BIN_NAME: b"\x00\x01synthetic test binary" * 100,
            "LICENSE-test.txt": b"Synthetic fixture license.\n",
            str(Path("nested") / "details.txt"): b"nested binaries content\n",
        }
        self.source = self.root / "BuildPkg.7z"
        self.make_source({
            **{f"binaries/{name}": data for name, data in self.binary_files.items()},
            "host_src_pkgs/source.txt": b"must be discarded",
            "ReadMe.txt": b"must be discarded",
            f"mmc_pkg_{VERSION}_loose.bin": b"must not be mixed into binaries",
        })
        self.http = mock.Mock(spec=artifactory.HttpClient)
        self.http.get_text.side_effect = lambda url: {
            BASE_URL: index_page(
                f"MMC_FW.DMR_AP.{VERSION}/", "MMC_FW.DMR_AP.0.1024.0/", "SNCU.26450200/"
            ),
            VERSION_URL: index_page("DMR_ZCODE/", "DMR_ZCODE_DebugSigned_linux/"),
            DIRECTORY_URL: index_page(SOURCE_NAME, "loose.bin"),
        }[url]
        self.http.download.side_effect = (
            lambda url, destination: shutil.copyfile(self.source, destination)
        )

    def make_source(self, files: dict[str, bytes]) -> None:
        source_root = Path(tempfile.mkdtemp(prefix="source-", dir=self.root))
        for name, data in files.items():
            path = source_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.source.unlink(missing_ok=True)
        fsutil.create_archive(source_root, self.source, self.seven_zip)

    def package(
        self, *, dry_run: bool = False, archive_threads: int | None = None,
    ) -> Path:
        return mmc.package_mmc_fw(
            self.bios,
            branch_url=BASE_URL,
            output_dir=self.output,
            http=self.http,
            seven_zip=self.seven_zip,
            dry_run=dry_run,
            archive_threads=archive_threads,
        )

    def assert_bios_and_outputs(self, *extra: str) -> None:
        self.assertEqual(self.bios.read_bytes(), self.original_bios)
        self.assertEqual(
            {path.name for path in self.output.iterdir()}, {self.bios.name, *extra}
        )

    def test_archive_threads_apply_only_to_final_compression(self) -> None:
        for threads in (None, 1, 2):
            with self.subTest(threads=threads), mock.patch.object(
                mmc, "run_7zip", wraps=mmc.run_7zip,
            ) as run:
                destination = self.package(archive_threads=threads)
                commands = [call.args[1] for call in run.call_args_list]
                compression = [args for args in commands if args[0] == "a"]
                self.assertEqual(len(compression), 1)
                self.assertEqual(
                    [arg for arg in compression[0] if arg.startswith("-mmt=")],
                    [] if threads is None else [f"-mmt={threads}"],
                )
                for args in commands:
                    if args[0] != "a":
                        self.assertFalse(any(arg.startswith("-mmt=") for arg in args))
                fsutil.validate_archive(destination, self.seven_zip)
                self.assert_bios_and_outputs(destination.name)

    def test_real_archives_keep_only_binaries_with_exact_names_and_contents(self) -> None:
        destination = self.package()
        self.assertEqual(destination.name, f"MMCFW_{VERSION}.7z")
        self.assert_bios_and_outputs(destination.name)
        self.assertEqual(
            [c.args[0] for c in self.http.get_text.call_args_list], [BASE_URL, VERSION_URL, DIRECTORY_URL]
        )
        self.assertEqual(self.http.download.call_args.args[0], DIRECTORY_URL + SOURCE_NAME)
        unpacked = self.root / "verified result"
        fsutil.extract_archive(destination, unpacked, self.seven_zip)
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

    def test_version_without_debug_signed_build_fails_clearly(self) -> None:
        pages = {
            BASE_URL: index_page(f"MMC_FW.{VERSION}/"),
            BASE_URL + f"MMC_FW.{VERSION}/": index_page("PinFile.xml", "DiffPinfile.json"),
        }
        self.http.get_text.side_effect = lambda url: pages[url]
        with self.assertRaisesRegex(ValueError, "has no DMR_ZCODE_DebugSigned_linux/ build"):
            self.package()
        self.http.download.assert_not_called()
        self.assert_bios_and_outputs()

    def test_existing_mmc_is_replaced_only_after_validation(self) -> None:
        destination = self.output / f"MMCFW_{VERSION}.7z"
        destination.write_bytes(b"old MMC")
        self.assertEqual(self.package(), destination)
        fsutil.validate_archive(destination, self.seven_zip)
        self.assert_bios_and_outputs(destination.name)

    def test_dry_run_reads_exact_version_without_download_or_workspace(self) -> None:
        destination = self.package(dry_run=True)
        self.assertEqual(destination.name, f"MMCFW_{VERSION}.7z")
        self.assertEqual(
            [c.args[0] for c in self.http.get_text.call_args_list], [BASE_URL, VERSION_URL, DIRECTORY_URL]
        )
        self.http.download.assert_not_called()
        self.assert_bios_and_outputs()

    def test_network_failure_preserves_bios_and_cleans_owned_workspace(self) -> None:
        self.http.download.side_effect = URLError("network unavailable")
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
            {"binaries/mmc_pkg_0.1024.0_other.bin": b"wrong version"},
        ):
            with self.subTest(files=list(files)):
                self.make_source(files)
                with self.assertRaisesRegex(ValueError, "do not match BIOS version"):
                    self.package()
                self.assert_bios_and_outputs()

    def test_extraction_error_is_reported_without_publishing(self) -> None:
        original_run = mmc.run_7zip

        def fail_extraction(
            tool: Path, arguments: list[str], *, cwd: Path | None = None
        ) -> str:
            if arguments[0] == "x" and "-so" not in arguments:
                raise RuntimeError("extraction failed")
            return original_run(tool, arguments, cwd=cwd)

        with mock.patch.object(mmc, "run_7zip", side_effect=fail_extraction):
            with self.assertRaisesRegex(RuntimeError, "extraction failed"):
                self.package()
        self.assert_bios_and_outputs()

    def test_same_size_content_change_is_rejected_by_source_crc_comparison(self) -> None:
        original_run = mmc.run_7zip

        def change_extracted_binary(
            tool: Path, arguments: list[str], *, cwd: Path | None = None
        ) -> str:
            result = original_run(tool, arguments, cwd=cwd)
            if arguments[0] == "x" and "-so" not in arguments:
                folder = Path(next(arg[2:] for arg in arguments if arg.startswith("-o")))
                binary = folder / "binaries" / BIN_NAME
                binary.write_bytes(b"X" * binary.stat().st_size)
            return result

        with mock.patch.object(mmc, "run_7zip", side_effect=change_extracted_binary):
            with self.assertRaisesRegex(ValueError, "sizes/CRCs differ"):
                self.package()
        self.assert_bios_and_outputs()


class WorkspaceCleanupTests(unittest.TestCase):
    @mock.patch.object(fsutil.time, "sleep")
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
                    fsutil.clear_workspace(workspace, remove_directory=True)
            self.assertFalse(workspace.exists())
            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(2)

    def test_other_permission_errors_are_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "job"
            workspace.mkdir()
            (workspace / "file").write_bytes(b"x")
            with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(PermissionError, "denied"):
                    fsutil.clear_workspace(workspace)


if __name__ == "__main__":
    unittest.main()
