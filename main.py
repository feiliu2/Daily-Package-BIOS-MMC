"""Daily OakStream BIOS ID + matching MMC packaging (Windows/Linux).

    python main.py [options]            full flow: BIOS ID -> matching MMC (-> upload)
    python main.py bios [options]       package only the BIOS ID archive
    python main.py mmc [options]        package only the MMC matching a BIOS archive
    python main.py upload [options]     upload an existing BIOS/MMC pair

Run any of them with --help for options. Implementation lives in bios_mmc/.
"""

from bios_mmc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
