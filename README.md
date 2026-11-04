# Daily OakStream BIOS and matching MMC packaging

The wrapper finds the newest `OAKSTRM.0.RPB.<release>.D.<revision>/`
directory in the Artifactory index and invokes the existing
`restructure_new.py`. Release and revision are compared **numerically**,
not by HTML link order, lexical order or directory modification time.
After BIOS packaging, it reads that archive's MMC firmware version and
downloads/packages the exact matching MMC build, not the newest unrelated version.
After both archives are ready, it asks for explicit Windows desktop confirmation
before offering a one-time token prompt and uploading them to their respective
Artifactory destinations. Declining keeps the verified local archives only.

For an offline Chinese workflow diagram, commands and cleanup/error rules, open
[packaging-workflow.html](packaging-workflow.html) in a browser. It is self-contained
and supports printing or saving as PDF.

For example, `OAKSTRM.0.RPB.0040.D.79/` becomes:

```text
--bios=0040D79 --package_fdr_file=0040.D79.7z
```

## Prerequisites

This repository is **not a standalone BIOS repackager**. It requires a local
`OakStreamAPIfwi` source workspace. **`C:\OKS\OakStreamAPIfwi` below is an example,
not a hardcoded source location.**
The automation does not clone or initialize that workspace during daily runs.

Install/configure `edkrepo` with access to the `OakStreamAPIfwi` project and its
source repositories, then download the code **from the parent directory
`C:\OKS`**:

```powershell
New-Item -ItemType Directory -Force -Path 'C:\OKS' | Out-Null
Set-Location 'C:\OKS'
edkrepo clone OakStreamAPIfwi OakStreamAPIfwi
```

This creates `C:\OKS\OakStreamAPIfwi`. If that workspace already exists, reuse it
and skip cloning; do not overwrite or delete the existing source workspace.

The workspace must contain the original repackager at
`Ifwi\oak_release\BuildScripts\CommonLib\BIOS_Repackaging`, including
`restructure_new.py`, `Config\config_oak_ap.ini`, and the bundled `7-Zip` tools.

Deploy [package_latest_bios.py](package_latest_bios.py) and
[Register-BiosPackagingTask.ps1](Register-BiosPackagingTask.ps1), together with
[Upload-BiosMmc.ps1](Upload-BiosMmc.ps1), into that
`BIOS_Repackaging` directory, beside `restructure_new.py`. If the automation
files are already included in the source branch you cloned, no copying is needed.
**The default tool directory is the automation script's own directory**, not the
current working directory. There is no fallback to another source workspace.

Use Python 3.10 or later with the existing repackager's dependencies
installed. The wrapper itself uses only the standard library. The scheduled
task pins the full Python executable path at registration.
The confirmed upload step additionally requires Windows PowerShell 5.1 and
`curl.exe` on `PATH`. Standalone uploading needs only those Windows tools and
the upload script; it does not require Python or the OakStream source workspace.

## Defaults

- Artifactory: the `Daily/OakStreamRp/main/` index supplied for this project.
- MMC Artifactory: `Daily/ZCodeFW/zcode_dmr_ap_dev/`, using the BIOS-derived
  `MMC_FW.DMR_AP.<version>/DMR_ZCODE_DebugSigned_linux/` directory.
- Existing repackager: the directory containing `package_latest_bios.py`,
  normally `BIOS_Repackaging` alongside `restructure_new.py`.
  Override with `--repackaging-dir` when keeping the automation elsewhere.
- Original configuration: `Config\config_oak_ap.ini` in that directory.
- Final archives: `C:\LocalPath\Bios`.
- Upload server: `https://af01p-sc.devtools.intel.com/artifactory/DEG-IFWI-LOCAL/`.
  BIOS archives go to `FIV_Portal/IFWI_CI/BIOSID/`; MMC archives go to
  `FIV_Portal/IFWI_CI/MMC/`, keeping their original filenames.
- Original workspace: `C:\ATF`, emptied after the BIOS archive is verified.
- Logs: `logs` beside [package_latest_bios.py](package_latest_bios.py).
- Daily schedule: **15:00, local computer time**.

## Preview and run

From the deployed `BIOS_Repackaging` directory containing both Python scripts:

```powershell
python .\package_latest_bios.py --dry-run
python .\package_latest_bios.py
```

The actual run offers upload **only after both packaging steps succeed**.
Choose **No** in the confirmation window to finish without uploading.
For local packaging without any upload window:

```powershell
python .\package_latest_bios.py --no-upload
```

The preview queries the real index and prints the selected BIOS ID and
planned command. It does not run the repackager or create output archives.
MMC selection is deferred until the new BIOS exists; the preview never guesses
its version from an older archive.
Use `--help` for URL, directory, configuration and log overrides.
The `DEFAULT_*` constants in the Python script control the defaults used
by the scheduled task as well as manual runs.

If running from a separate automation checkout, explicitly point to the existing
tools, for example:

```powershell
python .\package_latest_bios.py --repackaging-dir "C:\OKS\OakStreamAPIfwi\Ifwi\oak_release\BuildScripts\CommonLib\BIOS_Repackaging" --dry-run
```

Remove `--dry-run` to package. This override also applies to the MMC-only mode
below; it changes only that invocation and is not saved in the scheduled task.

To package only MMC from an existing BIOS without rebuilding or deleting BIOS:

```powershell
python .\package_latest_bios.py --bios-archive C:\LocalPath\Bios\0040.D79.7z --dry-run
python .\package_latest_bios.py --bios-archive C:\LocalPath\Bios\0040.D79.7z
```

In this mode, the preview reads the existing BIOS and queries its matching MMC
directory, but downloads/extracts nothing and leaves existing archives unchanged.
Use `--mmc-artifactory-url` to override the MMC base URL.
Successful MMC-only runs also offer to upload the explicitly selected BIOS and
its freshly generated MMC archive; `--no-upload` suppresses that offer.

## Upload existing BIOS and MMC archives

The standalone script does not rebuild, rename, delete or recompress archives:

```powershell
.\Upload-BiosMmc.ps1 -WhatIf
.\Upload-BiosMmc.ps1
```

By default, it requires exactly one `<release>.D<revision>.7z` BIOS archive
(four-digit release) and one `MMCFW_<major>.<minor>.<patch>.7z` in
`C:\LocalPath\Bios`. Missing, empty or ambiguous selections fail explicitly;
it never guesses the newest file or uploads every historical archive.
Use `-SourceDirectory 'D:\Firmware'` for another directory, or select both paths:

```powershell
.\Upload-BiosMmc.ps1 -BiosArchive 'C:\LocalPath\Bios\0040.D82.7z' -MmcArchive 'C:\LocalPath\Bios\MMCFW_0.1022.0.7z'
```

Both explicit options must be supplied together. Standalone selection uses
filenames and does not re-decode the BIOS's MMC version; select the matched pair.
The integrated flow passes the exact BIOS and MMC paths returned by packaging,
not another archive discovered later in the output directory.

The sequence is always:

1. Check the two local files, calculate SHA-256 and display their destinations.
2. Show a **Yes/No/Cancel confirmation window, defaulting to No**. Choosing No,
   Cancel or closing it
   requests no token and makes no HTTP requests. `-WhatIf` only previews the
   plan: no confirmation, credentials or network access.
3. Only after Yes, prompt once using the Windows credential dialog. Keep the
   username unchanged and paste the **Artifactory Access Token into Password**.
   Cancelling this dialog also makes no HTTP requests.
4. Preflight both destination folders and both existing filenames before any
   upload. Matching size and SHA-256 means skip; a different same-name archive
   fails without overwriting either destination.
5. Stream each missing archive using native `curl.exe` HTTPS PUT, sending the
   expected SHA-256 header. Re-read server metadata and require the exact
   repository, path, byte size and SHA-256 before reporting it verified.

The token is never accepted as a plaintext command-line option or written to
disk, environment variables or logs. It goes to curl through stdin and is
discarded after the batch. Do not put tokens in scripts or share them in chat.
Each upload has a 30-minute transfer limit; metadata requests have a 60-second
limit. Source files are held read-only against concurrent writes/deletes until
the operation ends.

Declining/cancelling is explicitly reported and returns exit code 0; real upload
or verification errors return nonzero. Failures do not remove local files or
roll back a separately verified remote BIOS if the later MMC upload fails.
Rerun this standalone script to retry only uploading; already identical files
will be skipped. When launched by the Python wrapper, progress and the final
result go to both the console and the daily packaging log. Standalone output
goes to the invoking console.

## Register the daily task

Run registration from the same deployed directory as the three automation files.
The task starts the adjacent automation script, which locates the original tools
relative to itself. If you move the automation after registration, update the
existing task's action to its new location.

Preview the registration without creating a task:

```powershell
.\Register-BiosPackagingTask.ps1 -WhatIf
```

Register it:

```powershell
.\Register-BiosPackagingTask.ps1
```

Use `-At '15:30'` to change the time, or `-PythonPath` to select the
Python installation containing the repackager's dependencies.
Registration does not immediately start packaging. It also does not
overwrite an existing task with the same name.

The task runs as the current user without saving a password, so that user
must remain logged on. The computer must be on and connected to the Intel
network or VPN. Missed runs start when available; failures retry up to
three times at 15-minute intervals. Overlapping scheduled runs are ignored.
Battery operation is allowed, and switching to battery does not stop a run.
The wrapper limits the BIOS child to three hours and each 7-Zip step to
30 minutes; the task has a four-hour overall limit.
The confirmed upload is intentionally **not unattended**: after packaging the
logged-in user must choose Yes and enter a token, otherwise nothing is uploaded.
An unanswered confirmation/token window waits within the task's existing
four-hour limit. Upload errors are nonzero failures, so an existing scheduled
retry repeats packaging and asks for fresh approval; use the standalone upload
script when only the transfer needs retrying. No task is registered or changed
merely by updating these files.

## Execution flow

1. At the scheduled time, read the Artifactory index and select the newest BIOS ID.
2. Run the existing command in the original `BIOS_Repackaging` directory:

   ```text
   python restructure_new.py --bios=0040D79 --package_fdr_file=0040.D79.7z --file_path=C:\LocalPath\Bios --config_ini=config_oak_ap.ini
   ```

3. The original script uses `C:\ATF` for downloads/extracted files and writes the
   archive directly to `C:\LocalPath\Bios`. The original INI is read, not modified;
   no temporary INI or alternate workspace/output directory is created.
4. After the child finishes, repeat its final summary in the console and daily
   log: total component packages, packaged/not-packaged counts, missing package
   names and the original elapsed time. These counts describe the source
   component/build packages, not individual files inside the BIOS archive.
   If the child exited successfully, validate the archive with the bundled
   `7-Zip\7z.exe t`.
5. Only after validation succeeds, delete the contents of `C:\ATF`, leaving the
   directory itself empty.
6. Read all `DEFINE MMC1`/`DEFINE MMC2` references in the BIOS archive's
   `Trunk_*\dsc\FvRawDefPath.dsc` files. Require one consistent `mmc_pkg_<version>_`
   version across all references. This is the build/stitching configuration's
   version, not an independently decoded ROM-internal version field.
7. Query the exact MMC version's `DMR_ZCODE_DebugSigned_linux/` directory and
   download its single `_BuildPkg.7z`. Multiple or missing build packages fail
   explicitly. Loose `.bin` files in the download index are not mixed into
   `binaries`.
8. Extract only the source archive's `binaries` subtree, including its license
   files. Other source/build directories are never extracted. Use direct 7-Zip
   calls with explicit destinations, not the original BIOS unpacking helper.
9. Produce `MMCFW_<version>.7z` with exactly this structure:

   ```text
   MMCFW_0.1022.0\
     binaries\
       LICENSE-3RD-PARTY.txt
       LICENSE-Intel-Agreement.txt
       mmc_pkg_0.1022.0_...bin
       ...
   ```

10. Fully test the MMC archive and require its file paths, sizes and CRCs to
    match the downloaded source's `binaries` subtree before publishing it.
    Remove the uniquely owned MMC staging directory; no downloaded/extracted
    MMC files remain. Staging is on the output volume so replacement is atomic.
11. Unless `--no-upload` is set, launch the adjacent
    [Upload-BiosMmc.ps1](Upload-BiosMmc.ps1) with these exact archive paths.
    Ask for approval before requesting a token or making HTTP requests.
    Upload/verify only after approval; a declined offer leaves both local files.

The final output contains the latest BIOS archive and its corresponding
`MMCFW_<version>.7z`. For example, the current `0040.D79.7z` references
MMC `0.1022.0`, producing `MMCFW_0.1022.0.7z`; it does not select `0.1024.0`
merely because that version also exists.

The original command is executed on every run, even if the latest BIOS ID has
not changed. **The original repackager clears the output directory before writing
the new archive: older BIOS/MMC archives and any other files there are not
preserved.** This latest-pair-only behavior is intentional. The standalone
`--bios-archive` mode does not run this destructive BIOS step.

## Safety and errors

- Both `C:\ATF` and `C:\LocalPath\Bios` must be dedicated to BIOS packaging.
  Do not store other data there or run another manual build concurrently.
- The original configuration must specify `work_space = C:\ATF`. The wrapper
  reports a mismatch instead of rewriting the INI or cleaning another directory.
- Preflight checks reject overlapping, linked/junction or protected workspace
  and output paths, including roots and paths containing the project,
  repackager, configuration, logs, current working directory or user home.
- The workspace path must contain no whitespace or periods because of the
  original repackager's extraction and compression path handling. The normal
  `C:\ATF` path satisfies this requirement.
- Dry runs do not execute the BIOS command or delete anything. If BIOS packaging or
  BIOS archive validation fails, no final workspace cleanup is performed; whatever the original
  script left in the workspace is retained for investigation. The original script
  may already have replaced workspace/output contents before a failure.
- MMC failures return a nonzero exit code without deleting or modifying the BIOS.
  Existing same-version MMC output is replaced only after the new archive passes
  verification. Owned MMC staging files are cleaned on both success and failure;
  unrelated files and other jobs' staging directories are not swept.
- Upload never runs after a failed BIOS/MMC step or during `--dry-run`.
  `--no-upload` preserves the packaging-only behavior. Confirmation or token
  cancellation is logged distinctly from a completed upload, not reported as
  remote success. A zero-exit uploader missing its final result is an error.
- Upload failures preserve the verified local archives and return exit code 1
  separately from packaging failures. No differing same-name remote archive is
  overwritten, and neither local nor remote files are deleted by the uploader.
- Unsafe archive paths, links inside `binaries`, missing/inconsistent MMC
  references, and a source binary version different from the BIOS are rejected.
- Workspace cleanup retries Windows sharing/lock violations up to five times at
  two-second intervals. Persistent locks and other errors are reported as failures.
- HTTP errors, login pages without build directories, missing tools and
  packaging/validation/cleanup failures are logged and return exit code 1.
  The wrapper does not silently select an older build or trust an unverified archive.
- Each run has a separate `restructure-*.log`. The daily `package-bios-*.log`
  contains BIOS/MMC selection, command and result information. Logs are retained outside
  the workspace and BIOS output directory.
- The original BIOS final summary is echoed to both the console and daily log
  before archive verification/cleanup, including on a failed child exit when
  that report is available. Nonzero not-packaged counts are shown as warnings.
  Missing, incomplete or inconsistent reports are explicitly warned about;
  unknown counts are never displayed as zero. The full child log remains
  available for investigation. Reporting does not change the original exit-code,
  archive-verification or cleanup rules.

## Tests

```powershell
python -m unittest discover -s tests -p "test_package*.py" -v
```

The integration tests use generated test data and an existing native 7-Zip:
the adjacent bundle, one on `PATH`, or a standard Windows/Scoop installation.
They copy the tools into owned test fixtures and do not depend on a fixed
OakStream workspace location. If no suitable 7-Zip is installed, native integration
tests explicitly report that they are skipped. They do not download firmware or
execute the real BIOS repackager.
Upload tests use native PowerShell/curl against an isolated loopback HTTP server
and generated archives/synthetic tokens, without a real token, GUI prompts or
Intel uploads. They cover confirmation-before-auth/network ordering, cancellation,
preview, exact routing/content/checksums, idempotency, conflicts before any PUT,
failed authentication/transfers/verification, retry after partial success,
literal paths with spaces/brackets, source locks and console/daily-log routing.
