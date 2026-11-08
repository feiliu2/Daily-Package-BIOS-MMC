# Daily OakStream BIOS and matching MMC packaging

A standalone, cross-platform (Windows and Linux) Python tool that:

1. Finds the newest `OAKSTRM.0.RPB.<release>.D.<revision>/` build on Artifactory
   (release and revision compared **numerically**), or the one given with `--bios`.
2. Rebuilds the BIOS ID package (`<release>.D<revision>.7z`) natively. This is a
   reimplementation of OakStreamAPIfwi's `restructure_new.py`, driven by the
   `biosid_oak_ap.xlsx` component mapping fetched from the IFWI GitHub repository.
3. Reads the MMC version that BIOS references and packages exactly that MMC build
   as `MMCFW_<version>.7z`.
4. Optionally uploads both archives to Artifactory with `--upload`, using a token from
   `IFWI_ARTIFACTORY_TOKEN`.

No local OakStreamAPIfwi checkout, edkrepo, bundled `7z.exe`, `UnzipManger.exe`,
pandas or openpyxl is required.

For an offline Chinese overview, open [packaging-workflow.html](packaging-workflow.html).

## Requirements

- Python 3.10 or later (standard library only).
- Native 7-Zip on `PATH`:
  - **Windows:** install 7-Zip. `C:\Program Files\7-Zip\7z.exe` is found automatically.
  - **Linux:** `sudo apt install 7zip` (provides `7zz`) or `p7zip-full` (provides `7z`).
  - Or pass `--seven-zip /path/to/7z`.
- Network access to the Intel Artifactory and to `api.github.com`. The standard
  `HTTP(S)_PROXY` / `NO_PROXY` variables are honoured.

## Environment variables

| Variable | When | Purpose |
|---|---|---|
| `IFWI_GITHUB_TOKEN` | BIOS packaging (unless `--mapping-xlsx`) | Read `biosid_oak_ap.xlsx` from `intel-innersource/firmware.boot.uefi.iafw.devops.build.ifwi`. The FIV Ubuntu image (v1.1.3+) already provides it. |
| `IFWI_ARTIFACTORY_TOKEN` | required for `--upload` / `upload`; optional otherwise | One Artifactory token for both servers. Required for `DEG-IFWI-LOCAL` uploads. When set, it is also sent on source downloads; otherwise downloads are anonymous. The FIV Ubuntu image already provides it. |
| `SSL_CERT_FILE` | optional | CA bundle; alternatively `--ca-bundle` |

Tokens are never accepted as command-line arguments and are never logged.

## Entry point and subcommands

`main.py` is the single entry point and dispatches to one step at a time.
`python -m bios_mmc` is equivalent.

| Command | Does | Implementation |
|---|---|---|
| `main.py [options]` | Full flow: BIOS ID, then the matching MMC, then upload if `--upload` | `bios_mmc/cli.py` |
| `main.py bios [options]` | Package only the BIOS ID archive | `bios_mmc/bios.py` |
| `main.py mmc --bios-archive X [options]` | Package only the MMC referenced by an existing BIOS archive (`--upload` uploads that pair) | `bios_mmc/mmc.py` |
| `main.py upload [options]` | Upload an existing BIOS/MMC pair | `bios_mmc/upload.py` |

Each subcommand accepts only its own options. Run `main.py <subcommand> --help` to
see them. Shared options (`--output-dir`, `--seven-zip`, `--dry-run`, `--log-dir`,
`--ca-bundle`) mean the same thing everywhere.

## Package BIOS and MMC

Linux / macOS shell:

```bash
export IFWI_GITHUB_TOKEN=...       # needs read access to the IFWI repository
python3 main.py --dry-run
python3 main.py
```

Windows PowerShell:

```powershell
$env:IFWI_GITHUB_TOKEN = '...'
python .\main.py --dry-run
python .\main.py
```

`--dry-run` fetches and parses the mapping and resolves every component package URL.
It does not download firmware or write archives.

Useful options:

| Option | Default | Meaning |
|---|---|---|
| `--bios 0040D79` | newest | Package a specific BIOS ID (`0040D79`, `0040.D79` or `0040.D.79`) |
| `--output-dir` | `C:\LocalPath\Bios` (Windows), `~/LocalPath/Bios` (Linux) | Final archives |
| `--mapping-ref` | `release/ap.post-silicon` | IFWI repository branch/tag/commit for the mapping. The repository's `main` branch does not contain it. |
| `--mapping-url` | GitHub contents API | Full override URL |
| `--mapping-xlsx` | — | Use a local `biosid_oak_ap.xlsx`; no GitHub access |
| `--jobs` | CPU count | Parallel component downloads/extractions |
| `--download-retry` | 3 | Attempts per component download + extraction |
| `--artifactory-url`, `--mmc-artifactory-url` | project defaults | Source indexes |
| `--seven-zip`, `--ca-bundle`, `--log-dir` | auto / system / `logs` | Tools and diagnostics |
| `--upload` | off | Upload both archives after successful packaging |

Single steps (the full flow above just runs `bios` then `mmc`):

```bash
python3 main.py bios --bios 0040D79
```

To package only MMC for an existing BIOS archive (no mapping or BIOS download):

```bash
python3 main.py mmc --bios-archive ~/LocalPath/Bios/0040.D79.7z
```

### How the BIOS package is built

This step reproduces `restructure_new.py`:

1. **Read the mapping.** It reads the `config` sheet of the mapping, with the columns
   `Original Package`, `Original Binary`, `Original Folder` and `Package file`, and
   groups the rows by package and folder.
2. **Find each package.** In the build directory, it takes the folder named
   `Original Folder`. Inside it, it picks the newest file (by the index timestamp)
   matching the `Original Package` regex.
3. **Download and extract.** Each package is downloaded and extracted, retrying
   truncated or corrupt downloads. If an extracted package contains
   `IFWIStitch20.bin`, that file is unzipped as well.
4. **Copy files.** For each row, it copies every file or directory whose full path
   matches `Original Binary + '$'` (case-insensitive) into `Package file`.
   - Paths are matched with `\` separators on every OS, so the existing regexes
     behave the same on Linux.
   - `Fv*Path.dsc` rows concatenate `<name>*.dsc` siblings, byte-for-byte as the
     original produced them on Windows.
   - `BinaryFvSpr.fdf` gets the S3M path rewritten to `$(FD_BIN_PKG)`.
5. **Report.** A package with any missing file is reported as *not packaged*. Its
   other files are still included, and the archive is still produced, as before.
   Every target folder is pre-created.
6. **Publish.** The tree is archived with 7-Zip, tested, and atomically moved into the
   output directory.

Downloads and extraction run in parallel. Copying runs one package at a time in
spreadsheet order, so packages that share a target folder (such as `Tools\BaseTools`)
are deterministic.

Differences from the original:

- **No fixed work directory.** There is no `C:\ATF`. Staging is a private temporary
  directory inside the output directory, always removed afterwards.
- **Output directory is not wiped.** The original deleted the whole output directory;
  now only the target archive is replaced.
- **Exact folder match first.** An exact folder name is preferred over regex
  matches, which the original resolved by listing order.

## Upload

Uploading is off by default. After successful packaging, add `--upload`. To upload an
existing pair, use the `upload` subcommand:

```bash
export IFWI_ARTIFACTORY_TOKEN=...
python3 main.py upload --dry-run
python3 main.py upload
python3 main.py upload --bios-archive ./0040.D82.7z --mmc-archive ./MMCFW_0.1022.0.7z
```

Without explicit paths, the `upload` subcommand requires exactly one `NNNN.D<rev>.7z`
and one `MMCFW_x.y.z.7z` in `--source-dir`. It never guesses between several.

Archives go to
`https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL/FIV_Portal/IFWI_CI/BIOSID/`
and `.../MMC/` under their original names. The upload runs in these steps:

1. Compute SHA-256 for both files. `--dry-run` stops here and reads no token.
2. Read `IFWI_ARTIFACTORY_TOKEN`. If it is missing or malformed, fail before any request.
3. Preflight both destination folders and both filenames.
   - An identical size and SHA-256 is skipped.
   - A different same-name archive fails without uploading either file.
4. Stream each missing archive with HTTP PUT and `X-Checksum-Sha256`. The upload is
   rejected if the local file changes during transfer.
5. Re-read the server metadata and require the exact repository, path, size and
   SHA-256.

Re-running the upload retries safely; files that are already identical are skipped.

## Logs and exit codes

The console and `logs/package-bios-YYYYMMDD.log` (beside the script) get the same
output. This includes:

- the mapping source and its SHA-256
- each component URL
- the packaged / not packaged summary
- MMC selection
- upload results

The exit code is `0` on success and `1` on any packaging or upload failure. Upload
failures keep the verified local archives.

## Scheduling

Scheduling is outside the scope of this tool. `Register-BiosPackagingTask.ps1` still
registers a Windows task that runs the script daily. Because uploads no longer prompt,
add `--upload` and provide `IFWI_ARTIFACTORY_TOKEN` (and `IFWI_GITHUB_TOKEN`) in the task's
environment if unattended uploads are wanted. On Linux, use cron or a systemd timer
the same way.

## Tests

```bash
python -m unittest discover -s tests -v
```

Integration tests use the native 7-Zip found as described above and a local fake
Artifactory/GitHub HTTP server. The suite passes on Windows and on Ubuntu 24.04.
