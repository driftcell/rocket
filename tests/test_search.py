"""Offline tests for rocket search: keyword search on Hangar and Modrinth."""

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
)

QUERY = "coreprotect"


def run_search(api: FakeAPI, query: str = QUERY) -> int:
    return main(["search", query], transport=api.transport(), sleep=lambda _: None)


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


def test_search_prints_copyable_specs(capsys) -> None:
    api = FakeAPI()
    add_both_routes(api)

    assert run_search(api) == 0

    captured = capsys.readouterr()
    assert "  hangar:Owner/Good     Good — block logging and rollback\n" in captured.out
    assert "  modrinth:coreprotect  CoreProtect — logging\n" in captured.out
    assert captured.err == ""


def test_search_without_description_omits_the_dash(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload([hangar_search_item(owner="Owner", slug="Good", name="Good")]),
    )
    api.add_json(modrinth_search_url(QUERY), modrinth_search_payload([]))

    assert run_search(api) == 0

    assert capsys.readouterr().out == "  hangar:Owner/Good  Good\n"


def test_search_no_results_is_success(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), hangar_search_payload([]))
    api.add_json(modrinth_search_url(QUERY), modrinth_search_payload([]))

    assert run_search(api) == 0

    captured = capsys.readouterr()
    assert captured.out == "no plugins found for 'coreprotect'\n"
    assert captured.err == ""


def test_search_one_provider_fails_the_other_still_prints(capsys) -> None:
    api = FakeAPI()
    api.add_json(
        hangar_search_url(QUERY),
        hangar_search_payload([hangar_search_item(owner="Owner", slug="Good", name="Good")]),
    )
    api.add_json(modrinth_search_url(QUERY), {"error": "server"}, status=500)

    assert run_search(api) == 1

    captured = capsys.readouterr()
    assert "  hangar:Owner/Good  Good\n" in captured.out
    assert "error: modrinth:" in captured.err


def test_search_both_providers_fail(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), {"error": "server"}, status=500)
    api.add_json(modrinth_search_url(QUERY), {"error": "server"}, status=500)

    assert run_search(api) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: hangar:" in captured.err
    assert "error: modrinth:" in captured.err


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

    assert run_search(api, query) == 0

    assert "query=essentials+%26+co" in api.urls()[0]
    assert "query=essentials+%26+co" in api.urls()[1]


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

    assert run_search(api) == 0

    out = capsys.readouterr().out
    assert "  hangar:Owner/Good     Good\n" in out
    assert "  modrinth:coreprotect  CoreProtect\n" in out
    assert "NoNamespace" not in out
    assert "NoSlug" not in out


def test_search_invalid_response_structure(capsys) -> None:
    api = FakeAPI()
    api.add_json(hangar_search_url(QUERY), ["not", "a", "table"])
    api.add_json(modrinth_search_url(QUERY), {"unexpected": True})

    assert run_search(api) == 1

    err = capsys.readouterr().err
    assert "error: hangar: Hangar search response has invalid structure" in err
    assert "error: modrinth: Modrinth search response is missing the hits field" in err


def test_search_network_error_is_reported(capsys) -> None:
    api = FakeAPI()
    api.add_error(hangar_search_url(QUERY), httpx.ConnectError("boom"))
    api.add_json(
        modrinth_search_url(QUERY),
        modrinth_search_payload([modrinth_search_hit(slug="coreprotect", title="CoreProtect")]),
    )

    assert run_search(api) == 1

    captured = capsys.readouterr()
    assert "error: hangar: network error" in captured.err
    assert "  modrinth:coreprotect  CoreProtect\n" in captured.out
