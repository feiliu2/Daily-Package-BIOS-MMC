---
applyTo: "bios_mmc/**/*.py,tests/**/*.py"
---

# Packaging concurrency

- `cli.add_packaging()` defines options shared by the full flow and the `bios`/`mmc` subcommands, not `upload`; validate shared packaging options in `prepare()` before network access.
- `--jobs` limits the BIOS component download/extraction thread pool, not 7-Zip internal threads. Component copies remain ordered to avoid shared-target races.
- `--archive-threads N` reaches `package_bios()` -> `fsutil.create_archive()` and `package_mmc_fw()` -> its separate 7-Zip creation command. Both must pass `-mmt=N` before `--`; omitted means no switch (7-Zip default). This option only controls final compression, not extraction or validation.
- MMC creates an archive containing the `MMCFW_<version>` root folder, unlike BIOS which archives the staging directory's children. Preserve those layouts when changing shared archive helpers.