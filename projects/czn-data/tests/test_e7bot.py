"""Contract: e7bot responses decode, and a run never replaces good e7bot data with broken data.

The run tests replace e7bot with an in-memory API (path -> data); the database is real Postgres.
"""

import json
from pathlib import Path

import pytest

from czn_data.e7bot import API, Client, decode, scrape_e7bot

FIXTURE = Path(__file__).parent / "fixtures" / "e7bot_packs.json"


def test_recorded_encrypted_response_decodes():
    packs = decode(json.loads(FIXTURE.read_text()), "czn/encounters/packs/list")
    mystery = next(p for p in packs if p["id"] == "uk_000_s01")
    assert (mystery["name"], mystery["pack_type"]) == ("Mystery", "PACK_FACTION")


def ranking(save_ids: list[str]) -> dict:
    entry = {"season_id": "s1", "score": 10}
    for slot, save_id in zip(["t1_c1", "t1_c2", "t1_c3"], save_ids, strict=False):
        entry |= {f"{slot}_id": 1, f"{slot}_save_id": save_id, f"{slot}_data_exists": True}
    return entry


def api(**overrides) -> dict:
    data = {
        "czn/characters/list": [
            {
                "id": "1",
                "char_type": "CHAR_COMBATANT",
                "names": {"en": "Haru"},
                "ego": "narcissism",
                "job": "striker",
                "rarity": 5,
                "gender_type": "GENDER_FEMALE",
            }
        ],
        "czn/cards/list": [
            {
                "id": "c_1_srt1",
                "name": "Anchor",
                "cost": 1,
                "card_category": "CARD_ATK",
                "rarity": "RARITY_COMMON",
                "desc_display": "100% Damage",
                "r_spark_cards": [],
                "base_combatant": {"id": "1"},
                "sort": "SORT_START",
                "talent": [],
                "tooltips": [],
            }
        ],
        "czn/characters/partner/list/detail": [
            {
                "id": "2",
                "name": "Arwen",
                "rarity": 4,
                "job": "controller",
                "partner_card": {},
                "partner_passives": [],
            }
        ],
        "czn/relics/list": [
            {
                "id": "r",
                "name": "Ring",
                "rarity": "RARITY_RARE",
                "relic_category": "EQUIPMENT",
                "relic_type": "EQUIP_ACC",
                "skills": [],
            }
        ],
        "czn/r-sparks/list": [
            {"id": "s", "description": "+30%", "god_type": "VITOR", "rarity": "R"}
        ],
        "czn/piece-set-options/list": [
            {"id": "f", "name": "Set", "set2": None, "set4": None, "set6": None}
        ],
        "czn/encounters/list": [
            {
                "id": "e1",
                "name": "Peddler",
                "link_encounter_pack_id": "p",
                "chaos_collection_info": None,
            }
        ],
        "czn/encounters/packs/list": [{"id": "p", "name": "Mystery", "pack_type": "PACK_FACTION"}],
        "czn/rankings/list": [ranking(["100", "101"])],
        "czn/snapshot/characters/diff": {"timestamp": 1.0},
        "czn/snapshot/encounters/diff": {"timestamp": 1.0},
        "czn/characters/1": {"potential_nodes": []},
        "czn/encounters/e1": {"options": []},
        "czn/rankings/savedata/100": {"card_datas": []},
        "czn/rankings/savedata/101": {"card_datas": []},
    }
    return data | overrides


def client(data: dict) -> Client:
    requested = []

    def get(url: str) -> str:
        path = url.removeprefix(API).removesuffix("?lang=en")
        requested.append(path)
        return json.dumps({"code": 0, "data": data[path]})

    result = Client(get)
    result.requested = requested
    return result


def stored_paths(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT path FROM e7bot.document")}


def test_clean_run_stores_every_path_and_save(conn):
    result = scrape_e7bot(conn, client(api()), allow_shrink=False)

    assert result.ok
    assert {"czn/cards/list", "czn/characters/1", "czn/encounters/e1"} <= stored_paths(conn)
    saves = {row[0] for row in conn.execute("SELECT save_id FROM e7bot.save")}
    assert saves == {"100", "101"}
    assert conn.execute("SELECT ok FROM e7bot.run").fetchall() == [(True,)]


@pytest.mark.parametrize(
    "broken",
    [
        {"czn/cards/list": [{"id": "c_1_srt1"}]},  # items lost required keys
        {  # after a game update, a detail changed shape
            "czn/snapshot/encounters/diff": {"timestamp": 2.0},
            "czn/encounters/e1": {"no_options": []},
        },
        {"czn/relics/list": []},  # list emptied
    ],
)
def test_broken_response_keeps_previous_documents_and_fails(conn, broken):
    scrape_e7bot(conn, client(api()), allow_shrink=False)
    before = conn.execute("SELECT path, body FROM e7bot.document ORDER BY path").fetchall()

    result = scrape_e7bot(conn, client(api(**broken)), allow_shrink=False)

    assert not result.ok
    assert conn.execute("SELECT path, body FROM e7bot.document ORDER BY path").fetchall() == before
    assert conn.execute("SELECT ok FROM e7bot.run ORDER BY started_at").fetchall() == [
        (True,),
        (False,),
    ]


def test_large_list_loss_is_rejected_unless_allowed(conn):
    many = api()["czn/encounters/packs/list"] * 10
    scrape_e7bot(conn, client(api(**{"czn/encounters/packs/list": many})), allow_shrink=False)

    rejected = scrape_e7bot(conn, client(api()), allow_shrink=False)
    accepted = scrape_e7bot(conn, client(api()), allow_shrink=True)

    assert not rejected.ok
    assert accepted.ok


def detail_requests(client: Client) -> list[str]:
    return sorted(p for p in client.requested if p in {"czn/characters/1", "czn/encounters/e1"})


def test_details_are_refetched_only_after_e7bot_imports_new_game_data(conn):
    scrape_e7bot(conn, client(api()), allow_shrink=False)

    unchanged = client(api())
    assert scrape_e7bot(conn, unchanged, allow_shrink=False).ok
    assert detail_requests(unchanged) == []
    assert "czn/encounters/e1" in stored_paths(conn)

    patched = client(api(**{"czn/snapshot/encounters/diff": {"timestamp": 2.0}}))
    scrape_e7bot(conn, patched, allow_shrink=False)
    assert detail_requests(patched) == ["czn/encounters/e1"]


def test_details_older_than_a_week_are_refetched(conn):
    scrape_e7bot(conn, client(api()), allow_shrink=False)
    conn.execute("UPDATE e7bot.document SET fetched_at = now() - interval '8 days'")

    later = client(api())
    scrape_e7bot(conn, later, allow_shrink=False)

    assert detail_requests(later) == ["czn/characters/1", "czn/encounters/e1"]


def test_stored_saves_are_not_fetched_again(conn):
    scrape_e7bot(conn, client(api()), allow_shrink=False)
    later = client(
        api(**{"czn/rankings/list": [ranking(["100", "102"])]})
        | {"czn/rankings/savedata/102": {"card_datas": []}}
    )

    scrape_e7bot(conn, later, allow_shrink=False)

    saves = [p for p in later.requested if p.startswith("czn/rankings/savedata/")]
    assert saves == ["czn/rankings/savedata/102"]
