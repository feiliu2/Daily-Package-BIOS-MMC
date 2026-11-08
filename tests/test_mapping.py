from __future__ import annotations

import http.server
import threading
import unittest
from pathlib import Path

import seven_zip_support  # noqa: F401  (adds the repository root to sys.path)
from xlsx_support import HEADER, make_xlsx

from bios_mmc import mapping

PKG = r"OakStreamRp_main_OakStreamRp_DMR_Debug_CLANG_\d+_BuildPkg.7z"
REAL_MAPPING = Path(
    r"C:\Users\binsun1\Workspace\OakStreamAPIfwi\Ifwi\oak_release\BuildScripts"
    r"\CommonLib\BIOS_Repackaging\biosid_oak_ap.xlsx"
)


def rows(*body: list[str]) -> list[list[str]]:
    return [HEADER, *body]


class ParseMappingTests(unittest.TestCase):
    def test_groups_rows_by_package_and_folder_in_first_seen_order(self) -> None:
        data = make_xlsx(rows(
            [PKG, r"..\\EmuMinibiosTemplate", "Debug", "", "EmuMinibiosTemplate"],
            ["Other_\\d+.7z", r"MAIN.fd", "Other", "", r"Trunk\\bios_fd"],
            [PKG, r"BiosPkg\\BiosFile\\MAIN.fd", "Debug", "", r"Trunk_1P0\\bios_fd"],
        ))
        components = mapping.parse_mapping(data)
        self.assertEqual([c.folder for c in components], ["Debug", "Other"])
        self.assertEqual(components[0].excel_line, 2)
        self.assertEqual(components[0].package_pattern, PKG)
        self.assertEqual(
            [(row.binary, row.target) for row in components[0].rows],
            [(r"..\\EmuMinibiosTemplate", "EmuMinibiosTemplate"),
             (r"BiosPkg\\BiosFile\\MAIN.fd", r"Trunk_1P0\\bios_fd")],
        )
        self.assertEqual(components[1].excel_line, 3)

    def test_inline_strings_and_sparse_cells_keep_column_positions(self) -> None:
        data = make_xlsx(rows([PKG, "MAIN.fd", "Debug", "", "1P0"]), inline=True)
        component = mapping.parse_mapping(data)[0]
        self.assertEqual(component.rows[0].target, "1P0")

    def test_blank_rows_are_skipped_but_partial_rows_fail(self) -> None:
        data = make_xlsx(rows(["", "", "", "", ""], [PKG, "MAIN.fd", "Debug", "", "1P0"]))
        self.assertEqual(len(mapping.parse_mapping(data)), 1)
        with self.assertRaisesRegex(ValueError, "line 2 has empty"):
            mapping.parse_mapping(make_xlsx(rows([PKG, "MAIN.fd", "Debug", "", ""])))

    def test_invalid_workbooks_fail_clearly(self) -> None:
        for data, message in (
            (b"<html>login</html>", "not a valid .xlsx"),
            (make_xlsx(rows([PKG, "a", "b", "", "c"]), sheet="other"), "no 'config'"),
            (make_xlsx([["Wrong"], ["x"]]), "missing a required column"),
            (make_xlsx([HEADER]), "no component rows"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    mapping.parse_mapping(data)

    @unittest.skipUnless(REAL_MAPPING.is_file(), "OakStreamAPIfwi mapping not present")
    def test_real_oak_ap_mapping_matches_original_grouping(self) -> None:
        components = mapping.parse_mapping(REAL_MAPPING.read_bytes())
        self.assertEqual(len(components), 10)
        self.assertEqual(sum(len(c.rows) for c in components), 553)


class _Handler(http.server.BaseHTTPRequestHandler):
    responses: dict[str, tuple[int, bytes]] = {}
    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append(dict(self.headers))
        status, body = type(self).responses.get(self.path, (404, b"missing"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class FetchMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.responses = {}
        _Handler.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_raw_download_sends_token_and_parses(self) -> None:
        _Handler.responses["/x.xlsx"] = (
            200, make_xlsx(rows([PKG, "MAIN.fd", "Debug", "", "1P0"]))
        )
        components, loaded = mapping.load_mapping(
            mapping.MappingSource("url", self.base + "/x.xlsx"), token="secret-token"
        )
        self.assertEqual(len(components), 1)
        self.assertEqual((loaded.component_packages, loaded.file_rows), (1, 1))
        self.assertRegex(loaded.sha256, "^[0-9a-f]{64}$")
        headers = {key.lower(): value for key, value in _Handler.seen[0].items()}
        self.assertEqual(headers["authorization"], "Bearer secret-token")
        self.assertEqual(headers["accept"], "application/vnd.github.raw")

    def test_http_errors_and_non_xlsx_fail_without_fallback(self) -> None:
        _Handler.responses["/denied"] = (401, b"Bad credentials")
        _Handler.responses["/html"] = (200, b"<html>sso</html>")
        for path, message in (
            ("/denied", "HTTP 401"), ("/missing", "HTTP 404"),
            ("/html", "not a valid .xlsx"),
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, message):
                    mapping.load_mapping(
                        mapping.MappingSource("url", self.base + path), token="t"
                    )

    def test_missing_token_fails_before_any_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "IFWI_GITHUB_TOKEN is not set"):
            mapping.load_mapping(mapping.MappingSource("url", self.base + "/x"), token=None)
        self.assertEqual(_Handler.seen, [])

    def test_default_url_points_at_ifwi_repository_ref(self) -> None:
        url = mapping.default_mapping_url("release/v1")
        self.assertTrue(url.startswith(
            "https://api.github.com/repos/intel-innersource/"
            "firmware.boot.uefi.iafw.devops.build.ifwi/contents/oak_release/"
        ))
        self.assertTrue(url.endswith("biosid_oak_ap.xlsx?ref=release%2Fv1"))

    def test_repo_path_and_ref_build_the_url(self) -> None:
        self.assertEqual(
            mapping.mapping_url("org/repo", "/a\\b/biosid_oak_sp.xlsx", " dev "),
            "https://api.github.com/repos/org/repo/contents/a/b/biosid_oak_sp.xlsx?ref=dev",
        )
        source = mapping.github_source("org/repo/", "x/m.xlsx", "v1")
        self.assertEqual((source.kind, source.repo, source.path, source.ref),
                         ("github", "org/repo", "x/m.xlsx", "v1"))

    def test_invalid_repo_path_or_ref_is_rejected(self) -> None:
        for repo, path, ref, option in (
            ("just-a-name", "m.xlsx", "main", "--mapping-repo"),
            ("org/repo/extra", "m.xlsx", "main", "--mapping-repo"),
            ("org/..", "m.xlsx", "main", "--mapping-repo"),
            ("org/repo", "", "main", "--mapping-path"),
            ("org/repo", "a/../m.xlsx", "main", "--mapping-path"),
            ("org/repo", "m.xlsx", " ", "--mapping-ref"),
        ):
            with self.subTest(repo=repo, path=path, ref=ref):
                with self.assertRaisesRegex(ValueError, option):
                    mapping.mapping_url(repo, path, ref)

    def test_local_file_source_records_hash(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "m.xlsx"
            local.write_bytes(make_xlsx(rows([PKG, "MAIN.fd", "Debug", "", "1P0"])))
            components, loaded = mapping.load_mapping(
                mapping.MappingSource("local", str(local)), token=None
            )
        self.assertEqual(len(components), 1)
        self.assertEqual(loaded.kind, "local")
        self.assertEqual(_Handler.seen, [])


if __name__ == "__main__":
    unittest.main()
