"""End-to-end offline tests for the CLI: exit codes, output layout and failure isolation."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx

from rocket import __version__
from rocket.cli import _format_count, _format_duration, _format_size, main

from .helpers import (
    FakeAPI,
    add_paper_routes,
    base_config,
    fill_build_payload,
    fill_builds_url,
    hangar_native_paper,
    hangar_project_payload,
    hangar_project_url,
    hangar_version_payload,
    hangar_version_url,
    make_paper_jar_bytes,
    make_plugin_jar_bytes,
    sha256_hex,
    write_config,
)

GOOD_DOWNLOAD_URL = (
    "https://hangarcdn.papermc.io/plugins/Owner/Good/versions/1.0.0/PAPER/Good-1.0.0.jar"
)

TWO_PLUGINS = """
[plugins.good]
provider = "hangar"
project = "Owner/Good"
version = "1.0.0"

[plugins.bad]
provider = "hangar"
project = "Owner/Bad"
version = "9.9.9"
"""


def add_good_plugin_routes(api: FakeAPI) -> bytes:
    jar = make_plugin_jar_bytes(name="Good")
    api.add_json(hangar_project_url("Good"), hangar_project_payload(owner="Owner", slug="Good"))
    api.add_json(
        hangar_version_url("Good", "1.0.0"),
        hangar_version_payload(
            name="1.0.0",
            downloads=hangar_native_paper(
                download_url=GOOD_DOWNLOAD_URL,
                name="Good-1.0.0.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    api.add_bytes(GOOD_DOWNLOAD_URL, jar)
    return jar


def run_main(tmp_path: Path, api: FakeAPI, text: str, *, argv: list[str] | None = None) -> int:
    config_path = write_config(tmp_path, text)
    args = argv if argv is not None else ["download", "--config", str(config_path)]
    return main(args, transport=api.transport(), sleep=lambda _: None)


def assert_no_unexpected_state_files(root: Path) -> None:
    for path in root.rglob("*"):
        if path.name == "rocket.lock":
            continue  # expected: the lock file written by download
        assert not path.name.startswith("server.lock"), f"lock file found: {path}"
        assert path.name not in {"server.sync", ".rocket-state", "server.lock.json"}


def test_invalid_config_exits_2_before_network(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    text = base_config().replace('version = "26.1.2"', "version = 26.1")

    code = run_main(tmp_path, api, text)

    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "paper.version" in err
    assert api.requests == []


def test_success_layout_and_exit_codes(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    paper_jar = add_paper_routes(api)
    plugin_jar = add_good_plugin_routes(api)

    code = run_main(
        tmp_path,
        api,
        base_config(
            '[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\nversion = "1.0.0"\n'
        ),
    )

    assert code == 0
    artifacts = tmp_path / "artifacts"
    assert (artifacts / "paper.jar").read_bytes() == paper_jar
    assert (artifacts / "plugins" / "good.jar").read_bytes() == plugin_jar
    captured = capsys.readouterr()
    out = captured.out
    assert "Resolved 2 items" in out
    assert "Downloaded 2 items" in out
    assert " + paper.jar (26.1.2 build 42, fill)" in out
    assert " + good.jar (1.0.0, hangar)" in out
    assert "[paper]" not in captured.err
    assert_no_unexpected_state_files(tmp_path)


def test_one_plugin_fails_others_succeed(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    paper_jar = add_paper_routes(api)
    plugin_jar = add_good_plugin_routes(api)
    api.add_json(hangar_project_url("Bad"), hangar_project_payload(owner="Owner", slug="Bad"))
    api.add_json(hangar_version_url("Bad", "9.9.9"), {"ok": False}, status=404)

    code = run_main(tmp_path, api, base_config(TWO_PLUGINS))

    assert code == 1
    artifacts = tmp_path / "artifacts"
    assert (artifacts / "paper.jar").read_bytes() == paper_jar
    assert (artifacts / "plugins" / "good.jar").read_bytes() == plugin_jar
    assert not (artifacts / "plugins" / "bad.jar").exists()
    captured = capsys.readouterr()
    assert "Resolved 2 items" in captured.out
    assert "Downloaded 2 items" in captured.out
    assert "error: bad:" in captured.err
    assert_no_unexpected_state_files(tmp_path)


def add_hangar_plugin_routes(
    api: FakeAPI, slug: str, jar: bytes, *, event: threading.Event | None = None
) -> str:
    url = f"https://hangarcdn.papermc.io/plugins/Owner/{slug}/versions/1.0.0/PAPER/{slug}-1.0.0.jar"
    api.add_json(hangar_project_url(slug), hangar_project_payload(owner="Owner", slug=slug))
    api.add_json(
        hangar_version_url(slug, "1.0.0"),
        hangar_version_payload(
            name="1.0.0",
            downloads=hangar_native_paper(
                download_url=url,
                name=f"{slug}-1.0.0.jar",
                sha256=sha256_hex(jar),
                size=len(jar),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    if event is None:
        api.add_bytes(url, jar)
    else:

        def gated_response() -> httpx.Response:
            # hold this download back until the other one has started
            assert event.wait(timeout=10)
            return httpx.Response(
                200, headers={"Content-Type": "application/java-archive"}, content=jar
            )

        api.add_response(url, gated_response)
    return url


def test_parallel_downloads_print_in_config_order(tmp_path: Path, capsys) -> None:
    """The slow first item finishes after the fast second one; output stays in config order."""
    api = FakeAPI()
    add_paper_routes(api)
    slow_jar = make_plugin_jar_bytes(name="Slow")
    fast_jar = make_plugin_jar_bytes(name="Fast")
    fast_started = threading.Event()
    add_hangar_plugin_routes(api, "Slow", slow_jar, event=fast_started)

    def fast_response() -> httpx.Response:
        fast_started.set()
        return httpx.Response(
            200, headers={"Content-Type": "application/java-archive"}, content=fast_jar
        )

    fast_url = add_hangar_plugin_routes(api, "Fast", fast_jar)
    api._routes[fast_url] = fast_response

    config = base_config(
        '[plugins.slow]\nprovider = "hangar"\nproject = "Owner/Slow"\nversion = "1.0.0"\n\n'
        '[plugins.fast]\nprovider = "hangar"\nproject = "Owner/Fast"\nversion = "1.0.0"\n'
    )
    code = run_main(tmp_path, api, config)

    assert code == 0
    out = capsys.readouterr().out
    assert "Downloaded 3 items" in out
    assert out.index(" + slow.jar") < out.index(" + fast.jar")
    artifacts = tmp_path / "artifacts" / "plugins"
    assert (artifacts / "slow.jar").read_bytes() == slow_jar
    assert (artifacts / "fast.jar").read_bytes() == fast_jar


def test_default_config_path_from_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    api = FakeAPI()
    add_paper_routes(api)
    write_config(tmp_path, base_config())

    code = main(["download"], transport=api.transport(), sleep=lambda _: None)

    assert code == 0
    assert (tmp_path / "artifacts" / "paper.jar").is_file()
    assert_no_unexpected_state_files(tmp_path)


def test_second_run_skips_download(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    text = base_config()

    assert run_main(tmp_path, api, text) == 0
    capsys.readouterr()
    paper_requests_before = api.count(next(url for url in api.urls() if "fill-data" in url))

    assert run_main(tmp_path, api, text) == 0
    out = capsys.readouterr().out
    assert "Audited 1 item" in out
    paper_requests_after = api.count(next(url for url in api.urls() if "fill-data" in url))
    assert paper_requests_before == paper_requests_after == 1


def test_lock_file_is_written_and_reused(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    paper_jar = add_paper_routes(api)
    plugin_jar = add_good_plugin_routes(api)
    text = base_config(
        '[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\nversion = "1.0.0"\n'
    )
    config_path = write_config(tmp_path, text)

    assert run_main(tmp_path, api, text) == 0

    lock_text = (tmp_path / "rocket.lock").read_text(encoding="utf-8")
    assert "version = 1" in lock_text
    assert "build = 42" in lock_text
    assert f'sha256 = "{sha256_hex(paper_jar)}"' in lock_text
    assert "[plugins.good]" in lock_text
    assert f'sha256 = "{sha256_hex(plugin_jar)}"' in lock_text

    requests_before = len(api.requests)
    capsys.readouterr()
    code = main(
        ["download", "-v", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 0
    assert len(api.requests) == requests_before  # locked: no resolution and no download requests
    captured = capsys.readouterr()
    assert "Audited 2 items" in captured.out
    assert captured.err.count("using locked artifact") == 2


def test_changed_plugin_config_reresolves_that_entry_only(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    add_good_plugin_routes(api)
    text_v1 = base_config(
        '[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\nversion = "1.0.0"\n'
    )

    assert run_main(tmp_path, api, text_v1) == 0
    builds_url = fill_builds_url("26.1.2")
    assert api.count(builds_url) == 1

    jar_v2 = make_plugin_jar_bytes(name="Good")
    download_url_v2 = (
        "https://hangarcdn.papermc.io/plugins/Owner/Good/versions/2.0.0/PAPER/Good-2.0.0.jar"
    )
    api.add_json(
        hangar_version_url("Good", "2.0.0"),
        hangar_version_payload(
            name="2.0.0",
            downloads=hangar_native_paper(
                download_url=download_url_v2,
                name="Good-2.0.0.jar",
                sha256=sha256_hex(jar_v2),
                size=len(jar_v2),
            ),
            platform_dependencies={"PAPER": ["26.1.2"]},
        ),
    )
    api.add_bytes(download_url_v2, jar_v2)

    text_v2 = text_v1.replace('version = "1.0.0"', 'version = "2.0.0"')
    assert run_main(tmp_path, api, text_v2) == 0

    assert api.count(builds_url) == 1  # paper still comes from the lock
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == jar_v2
    lock_text = (tmp_path / "rocket.lock").read_text(encoding="utf-8")
    assert 'version = "2.0.0"' in lock_text


def test_upgrade_reresolves_and_updates_the_lock(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    paper_v1 = add_paper_routes(api)  # build 42
    text = base_config()
    config_path = write_config(tmp_path, text)
    builds_url = fill_builds_url("26.1.2")

    assert run_main(tmp_path, api, text) == 0
    assert api.count(builds_url) == 1
    assert "build = 42" in (tmp_path / "rocket.lock").read_text(encoding="utf-8")

    # Upstream publishes build 43; a normal run still pins build 42 from the lock.
    jar_v2 = make_paper_jar_bytes()
    url_v2 = f"https://fill-data.papermc.io/v1/objects/{sha256_hex(jar_v2)}/paper-26.1.2-43.jar"
    api.add_json(
        builds_url,
        [
            fill_build_payload(
                build_id=43,
                channel="STABLE",
                url=url_v2,
                name="paper-26.1.2-43.jar",
                sha256=sha256_hex(jar_v2),
                size=len(jar_v2),
            )
        ],
    )
    api.add_bytes(url_v2, jar_v2)

    assert run_main(tmp_path, api, text) == 0
    assert api.count(builds_url) == 1
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == paper_v1

    capsys.readouterr()
    code = run_main(
        tmp_path,
        api,
        text,
        argv=["download", "--upgrade", "--config", str(config_path)],
    )

    assert code == 0
    assert api.count(builds_url) == 2
    assert (tmp_path / "artifacts" / "paper.jar").read_bytes() == jar_v2
    assert "build = 43" in (tmp_path / "rocket.lock").read_text(encoding="utf-8")


def test_broken_lock_warns_and_is_rewritten(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    (tmp_path / "rocket.lock").write_text(
        'version = 1\n\n[paper]\nbroken = "yes"\n', encoding="utf-8"
    )

    assert run_main(tmp_path, api, base_config()) == 0

    assert "lock warning" in capsys.readouterr().err
    lock_text = (tmp_path / "rocket.lock").read_text(encoding="utf-8")
    assert "broken" not in lock_text
    assert "build = 42" in lock_text


def test_verbose_lists_unchanged_items_and_details(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    text = base_config()
    config_path = write_config(tmp_path, text)

    assert run_main(tmp_path, api, text) == 0
    capsys.readouterr()

    code = main(
        ["download", "-v", "--config", str(config_path)],
        transport=api.transport(),
        sleep=lambda _: None,
    )

    assert code == 0
    captured = capsys.readouterr()
    assert " = paper.jar (26.1.2 build 42, fill)" in captured.out
    assert "upstream hash verified" in captured.out
    assert "sha256=" in captured.out
    assert "[paper] using locked artifact" in captured.err


def test_no_plugins_only_paper(tmp_path: Path) -> None:
    api = FakeAPI()
    add_paper_routes(api)

    code = run_main(tmp_path, api, base_config())

    assert code == 0
    assert (tmp_path / "artifacts" / "paper.jar").is_file()
    assert not (tmp_path / "artifacts" / "plugins").exists()


def test_generic_user_agent_warning_printed(tmp_path: Path, capsys) -> None:
    api = FakeAPI()
    add_paper_routes(api)
    text = """
schema_version = 1

[paper]
version = "26.1.2"

[http]
user_agent = "curl/8.4.0"
"""

    code = run_main(tmp_path, api, text)

    assert code == 0
    assert "user_agent" in capsys.readouterr().err


def test_format_duration_units() -> None:
    assert _format_duration(0.012) == "12ms"
    assert _format_duration(3.5) == "3.5s"
    assert _format_duration(75.4) == "1m 15s"


def test_format_size_units() -> None:
    assert _format_size(512) == "512B"
    assert _format_size(2048) == "2.0KB"
    assert _format_size(3_500_000) == "3.3MB"
    assert _format_size(2**31) == "2.00GB"


def test_format_count_units() -> None:
    assert _format_count(845) == "845"
    assert _format_count(12_345) == "12.3K"
    assert _format_count(999_949) == "999.9K"
    assert _format_count(999_999) == "1.0M"
    assert _format_count(5_234_567) == "5.2M"


def test_unknown_command_exits_2() -> None:
    # argparse exits directly with code 2 on an unknown subcommand
    try:
        main(["nope"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("an unknown subcommand should trigger SystemExit")


def test_version_flag_exits_0(capsys) -> None:
    # argparse prints "rocket <version>" and exits directly with code 0
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:  # pragma: no cover
        raise AssertionError("--version should trigger SystemExit")
    assert capsys.readouterr().out.strip() == f"rocket {__version__}"


def test_no_uuid_or_secret_leaks_in_output(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "top-secret-value")
    api = FakeAPI()
    add_paper_routes(api)
    jar = add_good_plugin_routes(api)
    text = base_config(
        '[plugins.good]\nprovider = "hangar"\nproject = "Owner/Good"\nversion = "1.0.0"\n'
    )

    code = run_main(tmp_path, api, text)

    assert code == 0
    captured = capsys.readouterr()
    assert "top-secret-value" not in captured.out
    assert "top-secret-value" not in captured.err
    assert (tmp_path / "artifacts" / "plugins" / "good.jar").read_bytes() == jar
