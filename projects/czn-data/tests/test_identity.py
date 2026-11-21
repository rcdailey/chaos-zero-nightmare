"""Contract: on a game screenshot, every character figure is found and named from the stored card
art prints, in reading order; a figure too close to call names both candidates, and one that
matches no character is unknown.

Runs the real models from Hugging Face, downloaded into CZN_IDENTITY_DIR on first use (mise
sets it); skipped when it is not set. Fixtures are tablet screenshots and the November 2026 card
art of all 41 characters.
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from starlette.testclient import TestClient
from test_server import CHARACTER_HTML, CHARACTER_URL, MEI_LIN_PORTRAIT

from czn_data import store
from czn_data.build import build
from czn_data.identity import MODEL, Identifier
from czn_data.parse import page_kind, parse_page
from czn_data.server import build_server

FIXTURES = Path(__file__).parent / "fixtures" / "identity"


@pytest.fixture(scope="module")
def identifier():
    if not os.environ.get("CZN_IDENTITY_DIR"):
        pytest.skip("CZN_IDENTITY_DIR not set; run tests through mise")
    return Identifier()


@pytest.fixture(scope="module")
def prints(identifier):
    return {
        path.stem: identifier.print_of(path.read_bytes())
        for path in sorted((FIXTURES / "portraits").glob("*.webp"))
    }


def named(identifier, prints, screenshot: str) -> list[tuple[str, list[str]]]:
    figures = identifier.identify((FIXTURES / screenshot).read_bytes(), prints)
    return [(figure.result, figure.names) for figure in figures]


@pytest.mark.parametrize(
    "screenshot",
    # The second has the monster icon above the cards' middle; it stays in their row.
    ["team-formation.jpg", "team-formation-icon-above.jpg"],
)
def test_team_formation_cards_are_named_and_a_monster_icon_is_unknown(
    identifier, prints, screenshot
):
    assert named(identifier, prints, screenshot) == [
        ("match", ["Rossweisse"]),
        ("match", ["Tiphera"]),
        ("match", ["Mika"]),
        ("unknown", []),
    ]


def test_chibi_characters_are_named_or_left_open_between_two(identifier, prints):
    result = named(identifier, prints, "theme-park.jpg")
    assert ("close", ["Maribell", "Veronica"]) in result
    assert [r for r in result if r[0] == "match"] == [
        ("match", ["Hilde"]),
        ("match", ["Tiphera"]),
        ("match", ["Fei"]),
        ("match", ["Yuki"]),
        ("match", ["Narja"]),
    ]


class OneTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "good":
            return None
        return AccessToken(token=token, client_id="app", scopes=[], expires_at=None)


@pytest.fixture
def endpoint(identifier, conn, database_url, monkeypatch):
    """The server's HTTP app over a database holding one character, Mei Lin, whose card art
    print is stored as Rossweisse's art: the endpoint must name whatever the prints say."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    store.save_page(
        conn,
        CHARACTER_URL,
        page_kind(CHARACTER_URL),
        CHARACTER_HTML,
        datetime.now(UTC),
        parsed,
        None,
    )
    build(conn)
    art = (FIXTURES / "portraits" / "Rossweisse.webp").read_bytes()
    conn.execute(
        "INSERT INTO prydwen.portrait_identity (image_url, model, print, tagged_at)"
        " VALUES (%s, %s, %s, now())",
        (MEI_LIN_PORTRAIT, MODEL, identifier.print_of(art)),
    )
    auth = AuthSettings(
        issuer_url="https://auth.example.com",
        resource_server_url="http://testserver/mcp",
        validate_token_resource=False,
    )
    app = build_server(auth, OneTokenVerifier()).streamable_http_app(stateless_http=True)
    with TestClient(app) as client:
        yield client


def post(client, body: bytes, token: str | None = "good", kind: str = "image/jpeg"):
    headers = {"content-type": kind}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return client.post("/identify", content=body, headers=headers)


def test_identify_endpoint_names_the_stored_characters_in_screen_order(endpoint):
    response = post(endpoint, (FIXTURES / "team-formation.jpg").read_bytes())
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == MODEL
    assert [(c["result"], c["names"]) for c in body["characters"]] == [
        ("match", ["Mei Lin"]),
        ("unknown", []),
        ("unknown", []),
        ("unknown", []),
    ]
    assert len(body["characters"][0]["box"]) == 4


def test_identify_endpoint_rejects_missing_tokens_and_non_images(endpoint):
    screenshot = (FIXTURES / "team-formation.jpg").read_bytes()
    assert post(endpoint, screenshot, token=None).status_code == 401
    assert post(endpoint, screenshot, token="bad").status_code == 401
    assert post(endpoint, b"text", kind="text/plain").status_code == 400
    assert post(endpoint, b"not a jpeg").status_code == 400


def test_without_stored_prints_every_figure_is_unknown(identifier):
    figures = identifier.identify((FIXTURES / "team-formation.jpg").read_bytes(), {})
    assert figures and all(figure.result == "unknown" for figure in figures)
