"""Artifactory directory listings and downloads (read-only)."""

from __future__ import annotations

import re
import shutil
import ssl
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from . import LOGGER

USER_AGENT = "OakStream-BIOS-Packager/2.0"
BUILD_PATTERN = re.compile(
    r"OAKSTRM\.0\.RPB\.(?P<release>\d{4})\.D\.(?P<revision>\d+)"
)
BIOS_ID_PATTERN = re.compile(r"(?P<release>\d{4})\.?D\.?(?P<revision>\d+)", re.I)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_LISTING_LINE = re.compile(
    r'<a\s[^>]*?href\s*=\s*"(?P<href>[^"]+)"[^>]*>.*?</a>\s*'
    r"(?P<day>\d{1,2})-(?P<month>[A-Za-z]{3})-(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})",
    re.I,
)


@dataclass(frozen=True)
class BiosBuild:
    release: str
    revision: str

    @property
    def bios_id(self) -> str:
        return f"{self.release}D{self.revision}"

    @property
    def package_name(self) -> str:
        return f"{self.release}.D{self.revision}.7z"

    @property
    def directory_name(self) -> str:
        return f"OAKSTRM.0.RPB.{self.release}.D.{self.revision}"


def parse_bios_id(value: str) -> BiosBuild:
    """Accept 0040D79, 0040.D79, 0040.D.79 or the full OAKSTRM directory name."""
    match = BUILD_PATTERN.search(value) or BIOS_ID_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Unrecognized BIOS ID: {value!r} (expected e.g. 0040D79)")
    return BiosBuild(match["release"], str(int(match["revision"])))


@dataclass
class HttpClient:
    """Small urllib wrapper with an optional bearer token and CA bundle."""

    token: str | None = None
    ca_bundle: Path | None = None
    timeout: int = 60

    def ssl_context(self) -> ssl.SSLContext | None:
        if self.ca_bundle is None:
            return None
        return ssl.create_default_context(cafile=str(self.ca_bundle))

    def request(self, url: str, headers: dict[str, str] | None = None) -> Request:
        merged = {"User-Agent": USER_AGENT, **(headers or {})}
        if self.token:
            merged["Authorization"] = f"Bearer {self.token}"
        return Request(url, headers=merged)

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        with urlopen(
            self.request(url, headers), timeout=self.timeout, context=self.ssl_context()
        ) as response:
            return response.read()

    def get_text(self, url: str) -> str:
        with urlopen(
            self.request(url), timeout=self.timeout, context=self.ssl_context()
        ) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset)

    def download(self, url: str, destination: Path) -> None:
        LOGGER.info("Downloading: %s", url)
        with urlopen(
            self.request(url), timeout=self.timeout, context=self.ssl_context()
        ) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
            length = response.headers.get("Content-Length")
            if length is not None and output.tell() != int(length):
                raise ValueError(f"Incomplete download: {url}")
        if destination.stat().st_size == 0:
            raise ValueError(f"Empty download: {url}")


class DirectoryIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def index_links(html: str) -> list[str]:
    parser = DirectoryIndexParser()
    parser.feed(html)
    parser.close()
    return parser.links


def _link_name(href: str) -> str:
    return unquote(urlsplit(href).path).rstrip("/").rsplit("/", 1)[-1]


def list_builds(html: str) -> list[BiosBuild]:
    builds: list[BiosBuild] = []
    for href in index_links(html):
        if not unquote(urlsplit(href).path).endswith("/"):
            continue
        match = BUILD_PATTERN.fullmatch(_link_name(href))
        if match:
            builds.append(BiosBuild(match["release"], match["revision"]))
    return builds


def parse_latest_build(html: str) -> BiosBuild:
    builds = list_builds(html)
    if not builds:
        raise ValueError(
            "No OAKSTRM.0.RPB.<release>.D.<revision>/ directories found. "
            "Check the Artifactory URL, Intel network access and permissions."
        )
    return max(builds, key=lambda build: (int(build.release), int(build.revision)))


def select_build(html: str, requested: BiosBuild | None) -> BiosBuild:
    if requested is None:
        return parse_latest_build(html)
    for build in list_builds(html):
        if (int(build.release), int(build.revision)) == (
            int(requested.release), int(requested.revision)
        ):
            return build
    raise ValueError(f"BIOS {requested.directory_name}/ is not in the Artifactory index.")


def select_subdirectory(html: str, directory_url: str, name: str) -> str | None:
    """Exact (case-insensitive) folder match, else the original's first regex match."""
    folders = [
        href for href in index_links(html)
        if unquote(urlsplit(href).path).endswith("/") and _link_name(href) != ".."
    ]
    for href in folders:
        if _link_name(href).lower() == name.lower():
            return urljoin(directory_url, href)
    for href in folders:
        if re.search(name, _link_name(href), re.I | re.M):
            return urljoin(directory_url, href)
    return None


def select_latest_file(html: str, directory_url: str, pattern: str) -> str | None:
    """Newest listed file whose name matches pattern, by the index's modified time."""
    candidates: list[tuple[datetime, str]] = []
    for match in _LISTING_LINE.finditer(html):
        href = unescape(match["href"])
        name = _link_name(href)
        if href.endswith("/") or not re.search(pattern, name, re.I | re.M):
            continue
        month = _MONTHS.get(match["month"].lower())
        if month is None:
            continue
        stamp = datetime(
            int(match["year"]), month, int(match["day"]),
            int(match["hour"]), int(match["minute"]),
        )
        candidates.append((stamp, urljoin(directory_url, href)))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def select_mmc_source(html: str, directory_url: str) -> str:
    base = urlsplit(directory_url)
    base_path = unquote(base.path).rstrip("/") + "/"
    candidates: set[str] = set()
    for href in index_links(html):
        url = urljoin(directory_url, href)
        target = urlsplit(url)
        path = unquote(target.path)
        if (
            (target.scheme, target.netloc) != (base.scheme, base.netloc)
            or not path.startswith(base_path)
        ):
            continue
        filename = path[len(base_path):]
        if (
            "/" not in filename and "\\" not in filename
            and filename.endswith("_BuildPkg.7z")
        ):
            candidates.add(url)
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one MMC _BuildPkg.7z at {directory_url}; "
            f"found {len(candidates)}: {sorted(candidates)}"
        )
    return candidates.pop()
