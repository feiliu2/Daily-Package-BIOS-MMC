from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from test_cli import CliFixture

from bios_mmc import cli
from bios_mmc.upload import RemoteArchive

BIOS = "0040.D79.7z"
MMC = "MMCFW_0.1022.0.7z"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResultJsonTests(CliFixture):
    """--result-json content for each command and outcome."""

    def setUp(self) -> None:
        super().setUp()
        self.result = self.root / "result" / "run.json"
        self.bios_bytes = b"bios archive"
        self.mmc_bytes = b"mmc archive"

        def make(name: str, data: bytes):
            def create(*args: object, **kwargs: object) -> Path:
                path = self.output / name
                if not kwargs.get("dry_run"):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                return path
            return create

        self.mocks["package_bios"].side_effect = make(BIOS, self.bios_bytes)
        self.mocks["package_mmc_fw"].side_effect = make(MMC, self.mmc_bytes)

        def fake_upload(bios, mmc, **kwargs):
            results = kwargs.get("results")
            for kind, path in (("BIOS", bios), ("MMC", mmc)):
                if path is not None and results is not None:
                    results[kind] = {
                        "url": f"https://up.test/artifactory/REPO/{kind}/{Path(path).name}",
                        "upload": "preview" if kwargs.get("dry_run") else "uploaded",
                    }
            return "preview" if kwargs.get("dry_run") else "completed"

        self.mocks["upload_archives"].side_effect = fake_upload
        self.remote.target.root = "https://up.test/artifactory"
        self.remote.target.repository = "REPO"

    def run_cli(self, *arguments: str) -> tuple[int, dict]:
        code = cli.main([*arguments, "--result-json", str(self.result)])
        return code, json.loads(self.result.read_text(encoding="utf-8"))

    def test_full_flow_without_upload(self) -> None:
        code, data = self.run_cli(*self.common)
        self.assertEqual(code, 0)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(
            (data["command"], data["status"], data["exit_code"], data["error"]),
            ("all", "success", 0, None),
        )
        self.assertEqual((data["bios_id"], data["mmc_version"]), ("0040D79", "0.1022.0"))
        self.assertIsNone(data["upload_target"])
        self.assertEqual(data["bios"], {
            "name": BIOS, "path": str((self.output / BIOS).resolve()),
            "sha256": sha(self.bios_bytes), "size": len(self.bios_bytes),
            "packaging": "packaged", "upload": "not_requested", "skipped": False,
            "remote_url": None,
        })
        self.assertEqual(data["mmc"]["sha256"], sha(self.mmc_bytes))
        self.assertEqual(
            (data["mmc"]["packaging"], data["mmc"]["upload"]), ("packaged", "not_requested")
        )
        self.assertTrue(data["started_at"] and data["finished_at"])

    def test_full_flow_upload_with_published_mmc(self) -> None:
        published = RemoteArchive("MMC", MMC, "https://up.test/artifactory/REPO/MMC/" + MMC,
                                  21, "c" * 64)
        self.published = {MMC: published}
        code, data = self.run_cli(*self.common, "--upload")
        self.assertEqual(code, 0)
        self.assertEqual(data["upload_target"], "https://up.test/artifactory/REPO")
        self.assertEqual(
            (data["bios"]["packaging"], data["bios"]["upload"], data["bios"]["skipped"]),
            ("packaged", "uploaded", False),
        )
        self.assertTrue(data["bios"]["remote_url"].endswith("/BIOS/" + BIOS))
        self.assertEqual(data["mmc"], {
            "name": MMC, "path": None, "sha256": "c" * 64, "size": 21,
            "packaging": "skipped_published", "upload": "skipped_published",
            "skipped": True, "remote_url": published.url,
        })

    def test_both_published(self) -> None:
        bios_meta = RemoteArchive("BIOS", BIOS, "https://up.test/B/" + BIOS, 12, "d" * 64)
        mmc_meta = RemoteArchive("MMC", MMC, "https://up.test/M/" + MMC, 11, "e" * 64)
        self.published = {BIOS: bios_meta, MMC: mmc_meta}
        code, data = self.run_cli(*self.common, "--upload")
        self.assertEqual(code, 0)
        self.assertTrue(data["bios"]["skipped"] and data["mmc"]["skipped"])
        self.assertEqual(data["bios"]["path"], str((self.output / BIOS).resolve()))
        self.assertEqual(data["bios"]["sha256"], "d" * 64)
        self.assertEqual(data["mmc_version"], "0.1022.0")

    def test_dry_run_reports_planned_without_hashes(self) -> None:
        code, data = self.run_cli(*self.common, "--dry-run", "--upload")
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertEqual(
            (data["bios"]["packaging"], data["bios"]["upload"], data["bios"]["sha256"]),
            ("planned", "preview", None),
        )
        self.assertIsNone(data["mmc_version"])
        self.assertEqual(data["mmc"]["packaging"], "not_run")

    def test_failure_is_written_with_error(self) -> None:
        self.mocks["package_bios"].side_effect = ValueError("No BIOS component files\ndetail")
        with self.assertLogs("bios_packager", level="ERROR"):
            code, data = self.run_cli(*self.common)
        self.assertEqual(code, 1)
        self.assertEqual((data["status"], data["exit_code"]), ("failed", 1))
        self.assertEqual(data["error"], "No BIOS component files")
        self.assertEqual(data["bios_id"], "0040D79")
        self.assertEqual(data["bios"]["packaging"], "not_run")

    def test_upload_failure_marks_artifacts(self) -> None:
        def fail_mmc(bios, mmc, **kwargs):
            kwargs["results"]["BIOS"] = {"url": "https://up.test/B/" + BIOS, "upload": "uploaded"}
            kwargs["results"]["MMC"] = {"url": "https://up.test/M/" + MMC, "upload": "preview"}
            raise RuntimeError("MMC upload rejected (HTTP 500)")

        self.mocks["upload_archives"].side_effect = fail_mmc
        with self.assertLogs("bios_packager", level="ERROR"):
            code, data = self.run_cli(*self.common, "--upload")
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "MMC upload rejected (HTTP 500)")
        self.assertEqual((data["bios"]["upload"], data["mmc"]["upload"]), ("uploaded", "failed"))

    def test_bios_subcommand(self) -> None:
        code, data = self.run_cli("bios", *self.common)
        self.assertEqual(code, 0)
        self.assertEqual(data["command"], "bios")
        self.assertEqual(data["bios"]["sha256"], sha(self.bios_bytes))
        self.assertEqual(data["mmc"]["packaging"], "not_run")
        self.assertIsNone(data["mmc_version"])

    def test_mmc_subcommand_records_provided_bios(self) -> None:
        existing = self.root / "0040.D70.7z"
        existing.write_bytes(b"given bios")
        code, data = self.run_cli("mmc", *self.common, "--bios-archive", str(existing))
        self.assertEqual(code, 0)
        self.assertEqual(data["bios_id"], "0040D70")
        self.assertEqual(
            (data["bios"]["packaging"], data["bios"]["sha256"]),
            ("provided", sha(b"given bios")),
        )
        self.assertEqual(data["mmc"]["packaging"], "packaged")

    def test_upload_subcommand(self) -> None:
        bios = self.root / "0040.D82.7z"
        mmc = self.root / "MMCFW_0.1022.0.7z"
        bios.write_bytes(b"b")
        mmc.write_bytes(b"m")
        code, data = self.run_cli(
            "upload", "--bios-archive", str(bios), "--mmc-archive", str(mmc),
            "--log-dir", str(self.root / "logs"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["command"], "upload")
        self.assertEqual(data["upload_target"],
                         "https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL")
        self.assertEqual((data["bios"]["packaging"], data["bios"]["upload"]),
                         ("provided", "uploaded"))
        self.assertEqual(data["mmc"]["sha256"], sha(b"m"))

    def test_no_result_file_without_option_and_no_hashing(self) -> None:
        with mock.patch("bios_mmc.report.sha256_file") as hasher:
            self.assertEqual(cli.main(self.common), 0)
        hasher.assert_not_called()
        self.assertFalse(self.result.exists())

    def test_unwritable_result_path_fails_the_run(self) -> None:
        self.result.mkdir(parents=True)  # a directory cannot be replaced by a file
        with self.assertLogs("bios_packager", level="ERROR") as logged:
            code = cli.main([*self.common, "--result-json", str(self.result)])
        self.assertEqual(code, 1)
        self.assertIn("--result-json", "\n".join(logged.output))
        self.assertEqual(
            [p.name for p in self.result.parent.iterdir() if p.name.startswith(".")], []
        )


if __name__ == "__main__":
    unittest.main()
