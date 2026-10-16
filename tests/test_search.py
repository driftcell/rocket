"""Offline tests for rocket search: keyword search on Hangar, Modrinth and Spiget."""

from __future__ import annotations

import httpx

from rocket.cli import main

from .helpers import (
    FakeAPI,
    hangar_search_item,
    hangar_search_payload,
    hangar_search_url,
    modrinth_search_hit,
    modrinth_search_payload,
    modrinth_search_url,
    spiget_search_item,
    spiget_search_url,
)

QUERY = "coreprotect"


def run_search(api: FakeAPI, query: str = QUERY) -> int:
    return main(["search", query], transport=api.transport(), sleep=lambda _: None)


def add_spiget_empty(api: FakeAPI, query: str = QUERY) -> None:
    api.add_json(spiget_search_url(query), [])


def add_both_routes(api: FakeAPI) -> None:
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload(
            [
                hangar_search_item(
                    owner="Owner",
                    slug="Good",
                    name="Good",
                    description="block logging and rollback",
                )
            ]
        ),
    )
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload(
            [modrinth_search_hit(slug="coreprotect", title="CoreProtect", description="logging")]
        ),
    )
    add_spiget_empty(api)


def test_search_prints_ranked_blocks(capsys) -> None:
    api = FakeAPI()
    add_both_routes(api)

    assert run_search(api) == 0

    captured = capsys.readouterr()
    assert captured.out == (
        "  modrinth:coreprotect · 4.3K downloads\n"
        "    logging\n"
        "\n"
        "  hangar:Owner/Good · 1.2K downloads\n"
        "    block logging and rollback\n"
    )
    assert captured.err == ""


def test_search_distinct_name_is_shown_with_the_spec(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), hangar_search_payload([]))
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload(
            [modrinth_search_hit(slug="coreprotect", title="CoreProtect CE", description="logging")]
        ),
    )
    add_spiget_empty(api)

    assert run_search(api) == 0

    out = capsys.readouterr().out
    assert out == "  CoreProtect CE — modrinth:coreprotect · 4.3K downloads\n    logging\n"


def test_search_sorts_all_providers_by_downloads(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload(
            [hangar_search_item(owner="Owner", slug="Good", name="Good", downloads=999_999)]
        ),
    )
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload(
            [modrinth_search_hit(slug="coreprotect", title="CoreProtect", downloads=5)]
        ),
    )
    api.add_json(
        spiget_search_url(QUERY),
        [spiget_search_item(resource_id=28140, name="CoreProtect", downloads=2_000_000)],
    )

    assert run_search(api) == 0

    out = capsys.readouterr().out
    assert out == (
        "  CoreProtect — spiget:28140 · 2.0M downloads\n"
        "\n"
        "  hangar:Owner/Good · 1.0M downloads\n"
        "\n"
        "  modrinth:coreprotect · 5 downloads\n"
    )


def test_search_unknown_downloads_sort_last_without_a_count(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload(
            [hangar_search_item(owner="Owner", slug="Good", name="Good", downloads=None)]
        ),
    )
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload(
            [modrinth_search_hit(slug="coreprotect", title="CoreProtect", downloads=5)]
        ),
    )
    add_spiget_empty(api)

    assert run_search(api) == 0

    out = capsys.readouterr().out
    assert out == ("  modrinth:coreprotect · 5 downloads\n\n  hangar:Owner/Good\n")


def test_search_spiget_hit_with_a_real_name_leads_with_the_name(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), hangar_search_payload([]))
    api.add_json(modrinth_search_url(QUERY), modrinth_search_payload([]))
    api.add_json(
        spiget_search_url(QUERY),
        [
            spiget_search_item(
                resource_id=28140,
                name="CoreProtect",
                tag="Block logging and rollback",
                downloads=8_900_000,
            )
        ],
    )

    assert run_search(api) == 0

    out = capsys.readouterr().out
    assert out == (
        "  CoreProtect — spiget:28140 · 8.9M downloads\n    Block logging and rollback\n"
    )


def test_search_without_description_omits_the_tagline(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload([hangar_search_item(owner="Owner", slug="Good", name="Good")]),
    )
    api.add_json(modrinth_search_url(QUERY), modrinth_search_payload([]))
    add_spiget_empty(api)

    assert run_search(api) == 0

    assert capsys.readouterr().out == "  hangar:Owner/Good · 1.2K downloads\n"


def test_search_long_description_is_truncated_to_one_line(capsys, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    description = "word " * 40
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload(
            [hangar_search_item(owner="Owner", slug="Good", name="Good", description=description)]
        ),
    )
    api.add_json(modrinth_search_url(QUERY), modrinth_search_payload([]))
    add_spiget_empty(api)

    assert run_search(api) == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("    ")
    assert lines[1].endswith("…")
    assert len(lines[1]) <= 80
    assert description.strip() not in lines[1]


def test_search_no_results_is_success(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), hangar_search_payload([]))
    api.add_json(modrinth_search_url(QUERY), modrinth_search_payload([]))
    add_spiget_empty(api)

    assert run_search(api) == 0

    captured = capsys.readouterr()
    assert captured.out == "no plugins found for 'coreprotect'\n"
    assert captured.err == ""


def test_search_one_provider_fails_the_others_still_print(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload([hangar_search_item(owner="Owner", slug="Good", name="Good")]),
    )
    api.add_json(modrinth_search_url(QUERY), {"error": "server"}, status=500)
    add_spiget_empty(api)

    assert run_search(api) == 1

    captured = capsys.readouterr()
    assert "  hangar:Owner/Good · 1.2K downloads\n" in captured.out
    assert "error: modrinth:" in captured.err


def test_search_all_providers_fail(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), {"error": "server"}, status=500)
    api.add_json(modrinth_search_url(QUERY), {"error": "server"}, status=500)
    api.add_json(spiget_search_url(QUERY), {"error": "server"}, status=500)

    assert run_search(api) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: hangar:" in captured.err
    assert "error: modrinth:" in captured.err
    assert "error: spiget:" in captured.err


def test_search_empty_query_fails_before_network(capsys) -> None:
    api = FakeAPI()

    assert run_search(api, "   ") == 1

    assert "must not be empty" in capsys.readouterr().err
    assert api.requests == []


def test_search_query_is_url_encoded() -> None:
    api = FakeAPI()
    query = "essentials & co"
    api.add_json(hangar_search_url(query), hangar_search_payload([]))
    api.add_json(modrinth_search_url(query), modrinth_search_payload([]))
    add_spiget_empty(api, query)

    assert run_search(api, query) == 0

    assert "query=essentials+%26+co" in api.urls()[0]
    assert "query=essentials+%26+co" in api.urls()[1]
    assert "search/resources/essentials%20%26%20co?" in api.urls()[2]


def test_search_skips_malformed_hits(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload(
            [
                "not-a-table",
                {"name": "NoNamespace"},
                hangar_search_item(owner="Owner", slug="Good", name="Good"),
            ]
        ),
    )
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload(
            [
                {"title": "NoSlug"},
                modrinth_search_hit(slug="coreprotect", title="CoreProtect"),
            ]
        ),
    )
    api.add_json(
        spiget_search_url(QUERY),
        [
            {"id": "not-an-int", "name": "BadId"},
            {"id": 99999},
            spiget_search_item(resource_id=28140, name="CoreProtect SE"),
        ],
    )

    assert run_search(api) == 0

    out = capsys.readouterr().out
    assert "  modrinth:coreprotect · 4.3K downloads\n" in out
    assert "  hangar:Owner/Good · 1.2K downloads\n" in out
    assert "  CoreProtect SE — spiget:28140 · 4.3K downloads\n" in out
    assert "NoNamespace" not in out
    assert "NoSlug" not in out
    assert "BadId" not in out


def test_search_invalid_response_structure(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), ["not", "a", "table"])
    api.add_json(modrinth_search_url(QUERY), {"unexpected": True})
    api.add_json(spiget_search_url(QUERY), {"unexpected": True})

    assert run_search(api) == 1

    err = capsys.readouterr().err
    assert "error: hangar: Hangar search response has invalid structure" in err
    assert "error: modrinth: Modrinth search response is missing the hits field" in err
    assert "error: spiget: Spiget search response has invalid structure" in err


def test_search_network_error_is_reported(capsys) -> None:
    api = FakeAPI()
    api.add_error(hangar_search_url(QUERY), httpx.ConnectError("boom"))
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload([modrinth_search_hit(slug="coreprotect", title="CoreProtect")]),
    )
    add_spiget_empty(api)

    assert run_search(api) == 1

    captured = capsys.readouterr()
    assert "error: hangar: network error" in captured.err
    assert "  modrinth:coreprotect · 4.3K downloads\n" in captured.out
