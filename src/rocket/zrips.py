"""zrips.net download pages (Zrips plugins: Residence, CMILib, ...) and version selection.

zrips.net has no API: every plugin ships its builds on one fixed page, addressed by the
case-sensitive directory name (``https://www.zrips.net/Residence/``). Two page layouts
exist in practice and both are parsed:

- a PHP file manager (Residence) linking ``download.php?file=<name>.jar``;
- a WordPress page (CMILib) linking JARs directly by path.

The pinned download URL is always the href published on the page (never a guessed file
path); the exact version is the middle of the upstream filename (``<Project><version>.jar``).
zrips.net provides no file hashes and no Minecraft compatibility info, so downloads are
verified structurally by the downloader and compatibility is reported as unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from .errors import HTTPStatusError, ProviderError
from .http_client import HTTPClient

_PAGE_BASE = "https://www.zrips.net"
_ALLOWED_HOSTS = {"zrips.net", "www.zrips.net"}
_PROJECT_RE = re.compile(r"[A-Za-z0-9_-]+")
_ANCHOR_RE = re.compile(
    r'<a\s+[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_FILE_PARAM_RE = re.compile(r"[?&]file=(?P<file>[^&\"']+)")
_DATE_RE = re.compile(r"\[(?P<date>\d{1,2}/\d{1,2}/\d{4})")


@dataclass(frozen=True)
class ZripsFile:
    """One listed build: exact version, upstream filename, published URL and list detail."""

    version: str
    filename: str
    url: str
    detail: str


def parse_project(project: str, *, field_path: str) -> str:
    """Validate a zrips.net project coordinate (the case-sensitive directory name)."""
    candidate = project.strip()
    if not _PROJECT_RE.fullmatch(candidate):
        raise ProviderError(
            f"{field_path} is not a valid zrips.net project: {project!r}",
            hint="use the case-sensitive directory name from the download page URL, "
            "e.g. zrips:Residence",
        )
    return candidate


def listing_url(project: str) -> str:
    """The download page of a project (redirects, e.g. to the WordPress page, are followed)."""
    return f"{_PAGE_BASE}/{project}/"


def fetch_listing(http: HTTPClient, project: str) -> tuple[ZripsFile, ...]:
    """Fetch and parse the download page of a project, newest entry first."""
    url = listing_url(project)
    try:
        html = http.get_text(url)
    except HTTPStatusError as exc:
        if exc.status_code == 404:
            raise ProviderError(
                f"zrips.net project not found: {project}",
                hint="the project is the case-sensitive directory name of the download page, "
                "e.g. zrips:Residence; no guessing by name",
            ) from exc
        raise
    files = parse_listing(html, project=project, page_url=url)
    if not files:
        raise ProviderError(
            f"zrips.net project {project} has no downloadable versions",
            hint=f"no {project}*.jar links were found on {url}; check the project "
            "coordinate (case-sensitive) — the page layout may also have changed",
        )
    return files


def parse_listing(html: str, *, project: str, page_url: str) -> tuple[ZripsFile, ...]:
    """All ``<Project>*.jar`` links of a download page, in page order (newest first).

    Duplicated versions keep their first occurrence; links pointing off zrips.net are
    ignored rather than followed.
    """
    files: list[ZripsFile] = []
    seen: set[str] = set()
    for match in _ANCHOR_RE.finditer(html):
        filename = _href_filename(unescape(match.group("href")))
        if filename is None or not filename.startswith(project):
            continue
        version = filename[len(project) : -len(".jar")]
        if not version or version in seen:
            continue
        url = urljoin(page_url, unescape(match.group("href")))
        if (urlparse(url).hostname or "").lower() not in _ALLOWED_HOSTS:
            continue
        seen.add(version)
        files.append(
            ZripsFile(
                version=version,
                filename=filename,
                url=url,
                detail=_entry_detail(match.group("text"), filename),
            )
        )
    return tuple(files)


def select_file(files: tuple[ZripsFile, ...], *, version: str, project: str) -> ZripsFile:
    """Exact version match against the listing; no latest resolution, no ranges."""
    for file in files:
        if file.version == version:
            return file
    available = ", ".join(file.version for file in files[:8]) or "(none)"
    raise ProviderError(
        f"zrips.net project {project} has no version {version!r}",
        hint=f"recent available versions: {available}; latest and version ranges are not supported",
    )


def _href_filename(href: str) -> str | None:
    """The JAR filename an anchor serves, from a ``download.php?file=`` or a direct link."""
    file_param = _FILE_PARAM_RE.search(href)
    if file_param is not None:
        candidate = unquote(file_param.group("file"))
    else:
        path = urlparse(href).path
        if not path.lower().endswith(".jar"):
            return None
        candidate = unquote(path.rsplit("/", 1)[-1])
    if not candidate.lower().endswith(".jar") or "/" in candidate or "\\" in candidate:
        return None
    return candidate


def _entry_detail(text: str, filename: str) -> str:
    """Pin-catalog detail: the upload date (file manager) or the trailing note (WordPress)."""
    plain = " ".join(_TAG_RE.sub(" ", text).split())
    date = _DATE_RE.search(plain)
    if date is not None:
        return date.group("date")
    if plain.startswith(filename):
        return plain[len(filename) :].strip()
    return ""
