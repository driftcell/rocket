"""Test utilities: small JAR builders, a MockTransport backend and common response payloads.

All test data is fictional and distinct from real API examples.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

TEST_USER_AGENT = "rocket-test/0.1 (test@example.invalid)"

FILL_PROJECTS_URL = "https://fill.papermc.io/v3/projects/paper"


# ---------------------------------------------------------------------------
# Small JAR builders
# ---------------------------------------------------------------------------


def make_jar_bytes(
    *,
    manifest: str | None = None,
    plugin_yml: str | None = None,
    stored: bool = False,
) -> bytes:
    """Build a valid in-memory ZIP; manifest/plugin_yml are not written when None."""
    buffer = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        if manifest is not None:
            archive.writestr("META-INF/MANIFEST.MF", manifest)
        if plugin_yml is not None:
            archive.writestr("plugin.yml", plugin_yml)
        archive.writestr("com/example/Noop.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x34")
    return buffer.getvalue()


def paper_manifest(main_class: str = "io.papermc.paper.Main") -> str:
    return f"Manifest-Version: 1.0\r\nMain-Class: {main_class}\r\n\r\n"


def make_paper_jar_bytes(*, main_class: str = "io.papermc.paper.Main") -> bytes:
    return make_jar_bytes(manifest=paper_manifest(main_class))


def make_plugin_jar_bytes(*, name: str = "TestPlugin") -> bytes:
    payload = f"name: {name}\nversion: 1.0.0\nmain: com.example.TestPlugin\n"
    return make_jar_bytes(plugin_yml=payload)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha512_hex(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def corrupt_zip(content: bytes) -> bytes:
    """Flip a known content byte to introduce a CRC error."""
    index = content.index(b"name: TestPlugin")
    data = bytearray(content)
    data[index] ^= 0xFF
    return bytes(data)


# ---------------------------------------------------------------------------
# MockTransport backend
# ---------------------------------------------------------------------------


class ScriptedPrompter:
    """Test double replaying predefined answers and recording every prompt."""

    def __init__(
        self,
        *,
        select: Sequence[str] = (),
        text: str = "",
        confirm: bool = True,
        checkbox: list[str] | None = None,
        abort_on: str | None = None,
    ) -> None:
        self._select = list(select)
        self._text = text
        self._confirm = confirm
        self._checkbox = checkbox
        self._abort_on = abort_on
        self.calls: list[tuple[str, str]] = []
        self.choices: dict[str, list[str]] = {}

    def _abort(self, kind: str) -> None:
        if self._abort_on == kind:
            raise KeyboardInterrupt

    def select(self, message: str, choices: Sequence[tuple[str, str]], *, default: str) -> str:
        self._abort("select")
        self.calls.append(("select", message))
        values = [value for _, value in choices]
        self.choices[message] = values
        assert self._select, f"no scripted select answer left for {message!r}"
        answer = self._select.pop(0)
        assert answer in values, f"select answer {answer!r} not in {values}"
        return answer

    def text(self, message: str, *, default: str = "") -> str:
        self._abort("text")
        self.calls.append(("text", message))
        return self._text

    def confirm(self, message: str, *, default: bool = True) -> bool:
        self._abort("confirm")
        self.calls.append(("confirm", message))
        return self._confirm

    def checkbox(self, message: str, choices: Sequence[tuple[str, str]]) -> list[str]:
        self._abort("checkbox")
        self.calls.append(("checkbox", message))
        values = [value for _, value in choices]
        self.choices[message] = values
        wanted = values if self._checkbox is None else self._checkbox
        assert all(value in values for value in wanted)
        return list(wanted)


class FakeAPI:
    """httpx.MockTransport backend that dispatches by URL and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[str, Callable[[], httpx.Response]] = {}

    def add_json(self, url: str, payload: object, *, status: int = 200) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        self._routes[url] = lambda: httpx.Response(
            status,
            headers={"Content-Type": "application/json"},
            content=text.encode("utf-8"),
        )

    def add_bytes(
        self,
        url: str,
        data: bytes,
        *,
        status: int = 200,
        content_type: str = "application/java-archive",
    ) -> None:
        self._routes[url] = lambda: httpx.Response(
            status, headers={"Content-Type": content_type}, content=data
        )

    def add_html(
        self,
        url: str,
        html: str = "<html><body>download page</body></html>",
        *,
        status: int = 200,
    ) -> None:
        self.add_bytes(
            url, html.encode("utf-8"), status=status, content_type="text/html; charset=utf-8"
        )

    def add_response(self, url: str, factory: Callable[[], httpx.Response]) -> None:
        self._routes[url] = factory

    def add_error(self, url: str, exception: Exception) -> None:
        def raise_error() -> httpx.Response:
            raise exception

        self._routes[url] = raise_error

    def add_sequence(self, url: str, factories: list[Callable[[], httpx.Response]]) -> None:
        queue = list(factories)

        def next_response() -> httpx.Response:
            if not queue:
                raise AssertionError(f"request sequence exhausted: {url}")
            if len(queue) > 1:
                return queue.pop(0)()
            return queue[0]()

        self._routes[url] = next_response

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            factory = self._routes.get(str(request.url))
            if factory is None:
                raise AssertionError(f"unexpected request: {request.method} {request.url}")
            return factory()

        return httpx.MockTransport(handler)

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def count(self, url: str) -> int:
        return self.urls().count(url)


# ---------------------------------------------------------------------------
# URL builders (same quote rules as the implementation; encoding changes are caught by tests)
# ---------------------------------------------------------------------------


def fill_builds_url(version: str) -> str:
    return f"https://fill.papermc.io/v3/projects/paper/versions/{quote(version, safe='')}/builds"


def hangar_project_url(slug: str) -> str:
    return f"https://hangar.papermc.io/api/v1/projects/{quote(slug, safe='')}"


def hangar_version_url(slug: str, version: str) -> str:
    quoted_slug = quote(slug, safe="")
    quoted_version = quote(version, safe="")
    return f"https://hangar.papermc.io/api/v1/projects/{quoted_slug}/versions/{quoted_version}"


def hangar_versions_url(
    slug: str,
    *,
    platform_version: str,
    channel: str = "Release",
    limit: int = 10,
) -> str:
    params = urlencode(
        {
            "platform": "PAPER",
            "platformVersion": platform_version,
            "channel": channel,
            "limit": str(limit),
        }
    )
    quoted_slug = quote(slug, safe="")
    return f"https://hangar.papermc.io/api/v1/projects/{quoted_slug}/versions?{params}"


def github_release_api_url(owner: str, repo: str, tag: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{quote(tag, safe='')}"


def github_releases_url(owner: str, repo: str, *, per_page: int = 30) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/releases?per_page={per_page}"


# ---------------------------------------------------------------------------
# Response payload builders
# ---------------------------------------------------------------------------


def fill_project_payload(groups: dict[str, list[str]]) -> dict[str, object]:
    return {"project": {"id": "paper", "name": "Paper"}, "versions": groups}


def fill_build_payload(
    *,
    build_id: int,
    channel: str,
    url: str,
    name: str,
    sha256: str,
    size: int | None = None,
) -> dict[str, object]:
    download: dict[str, object] = {"name": name, "url": url, "checksums": {"sha256": sha256}}
    if size is not None:
        download["size"] = size
    return {"id": build_id, "channel": channel, "downloads": {"server:default": download}}


def add_paper_routes(api: FakeAPI, *, version: str = "26.1.2", build_id: int = 42) -> bytes:
    """Register a working Paper download chain on a FakeAPI; return the JAR bytes."""
    jar = make_paper_jar_bytes()
    sha = sha256_hex(jar)
    url = f"https://fill-data.papermc.io/v1/objects/{sha}/paper-{version}-{build_id}.jar"
    group = ".".join(version.split(".")[:2])
    api.add_json(FILL_PROJECTS_URL, fill_project_payload({group: [version]}))
    api.add_json(
        fill_builds_url(version),
        [
            fill_build_payload(
                build_id=build_id,
                channel="STABLE",
                url=url,
                name=f"paper-{version}-{build_id}.jar",
                sha256=sha,
                size=len(jar),
            )
        ],
    )
    api.add_bytes(url, jar)
    return jar


def hangar_project_payload(*, owner: str, slug: str) -> dict[str, object]:
    return {"name": slug, "namespace": {"owner": owner, "slug": slug}}


def hangar_version_payload(
    *,
    name: str,
    downloads: dict[str, object],
    platform_dependencies: dict[str, list[str]] | None = None,
    plugin_dependencies: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name, "downloads": downloads}
    if platform_dependencies is not None:
        payload["platformDependencies"] = platform_dependencies
    if plugin_dependencies is not None:
        payload["pluginDependencies"] = plugin_dependencies
    return payload


def hangar_native_paper(
    *,
    download_url: str,
    name: str,
    sha256: str,
    size: int,
) -> dict[str, object]:
    return {
        "PAPER": {
            "downloadUrl": download_url,
            "externalUrl": None,
            "fileInfo": {"name": name, "sizeBytes": size, "sha256Hash": sha256},
        }
    }


def hangar_external_paper(*, external_url: str) -> dict[str, object]:
    return {"PAPER": {"downloadUrl": None, "externalUrl": external_url, "fileInfo": None}}


def hangar_version_list_payload(results: list[dict[str, object]]) -> dict[str, object]:
    return {
        "pagination": {"count": len(results), "limit": len(results), "offset": 0},
        "result": results,
    }


def hangar_version_list_item(*, name: str, channel: str = "Release") -> dict[str, object]:
    return {"name": name, "channel": {"name": channel}}


def github_release_payload(
    *,
    html_url: str,
    tag: str,
    assets: list[dict[str, object]],
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    return {
        "tag_name": tag,
        "html_url": html_url,
        "assets": assets,
        "draft": draft,
        "prerelease": prerelease,
    }


def github_asset_payload(
    *,
    name: str,
    url: str,
    size: int,
    digest: str | None = None,
) -> dict[str, object]:
    return {"name": name, "browser_download_url": url, "size": size, "digest": digest}


# ---------------------------------------------------------------------------
# Modrinth payloads
# ---------------------------------------------------------------------------


def modrinth_project_url(slug: str) -> str:
    return f"https://api.modrinth.com/v2/project/{slug}"


def modrinth_versions_url(slug: str) -> str:
    return f"https://api.modrinth.com/v2/project/{slug}/version"


def hangar_search_url(query: str, *, limit: int = 5) -> str:
    params = urlencode({"query": query, "limit": str(limit), "offset": "0", "sort": "-downloads"})
    return f"https://hangar.papermc.io/api/v1/projects?{params}"


def modrinth_search_url(query: str, *, limit: int = 5) -> str:
    params = urlencode({"query": query, "limit": str(limit), "index": "downloads"})
    return f"https://api.modrinth.com/v2/search?{params}"


def hangar_search_payload(items: list[object]) -> dict[str, object]:
    return {
        "pagination": {"count": len(items), "limit": len(items), "offset": 0},
        "result": items,
    }


def hangar_search_item(
    *, owner: str, slug: str, name: str, description: str = "", downloads: int | None = 1234
) -> dict[str, object]:
    stats: dict[str, object] = {"views": 100, "stars": 1, "watchers": 1}
    if downloads is not None:
        stats["downloads"] = downloads
    return {
        "name": name,
        "namespace": {"owner": owner, "slug": slug},
        "description": description,
        "stats": stats,
    }


def modrinth_search_payload(hits: list[object]) -> dict[str, object]:
    return {"hits": hits, "offset": 0, "limit": len(hits), "total_hits": len(hits)}


def modrinth_search_hit(
    *, slug: str, title: str, description: str = "", downloads: int | None = 4321
) -> dict[str, object]:
    hit: dict[str, object] = {
        "project_id": slug[:8],
        "slug": slug,
        "title": title,
        "description": description,
        "project_type": "mod",
    }
    if downloads is not None:
        hit["downloads"] = downloads
    return hit


def modrinth_project_payload(*, slug: str) -> dict[str, object]:
    return {"slug": slug, "title": slug.title(), "project_type": "mod"}


def modrinth_file_payload(
    *,
    filename: str,
    url: str,
    size: int,
    sha512: str | None = None,
    sha1: str | None = None,
) -> dict[str, object]:
    hashes: dict[str, str] = {}
    if sha512 is not None:
        hashes["sha512"] = sha512
    if sha1 is not None:
        hashes["sha1"] = sha1
    return {"filename": filename, "url": url, "size": size, "hashes": hashes, "primary": True}


def modrinth_version_payload(
    *,
    version_number: str,
    game_versions: list[str],
    loaders: list[str],
    files: list[dict[str, object]],
    version_type: str = "release",
) -> dict[str, object]:
    return {
        "version_number": version_number,
        "game_versions": game_versions,
        "loaders": loaders,
        "version_type": version_type,
        "files": files,
    }


# ---------------------------------------------------------------------------
# Config text
# ---------------------------------------------------------------------------


def base_config(plugin_sections: str = "", *, paper_version: str = "26.1.2") -> str:
    return f"""
schema_version = 1

[paper]
version = "{paper_version}"

[http]
user_agent = "{TEST_USER_AGENT}"

{plugin_sections}
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "server.toml"
    path.write_text(text, encoding="utf-8")
    return path
