"""Offline tests for HTTPDownloader and HTTPClient."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from rocket.downloader import HTTPDownloader
from rocket.errors import (
    DownloadError,
    HTTPStatusError,
    NetworkError,
    RedirectError,
    UnsafeURLError,
    ValidationError,
)
from rocket.http_client import HTTPClient
from rocket.models import Artifact, ArtifactKind, Checksum, Status

from .helpers import (
    FakeAPI,
    corrupt_zip,
    make_jar_bytes,
    make_paper_jar_bytes,
    make_plugin_jar_bytes,
    paper_manifest,
    sha256_hex,
    sha512_hex,
)

URL = "https://cdn.example/demo.jar"


def make_artifact(
    target: Path,
    *,
    url: str = URL,
    kind: ArtifactKind = ArtifactKind.PLUGIN,
    sha256: str | None = None,
    sha512: str | None = None,
    size: int | None = None,
    filename: str | None = None,
) -> Artifact:
    checksum: Checksum | None = None
    if sha512 is not None:
        checksum = Checksum(algorithm="sha512", value=sha512)
    elif sha256 is not None:
        checksum = Checksum(algorithm="sha256", value=sha256)
    return Artifact(
        artifact_id="demo",
        kind=kind,
        provider="test",
        project="Owner/Demo",
        version="1.0.0",
        url=url,
        upstream_filename=filename,
        size=size,
        checksum=checksum,
        source_chain=("test",),
        target=target,
    )


def make_http(
    api: FakeAPI,
    *,
    retries: int = 3,
    sleep=None,
    github_token: str | None = None,
) -> HTTPClient:
    return HTTPClient(
        user_agent="rocket-test/0.1",
        timeout_seconds=5.0,
        retries=retries,
        transport=api.transport(),
        sleep=sleep if sleep is not None else (lambda _: None),
        github_token=github_token,
    )


def download(tmp_path: Path, api: FakeAPI, artifact: Artifact, *, retries: int = 0, sleep=None):
    with make_http(api, retries=retries, sleep=sleep) as http:
        downloader = HTTPDownloader(
            http, retries=retries, sleep=sleep if sleep is not None else (lambda _: None)
        )
        return downloader.fetch(artifact)


def assert_no_temp_files(directory: Path) -> None:
    leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_download_success_atomic(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    result = download(tmp_path, api, make_artifact(target, sha256=sha256_hex(jar), size=len(jar)))

    assert result.status is Status.DOWNLOADED
    assert result.checksum == Checksum(algorithm="sha256", value=sha256_hex(jar))
    assert result.upstream_hash_verified is True
    assert result.size == len(jar)
    assert target.read_bytes() == jar
    assert list(target.parent.iterdir()) == [target]


def test_download_without_upstream_hash_marks_unverified(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    result = download(tmp_path, api, make_artifact(target))

    assert result.status is Status.DOWNLOADED
    assert result.upstream_hash_verified is False
    assert result.upstream_checksum is None
    assert target.read_bytes() == jar


def test_sha512_upstream_hash_verified(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    result = download(tmp_path, api, make_artifact(target, sha512=sha512_hex(jar), size=len(jar)))

    assert result.status is Status.DOWNLOADED
    assert result.checksum == Checksum(algorithm="sha512", value=sha512_hex(jar))
    assert result.upstream_checksum == Checksum(algorithm="sha512", value=sha512_hex(jar))
    assert result.upstream_hash_verified is True
    assert target.read_bytes() == jar


def test_sha512_mismatch_rejected(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(ValidationError) as excinfo:
        download(tmp_path, api, make_artifact(target, sha512="0" * 128))

    assert "SHA-512" in excinfo.value.message
    assert not target.exists()


def test_skip_with_sha512_upstream(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"
    target.parent.mkdir(parents=True)
    target.write_bytes(jar)

    result = download(tmp_path, api, make_artifact(target, sha512=sha512_hex(jar), size=len(jar)))

    assert result.status is Status.SKIPPED
    assert result.upstream_hash_verified is True
    assert api.count(URL) == 0


def test_paper_jar_validation(tmp_path: Path) -> None:
    jar = make_paper_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "paper.jar"

    result = download(
        tmp_path,
        api,
        make_artifact(target, kind=ArtifactKind.PAPER, sha256=sha256_hex(jar), size=len(jar)),
    )

    assert result.status is Status.DOWNLOADED
    assert target.read_bytes() == jar


# ---------------------------------------------------------------------------
# Content validation: keep the existing target and clean up temporary files on failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (b"<html><body>404 not found</body></html>", "text/html"),
        (b'{"error": "no such release"}', "application/json"),
        (b"", "application/octet-stream"),
    ],
)
def test_invalid_payload_rejected(tmp_path: Path, payload: bytes, content_type: str) -> None:
    api = FakeAPI()
    api.add_bytes(URL, payload, content_type=content_type)
    target = tmp_path / "plugins" / "demo.jar"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing-bytes")

    with pytest.raises(ValidationError):
        download(tmp_path, api, make_artifact(target))

    assert target.read_bytes() == b"existing-bytes"
    assert_no_temp_files(target.parent)


def test_hash_mismatch_rejected(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(ValidationError) as excinfo:
        download(tmp_path, api, make_artifact(target, sha256="0" * 64))

    assert "SHA-256 mismatch" in excinfo.value.message
    assert not target.exists()
    assert_no_temp_files(target.parent)


def test_size_mismatch_rejected(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(ValidationError) as excinfo:
        download(tmp_path, api, make_artifact(target, size=len(jar) + 1))

    assert "size mismatch" in excinfo.value.message
    assert not target.exists()


def test_crc_corruption_rejected(tmp_path: Path) -> None:
    jar = make_jar_bytes(plugin_yml="name: TestPlugin\nversion: 1.0.0\n", stored=True)
    api = FakeAPI()
    api.add_bytes(URL, corrupt_zip(jar))
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(ValidationError):
        download(tmp_path, api, make_artifact(target))

    assert not target.exists()


def test_truncated_jar_rejected(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar[: len(jar) // 2])
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(ValidationError):
        download(tmp_path, api, make_artifact(target))

    assert not target.exists()


def test_plugin_jar_without_descriptor_rejected(tmp_path: Path) -> None:
    jar = make_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(ValidationError) as excinfo:
        download(tmp_path, api, make_artifact(target))

    assert "plugin.yml" in excinfo.value.message


def test_paper_jar_without_manifest_rejected(tmp_path: Path) -> None:
    jar = make_jar_bytes(plugin_yml="name: NotPaper\n")
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "paper.jar"

    with pytest.raises(ValidationError) as excinfo:
        download(tmp_path, api, make_artifact(target, kind=ArtifactKind.PAPER))

    assert "MANIFEST.MF" in excinfo.value.message


def test_paper_jar_with_empty_main_class_rejected(tmp_path: Path) -> None:
    jar = make_jar_bytes(manifest=paper_manifest(main_class=""))
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "paper.jar"

    with pytest.raises(ValidationError) as excinfo:
        download(tmp_path, api, make_artifact(target, kind=ArtifactKind.PAPER))

    assert "Main-Class" in excinfo.value.message


# ---------------------------------------------------------------------------
# Re-runs: skip policy
# ---------------------------------------------------------------------------


def test_skip_requires_matching_upstream_hash(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"
    target.parent.mkdir(parents=True)
    target.write_bytes(jar)

    result = download(tmp_path, api, make_artifact(target, sha256=sha256_hex(jar), size=len(jar)))

    assert result.status is Status.SKIPPED
    assert result.upstream_hash_verified is True
    assert api.count(URL) == 0


def test_no_upstream_hash_always_redownloads(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"
    target.parent.mkdir(parents=True)
    target.write_bytes(jar)

    result = download(tmp_path, api, make_artifact(target))

    assert result.status is Status.DOWNLOADED
    assert api.count(URL) == 1


def test_existing_file_with_wrong_hash_redownloads(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    target = tmp_path / "plugins" / "demo.jar"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"stale")

    result = download(tmp_path, api, make_artifact(target, sha256=sha256_hex(jar)))

    assert result.status is Status.DOWNLOADED
    assert target.read_bytes() == jar


def test_existing_file_invalid_jar_with_matching_hash_still_fails(tmp_path: Path) -> None:
    """Hash matches but content is not a valid JAR: delivery is rejected.

    A re-download still fails and keeps the existing file.
    """
    payload = b"not-a-zip"
    api = FakeAPI()
    api.add_bytes(URL, payload)
    target = tmp_path / "plugins" / "demo.jar"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    with pytest.raises(ValidationError):
        download(tmp_path, api, make_artifact(target, sha256=sha256_hex(payload)))

    assert target.read_bytes() == payload
    assert_no_temp_files(target.parent)


# ---------------------------------------------------------------------------
# Network behavior: retries, 429, redirects, schemes
# ---------------------------------------------------------------------------


def test_retry_on_5xx_then_success(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    busy = lambda: httpx.Response(503, content=b"busy")  # noqa: E731
    api.add_sequence(URL, [busy, busy, lambda: httpx.Response(200, content=jar)])
    sleeps: list[float] = []
    target = tmp_path / "plugins" / "demo.jar"

    result = download(tmp_path, api, make_artifact(target), retries=3, sleep=sleeps.append)

    assert result.status is Status.DOWNLOADED
    assert len(sleeps) == 2
    assert sleeps == [0.5, 1.0]


def test_retry_after_header_honoured(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_sequence(
        URL,
        [
            lambda: httpx.Response(429, headers={"Retry-After": "2"}, content=b"slow down"),
            lambda: httpx.Response(200, content=jar),
        ],
    )
    sleeps: list[float] = []
    target = tmp_path / "plugins" / "demo.jar"

    result = download(tmp_path, api, make_artifact(target), retries=3, sleep=sleeps.append)

    assert result.status is Status.DOWNLOADED
    assert sleeps == [2.0]


def test_retries_exhausted_on_5xx(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_sequence(URL, [lambda: httpx.Response(500, content=b"boom")])
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(HTTPStatusError):
        download(tmp_path, api, make_artifact(target), retries=2)

    assert api.count(URL) == 3  # first attempt + 2 retries


def test_timeout_retried_then_fails(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_error(URL, httpx.ConnectTimeout("simulated timeout"))
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(NetworkError):
        download(tmp_path, api, make_artifact(target), retries=1)

    assert api.count(URL) == 2


def test_redirect_loop_rejected(tmp_path: Path) -> None:
    other = "https://cdn.example/other.jar"
    api = FakeAPI()
    api.add_response(URL, lambda: httpx.Response(302, headers={"Location": other}))
    api.add_response(other, lambda: httpx.Response(302, headers={"Location": URL}))
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(RedirectError):
        download(tmp_path, api, make_artifact(target))

    assert len(api.requests) <= 7  # bounded; redirects are not followed forever
    assert not target.exists()


def test_redirect_to_http_rejected(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_response(
        URL, lambda: httpx.Response(302, headers={"Location": "http://cdn.example/demo.jar"})
    )
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(UnsafeURLError):
        download(tmp_path, api, make_artifact(target))

    assert api.count("http://cdn.example/demo.jar") == 0


def test_non_https_url_rejected(tmp_path: Path) -> None:
    api = FakeAPI()
    target = tmp_path / "plugins" / "demo.jar"

    with pytest.raises(UnsafeURLError):
        download(tmp_path, api, make_artifact(target, url="http://cdn.example/demo.jar"))

    assert api.requests == []


def test_midstream_interrupt_restarts_download(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_sequence(
        URL,
        [
            lambda: httpx.Response(200, content=_failing_stream()),
            lambda: httpx.Response(200, content=jar),
        ],
    )
    sleeps: list[float] = []
    target = tmp_path / "plugins" / "demo.jar"

    result = download(tmp_path, api, make_artifact(target), retries=2, sleep=sleeps.append)

    assert result.status is Status.DOWNLOADED
    assert target.read_bytes() == jar
    assert api.count(URL) == 2
    assert sleeps  # backoff wait before the retry


def _failing_stream() -> Iterator[bytes]:
    yield b"partial-bytes"
    raise httpx.ReadTimeout("simulated mid-stream disconnect")


# ---------------------------------------------------------------------------
# Security: auth header is per domain; remote filename does not affect the output path
# ---------------------------------------------------------------------------


def test_github_token_only_sent_to_api_github(tmp_path: Path) -> None:
    api = FakeAPI()
    api.add_json("https://api.github.com/repos/Owner/Demo/releases/tags/v1", {"assets": []})
    api.add_bytes(URL, make_plugin_jar_bytes())
    with make_http(api, github_token="secret-token") as http:
        http.get_json("https://api.github.com/repos/Owner/Demo/releases/tags/v1")
        with http.stream(URL) as response:
            for _chunk in response.iter_bytes():
                pass

    github_request = api.requests[0]
    cdn_request = api.requests[1]
    assert github_request.headers.get("authorization") == "Bearer secret-token"
    assert "authorization" not in cdn_request.headers


def test_remote_filename_with_traversal_confined(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    output_dir = tmp_path / "out"
    target = output_dir / "plugins" / "demo.jar"

    result = download(
        tmp_path,
        api,
        make_artifact(target, sha256=sha256_hex(jar), filename="../../evil.jar"),
    )

    assert result.status is Status.DOWNLOADED
    assert target.read_bytes() == jar
    assert not (output_dir / "evil.jar").exists()
    assert not (tmp_path / "evil.jar").exists()


def test_write_failure_reports_download_error(tmp_path: Path) -> None:
    jar = make_plugin_jar_bytes()
    api = FakeAPI()
    api.add_bytes(URL, jar)
    blocker = tmp_path / "plugins"
    blocker.write_bytes(b"i am a file, not a directory")
    target = blocker / "demo.jar"

    with pytest.raises(DownloadError):
        download(tmp_path, api, make_artifact(target, sha256=sha256_hex(jar)))
