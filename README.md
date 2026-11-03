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
- Legacy disposable cache: `C:\ATF`, removed after a verified archive is saved.
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

## Safety and errors

- The original repackager **deletes its configured workspace and output
  directory**. The wrapper instead gives it unique temporary directories
  and a temporary INI with an isolated `work_space`. The original INI,
  configured workspace (unless it is the disposable `C:\ATF` cache) and
  previous archives are not modified.
- After the archive is verified and saved outside the cache, `C:\ATF`
  and all its contents are deleted. This directory must be dedicated to
  disposable BIOS downloads/extracted files; do not store other data there
  or use it for another build while the task runs. The final output directory
  retains only archives, not extracted build files; diagnostic logs remain
  in the separate log directory.
- A valid existing same-name archive also triggers legacy-cache cleanup.
  Dry runs, packaging failures and archive-validation failures leave that
  cache untouched. Cleanup errors return a failure and retain the saved
  archive, allowing the scheduled retry to attempt cleanup without repackaging.
- Cleanup refuses linked/junction cache roots and paths containing the
  archive, repackager, logs, project, current working directory or user home.
- The original working directory is retained for relative Excel/tool paths.
- A new archive is published only after the child exits successfully and
  the bundled `7-Zip\7z.exe t` validates its complete contents.
- An existing same-name archive is validated and skipped. A damaged
  existing archive produces an error; it is not silently overwritten.
- Output paths containing whitespace are rejected because the existing
  repackager does not quote its compression workspace.
- Failed or unfinished jobs do not publish partial archives. Their
  isolated temporary directory is removed; diagnostic logs are retained.
- HTTP errors, login pages without build directories, missing tools and
  packaging/validation failures are logged and return exit code 1. The
  script does not silently select an older build when the newest fails.
- Each packaging run has a separate `restructure-*.log`. The daily
  `package-bios-*.log` contains selection, command and result information.
- No old archive or log retention cleanup is performed.

## Tests

```powershell
python -m unittest discover -s tests -p test_package_latest_bios.py -v
```
