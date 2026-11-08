"""BIOS component mapping (biosid_oak_ap.xlsx) fetched from the IFWI repository."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from xml.etree import ElementTree

from . import LOGGER
from .artifactory import HttpClient

DEFAULT_MAPPING_REPO = "intel-innersource/firmware.boot.uefi.iafw.devops.build.ifwi"
DEFAULT_MAPPING_PATH = (
    "oak_release/BuildScripts/CommonLib/BIOS_Repackaging/biosid_oak_ap.xlsx"
)
DEFAULT_MAPPING_REF = "release/ap.post-silicon"
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN_ENV = "IFWI_GITHUB_TOKEN"
SHEET_NAME = "config"
REQUIRED_COLUMNS = (
    "Original Package", "Original Binary", "Original Folder", "Package file",
)

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


@dataclass(frozen=True)
class MappingRow:
    binary: str
    target: str


@dataclass
class ComponentPackage:
    """One downloadable build package and the files copied out of it."""

    package_pattern: str
    folder: str
    excel_line: int
    rows: list[MappingRow] = field(default_factory=list)


def default_mapping_url(ref: str = DEFAULT_MAPPING_REF) -> str:
    return (
        f"{GITHUB_API}/repos/{DEFAULT_MAPPING_REPO}/contents/"
        f"{quote(DEFAULT_MAPPING_PATH)}?ref={quote(ref, safe='')}"
    )


def fetch_mapping(url: str, token: str | None, http: HttpClient | None = None) -> bytes:
    if not token:
        raise ValueError(
            f"{GITHUB_TOKEN_ENV} is not set; it is required to read the BIOS mapping "
            "from GitHub. Alternatively pass --mapping-xlsx <local file>."
        )
    client = HttpClient(
        token=token,
        ca_bundle=http.ca_bundle if http else None,
        timeout=http.timeout if http else 60,
    )
    try:
        data = client.get_bytes(
            url,
            headers={
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except HTTPError as error:
        raise ValueError(
            f"Cannot download BIOS mapping (HTTP {error.code}): {url}"
        ) from error
    return data


def load_mapping(
    *, local_path: Path | None, url: str, token: str | None,
    http: HttpClient | None = None,
) -> list[ComponentPackage]:
    if local_path is not None:
        data = local_path.read_bytes()
        source = str(local_path)
    else:
        data = fetch_mapping(url, token, http)
        source = url
    components = parse_mapping(data)
    LOGGER.info(
        "BIOS mapping: %s (SHA-256 %s): %s component packages, %s file rows.",
        source, hashlib.sha256(data).hexdigest(), len(components),
        sum(len(component.rows) for component in components),
    )
    return components


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        raise ValueError(f"Invalid cell reference: {reference}")
    index = 0
    for character in letters.group():
        index = index * 26 + ord(character) - ord("A") + 1
    return index - 1


def read_sheet(data: bytes, sheet_name: str = SHEET_NAME) -> list[list[str]]:
    """Read one worksheet as strings using only the standard library."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError("BIOS mapping is not a valid .xlsx file.") from error
    with archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("BIOS mapping is not a valid .xlsx file (no workbook).")
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationship_id = None
        for sheet in workbook.iter(f"{_MAIN}sheet"):
            if sheet.get("name") == sheet_name:
                relationship_id = sheet.get(f"{_REL}id")
        if relationship_id is None:
            raise ValueError(f"BIOS mapping has no '{sheet_name}' sheet.")
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        target = next(
            (rel.get("Target") for rel in relationships.iter(f"{_PKG_REL}Relationship")
             if rel.get("Id") == relationship_id),
            None,
        )
        if not target:
            raise ValueError(f"Cannot resolve the '{sheet_name}' worksheet.")
        sheet_path = (
            target.lstrip("/") if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for item in ElementTree.fromstring(archive.read("xl/sharedStrings.xml")):
                shared.append("".join(text.text or "" for text in item.iter(f"{_MAIN}t")))
        worksheet = ElementTree.fromstring(archive.read(sheet_path))

    rows: list[list[str]] = []
    for row in worksheet.iter(f"{_MAIN}row"):
        values: dict[int, str] = {}
        for position, cell in enumerate(row.iter(f"{_MAIN}c")):
            reference = cell.get("r")
            column = _column_index(reference) if reference else position
            kind = cell.get("t")
            value = cell.find(f"{_MAIN}v")
            if kind == "inlineStr":
                text = "".join(node.text or "" for node in cell.iter(f"{_MAIN}t"))
            elif value is None or value.text is None:
                text = ""
            elif kind == "s":
                text = shared[int(value.text)]
            else:
                text = value.text
            values[column] = text
        width = max(values, default=-1) + 1
        rows.append([values.get(index, "") for index in range(width)])
    return rows


def parse_mapping(data: bytes) -> list[ComponentPackage]:
    rows = read_sheet(data)
    if not rows:
        raise ValueError("BIOS mapping sheet is empty.")
    header = [value.strip() for value in rows[0]]
    try:
        columns = {name: header.index(name) for name in REQUIRED_COLUMNS}
    except ValueError as error:
        raise ValueError(
            f"BIOS mapping is missing a required column {REQUIRED_COLUMNS}: {header}"
        ) from error

    components: dict[tuple[str, str], ComponentPackage] = {}
    for line, row in enumerate(rows[1:], start=2):
        values = {
            name: (row[index] if index < len(row) else "")
            for name, index in columns.items()
        }
        if not any(value.strip() for value in values.values()):
            continue
        empty = [name for name, value in values.items() if not value.strip()]
        if empty:
            raise ValueError(f"BIOS mapping line {line} has empty {empty}.")
        key = (values["Original Package"], values["Original Folder"])
        component = components.get(key)
        if component is None:
            component = components[key] = ComponentPackage(
                package_pattern=key[0].strip(),
                folder=key[1].strip(),
                excel_line=line,
            )
        component.rows.append(
            MappingRow(values["Original Binary"], values["Package file"])
        )
    if not components:
        raise ValueError("BIOS mapping contains no component rows.")
    return list(components.values())
