"""Contract: POST /identify takes the MCP Bearer token and a JPEG or PNG screenshot, and names
figures from the card art prints stored for the current identity model.

The models are replaced by a fake that names one figure per stored print; how well the real
models name characters is checked by hand, not here.
"""

import io
from datetime import UTC, datetime

import pytest
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from PIL import Image
from starlette.testclient import TestClient
from test_server import CHARACTER_HTML, CHARACTER_URL, MEI_LIN_PORTRAIT

from czn_data import store
from czn_data.build import build
from czn_data.identity import MODEL, Figure
from czn_data.parse import page_kind, parse_page
from czn_data.server import build_server


class OneFigurePerPrint:
    def identify(self, image: bytes, prints: dict[str, list[float]]) -> list[Figure]:
        Image.open(io.BytesIO(image)).load()
        return [Figure((0, 0, 1, 1), "match", [name], [name], [0.0]) for name in prints]


class OneTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "good":
            return None
        return AccessToken(token=token, client_id="app", scopes=[], expires_at=None)


def screenshot() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def endpoint(conn, database_url, monkeypatch):
    """The server's HTTP app over a database holding Mei Lin with a print from the current
    model, and a stale print from an older model for the same art."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr("czn_data.server.Identifier", OneFigurePerPrint)
    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    now = datetime.now(UTC)
    store.save_page(
        conn, CHARACTER_URL, page_kind(CHARACTER_URL), CHARACTER_HTML, now, parsed, None
    )
    build(conn)
    for model in (MODEL, "older-model"):
        conn.execute(
            "INSERT INTO prydwen.portrait_identity (image_url, model, print, tagged_at)"
            " VALUES (%s, %s, %s, now())",
            (MEI_LIN_PORTRAIT, model, [1.0]),
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


def test_identify_endpoint_names_characters_from_the_current_models_prints(endpoint):
    response = post(endpoint, screenshot())
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == MODEL
    assert [(c["result"], c["names"], c["box"]) for c in body["characters"]] == [
        ("match", ["Mei Lin"], [0, 0, 1, 1])
    ]


def test_identify_endpoint_rejects_missing_tokens_and_non_images(endpoint):
    assert post(endpoint, screenshot(), token=None).status_code == 401
    assert post(endpoint, screenshot(), token="bad").status_code == 401
    assert post(endpoint, b"text", kind="text/plain").status_code == 400
    assert post(endpoint, b"not a jpeg").status_code == 400
