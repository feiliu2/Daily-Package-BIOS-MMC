# Daily OakStream BIOS packaging

The wrapper finds the newest `OAKSTRM.0.RPB.<release>.D.<revision>/`
directory in the Artifactory index and invokes the existing
`restructure_new.py`. Release and revision are compared **numerically**,
not by HTML link order, lexical order or directory modification time.

For example, `OAKSTRM.0.RPB.0040.D.79/` becomes:

```text
--bios=0040D79 --package_fdr_file=0040.D79.7z
```

## Defaults

- Artifactory: the `Daily/OakStreamRp/main/` index supplied for this project.
- Existing repackager:
  `C:\OKS\OakStreamAPIfwi\Ifwi\oak_release\BuildScripts\CommonLib\BIOS_Repackaging`
- Original configuration: `Config\config_oak_ap.ini` in that directory.
- Final archives: `C:\LocalPath\Bios`.
- Original workspace: `C:\ATF`, emptied after the final archive is verified.
- Logs: `logs` beside [package_latest_bios.py](package_latest_bios.py).
- Daily schedule: **15:00, local computer time**.

Use Python 3.10 or later with the existing repackager's dependencies
installed. The wrapper itself uses only the standard library. The scheduled
task pins the full Python executable path at registration.

## Preview and run

From this directory:

```powershell
python .\package_latest_bios.py --dry-run
python .\package_latest_bios.py
```

The preview queries the real index and prints the selected BIOS ID and
planned command. It does not run the repackager or create output archives.
Use `--help` for URL, directory, configuration and log overrides.
The `DEFAULT_*` constants in the Python script control the defaults used
by the scheduled task as well as manual runs.

## Register the daily task

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
The wrapper limits packaging to three hours and archive validation to
30 minutes; the task has a four-hour overall limit.

## Execution flow

1. At the scheduled time, read the Artifactory index and select the newest BIOS ID.
2. Run the existing command in the original `BIOS_Repackaging` directory:

   ```text
   python restructure_new.py --bios=0040D79 --package_fdr_file=0040.D79.7z --file_path=C:\LocalPath\Bios --config_ini=config_oak_ap.ini
   ```

3. The original script uses `C:\ATF` for downloads/extracted files and writes the
   archive directly to `C:\LocalPath\Bios`. The original INI is read, not modified;
   no temporary INI or alternate workspace/output directory is created.
4. After the command exits successfully, validate the archive with the bundled
   `7-Zip\7z.exe t`.
5. Only after validation succeeds, delete the contents of `C:\ATF`, leaving the
   directory itself empty. The output directory retains only the latest archive.

The original command is executed on every run, even if the latest BIOS ID has
not changed. **The original repackager clears the output directory before writing
the new archive: older archives and any other files there are not preserved.**
This latest-only behavior is intentional.

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
- Dry runs do not execute the command or delete anything. If packaging or archive
  validation fails, no final workspace cleanup is performed; whatever the original
  script left in the workspace is retained for investigation. The original script
  may already have replaced workspace/output contents before a failure.
- Workspace cleanup retries Windows sharing/lock violations up to five times at
  two-second intervals. Persistent locks and other errors are reported as failures.
- HTTP errors, login pages without build directories, missing tools and
  packaging/validation/cleanup failures are logged and return exit code 1.
  The wrapper does not silently select an older build or trust an unverified archive.
- Each run has a separate `restructure-*.log`. The daily `package-bios-*.log`
  contains selection, command and result information. Logs are retained outside
  the workspace and BIOS output directory.

## Tests

```powershell
python -m unittest discover -s tests -p test_package_latest_bios.py -v
```
