from __future__ import annotations

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from seven_zip_support import SEVEN_ZIP

from bios_mmc import artifactory, bios, fsutil
from bios_mmc.artifactory import BiosBuild
from bios_mmc.mapping import ComponentPackage, MappingRow

BUILD_URL = "https://example.test/main/OAKSTRM.0.RPB.0040.D.79/"
DEBUG_PKG = r"OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_\d+_BuildPkg.7z"


def listing(*entries: tuple[str, str]) -> str:
    lines = [f'<a href="{name}">{name}</a>   {stamp}  1 MB' for name, stamp in entries]
    return "<pre><a href=\"../\">../</a>\n" + "\n".join(lines) + "</pre>"


class SelectionTests(unittest.TestCase):
    def test_latest_file_uses_index_time_not_link_order_or_build_number(self) -> None:
        html = listing(
            ("OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_900_BuildPkg.7z",
             "07-Nov-2026 19:54"),
            ("OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_100_BuildPkg.7z",
             "08-Nov-2026 01:00"),
            ("OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_999_BuildFdr.7z",
             "09-Nov-2026 01:00"),
        )
        self.assertEqual(
            artifactory.select_latest_file(html, BUILD_URL, DEBUG_PKG),
            BUILD_URL + "OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_100_BuildPkg.7z",
        )
        self.assertIsNone(artifactory.select_latest_file(html, BUILD_URL, "Nope"))

    def test_folder_prefers_exact_name_over_longer_variants(self) -> None:
        html = listing(
            ("OakStreamRp_DMR_Debug_CLANG_Linux/", "07-Nov-2026 19:54"),
            ("OakStreamRp_DMR_Debug_CLANG/", "07-Nov-2026 19:54"),
        )
        self.assertEqual(
            artifactory.select_subdirectory(html, BUILD_URL, "OakStreamRp_DMR_Debug_CLANG"),
            BUILD_URL + "OakStreamRp_DMR_Debug_CLANG/",
        )
        self.assertEqual(
            artifactory.select_subdirectory(html, BUILD_URL, "DMR_Debug"),
            BUILD_URL + "OakStreamRp_DMR_Debug_CLANG_Linux/",
        )
        self.assertIsNone(artifactory.select_subdirectory(html, BUILD_URL, "VAB"))

    def test_bios_id_formats(self) -> None:
        for value in ("0040D79", "0040.D79", "0040.D.79", "OAKSTRM.0.RPB.0040.D.79"):
            with self.subTest(value=value):
                build = artifactory.parse_bios_id(value)
                self.assertEqual((build.bios_id, build.package_name),
                                 ("0040D79", "0040.D79.7z"))
        with self.assertRaisesRegex(ValueError, "Unrecognized BIOS ID"):
            artifactory.parse_bios_id("latest")

    def test_requested_build_must_exist(self) -> None:
        html = '<a href="OAKSTRM.0.RPB.0040.D.79/">x</a>'
        build = artifactory.select_build(html, artifactory.parse_bios_id("0040D79"))
        self.assertEqual(build.directory_name, "OAKSTRM.0.RPB.0040.D.79")
        with self.assertRaisesRegex(ValueError, "not in the Artifactory index"):
            artifactory.select_build(html, artifactory.parse_bios_id("0040D80"))


class PathRuleTests(unittest.TestCase):
    def test_mapping_targets_split_on_backslashes_and_reject_escape(self) -> None:
        self.assertEqual(
            bios.safe_relative(r"Trunk_1P0_Debug\\bios_fd"), Path("Trunk_1P0_Debug", "bios_fd")
        )
        for value in ("", r"..\\x", r"\\root", "C:\\x", "/abs"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    bios.safe_relative(value)

    def test_match_string_uses_windows_separators_on_every_platform(self) -> None:
        self.assertNotIn("/", bios.match_string(Path("a") / "b" / "c"))

    def test_dsc_combination_matches_original_windows_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "BiosPkg" / "SourceFile"
            source.mkdir(parents=True)
            (source / "FvRawDefPath.dsc").write_bytes(b"A1\r\nA2")
            (source / "FvRawDefPathB.dsc").write_bytes(b"B1\nB2\n")
            (source / "FvRawDefPathA.DSC").write_bytes(b"C1\r")
            (source / "FvRawDefPathZ.txt").write_bytes(b"ignored")
            (source / "Other.dsc").write_bytes(b"ignored")
            destination = root / "out"
            destination.mkdir()
            self.assertTrue(
                bios.combine_dsc(r"BiosPkg\\SourceFile\\FvRawDefPath.dsc", destination, root)
            )
            self.assertEqual(
                (destination / "FvRawDefPath.dsc").read_bytes(),
                b"A1\r\nA2\r\r\n" b"C1\r\n\r\r\n" b"B1\r\nB2\r\n\r\r\n",
            )
            self.assertFalse(
                bios.combine_dsc(r"BiosPkg\\SourceFile\\FvOpRomPath.dsc", destination, root)
            )


@unittest.skipUnless(SEVEN_ZIP is not None, "Integration tests require native 7-Zip.")
class RestructureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="bios tests.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output"
        self.output.mkdir()
        (self.output / "keep.txt").write_bytes(b"unrelated output is preserved")
        tree = self.root / "pkg"
        files = {
            "BiosPkg/BiosFile/MAIN.fd": b"main firmware",
            "BiosPkg/SourceFile/FvRawDefPath.dsc": b"DEFINE MMC1 = mmc_pkg_0.1.0_x.bin\n",
            "BiosPkg/SourceFile/FvRawDefPath2.dsc": b"DEFINE MMC2 = mmc_pkg_0.1.0_x.bin\n",
            "EmuMinibiosTemplate/inner/template.bin": b"template",
            "OSXML_Version.html": b"<html/>",
        }
        for name, data in files.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("Stitch/Capsule.ffs", b"ffs payload")
        (tree / bios.NESTED_ZIP).write_bytes(nested.getvalue())
        self.package = self.root / "OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_42_BuildPkg.7z"
        fsutil.create_archive(tree, self.package, SEVEN_ZIP)

        self.components = [
            ComponentPackage(DEBUG_PKG, "OakStreamRp_DMR_Debug_CLANG", 2, [
                MappingRow(r"..\\EmuMinibiosTemplate", "EmuMinibiosTemplate"),
                MappingRow(r"..\\OSXML_Version.html", "1P0"),
                MappingRow(r"BiosPkg\\BiosFile\\MAIN.fd", r"Trunk_1P0_Debug\\bios_fd"),
                MappingRow(
                    r"BiosPkg\\SourceFile\\FvRawDefPath.dsc", r"Trunk_1P0_Debug\\dsc"
                ),
                MappingRow(r"Stitch\\Capsule.ffs", r"Trunk_1P0_Debug\\ffs"),
            ]),
            ComponentPackage(r"Missing_\d+.7z", "OakStreamRp_DMR_VAB_Debug_CLANG", 9, [
                MappingRow("MAIN.fd", r"Trunk_VAB\\bios_fd"),
            ]),
        ]
        package_name = self.package.name
        self.http = mock.Mock(spec=artifactory.HttpClient)
        self.http.get_text.side_effect = lambda url: {
            BUILD_URL: listing(("OakStreamRp_DMR_Debug_CLANG/", "07-Nov-2026 19:54")),
            BUILD_URL + "OakStreamRp_DMR_Debug_CLANG/": listing(
                (package_name, "07-Nov-2026 19:54"),
                ("OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_41_BuildPkg.7z",
                 "06-Nov-2026 19:54"),
            ),
        }[url]
        self.http.download.side_effect = (
            lambda url, destination: shutil.copyfile(self.package, destination)
        )

    def run_package(self, *, dry_run: bool = False) -> Path:
        return bios.package_bios(
            BiosBuild("0040", "79"),
            build_url=BUILD_URL,
            components=self.components,
            output_dir=self.output,
            http=self.http,
            seven_zip=SEVEN_ZIP,
            jobs=2,
            retries=2,
            dry_run=dry_run,
        )

    def contents(self, archive: Path) -> dict[str, bytes | None]:
        unpacked = self.root / "unpacked"
        shutil.rmtree(unpacked, ignore_errors=True)
        fsutil.extract_archive(archive, unpacked, SEVEN_ZIP)
        return {
            path.relative_to(unpacked).as_posix(): (
                path.read_bytes() if path.is_file() else None
            )
            for path in unpacked.rglob("*")
        }

    def test_restructures_like_the_original_and_reports_missing_component(self) -> None:
        with self.assertLogs("bios_packager", level="INFO") as logged:
            archive = self.run_package()
        self.assertEqual(archive, self.output / "0040.D79.7z")
        self.assertEqual(
            sorted(path.name for path in self.output.iterdir()), ["0040.D79.7z", "keep.txt"]
        )
        self.http.download.assert_called_once_with(
            BUILD_URL + "OakStreamRp_DMR_Debug_CLANG/" + self.package.name, mock.ANY
        )
        contents = self.contents(archive)
        self.assertEqual(contents["EmuMinibiosTemplate/inner/template.bin"], b"template")
        self.assertEqual(contents["1P0/OSXML_Version.html"], b"<html/>")
        self.assertEqual(contents["Trunk_1P0_Debug/bios_fd/MAIN.fd"], b"main firmware")
        self.assertEqual(contents["Trunk_1P0_Debug/ffs/Capsule.ffs"], b"ffs payload")
        self.assertEqual(
            contents["Trunk_1P0_Debug/dsc/FvRawDefPath.dsc"],
            b"DEFINE MMC1 = mmc_pkg_0.1.0_x.bin\r\n\r\r\n"
            b"DEFINE MMC2 = mmc_pkg_0.1.0_x.bin\r\n\r\r\n",
        )
        # The original pre-creates every target folder, even for missing packages.
        self.assertIn("Trunk_VAB/bios_fd", contents)
        log = "\n".join(logged.output)
        self.assertIn("total=2, packaged=1, not packaged=1", log)
        self.assertIn("OakStreamRp_DMR_VAB_Debug_CLANG", log)
        self.assertEqual(
            [p.name for p in self.output.iterdir() if p.name.startswith("bios-")], []
        )

    def test_partial_matches_are_kept_but_component_is_not_packaged(self) -> None:
        self.components[0].rows.append(MappingRow("DoesNotExist.bin", "1P0"))
        with self.assertLogs("bios_packager", level="WARNING") as logged:
            archive = self.run_package()
        self.assertIn("1P0/OSXML_Version.html", self.contents(archive))
        self.assertIn("not packaged=2", "\n".join(logged.output))

    def test_truncated_download_is_retried(self) -> None:
        attempts: list[str] = []

        def flaky(url: str, destination: Path) -> None:
            attempts.append(url)
            if len(attempts) == 1:
                destination.write_bytes(self.package.read_bytes()[:100])
                return
            shutil.copyfile(self.package, destination)

        self.http.download.side_effect = flaky
        with self.assertLogs("bios_packager", level="WARNING") as logged:
            self.run_package()
        self.assertEqual(len(attempts), 2)
        self.assertIn("(1/2)", "\n".join(logged.output))

    def test_dry_run_resolves_sources_without_downloading_or_writing(self) -> None:
        self.run_package(dry_run=True)
        self.http.download.assert_not_called()
        self.assertEqual([p.name for p in self.output.iterdir()], ["keep.txt"])

    def test_no_files_found_does_not_publish_or_replace_previous_archive(self) -> None:
        previous = self.output / "0040.D79.7z"
        previous.write_bytes(b"previous")
        self.http.download.side_effect = OSError("network down")
        with self.assertLogs("bios_packager", level="WARNING"):
            with self.assertRaisesRegex(ValueError, "No BIOS component files"):
                self.run_package()
        self.assertEqual(previous.read_bytes(), b"previous")
        self.assertEqual(
            sorted(p.name for p in self.output.iterdir()), ["0040.D79.7z", "keep.txt"]
        )


if __name__ == "__main__":
    unittest.main()
