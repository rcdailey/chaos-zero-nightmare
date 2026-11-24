"""Contract: POST /identify-screenshots takes the MCP Bearer token and one or more JPEG or PNG
screenshots, and names the figures on each, in the order sent, from the card art prints stored
for the current print code.

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
from czn_data.identity import MODEL, Figure, prints_digest
from czn_data.parse import page_kind, parse_page
from czn_data.server import build_server


class OneFigurePerPrint:
    """Names one figure per stored print, boxed as the whole screenshot."""

    def identify(self, image: bytes, prints: dict[str, list[float]]) -> list[Figure]:
        picture = Image.open(io.BytesIO(image))
        picture.load()
        box = (0, 0, picture.width, picture.height)
        return [Figure(box, "match", [name], [name], [0.0]) for name in prints]


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
    print code, and a stale print from older code for the same art."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr("czn_data.server.Identifier", OneFigurePerPrint)
    parsed = parse_page(CHARACTER_URL, CHARACTER_HTML)
    now = datetime.now(UTC)
    store.save_page(
        conn, CHARACTER_URL, page_kind(CHARACTER_URL), CHARACTER_HTML, now, parsed, None
    )
    build(conn)
    for code in (prints_digest(), "older-code"):
        conn.execute(
            "INSERT INTO prydwen.portrait_identity (image_url, digest, print, tagged_at)"
            " VALUES (%s, %s, %s, now())",
            (MEI_LIN_PORTRAIT, code, [1.0]),
        )
    auth = AuthSettings(
        issuer_url="https://auth.example.com",
        resource_server_url="http://testserver/mcp",
        validate_token_resource=False,
    )
    app = build_server(auth, OneTokenVerifier()).streamable_http_app(stateless_http=True)
    with TestClient(app) as client:
        yield client


def post(client, *bodies: bytes, token: str | None = "good", kind: str = "image/jpeg"):
    """Sends ``bodies`` as the screenshots, each an ``image`` part of type ``kind``."""
    headers = {"authorization": f"Bearer {token}"} if token else {}
    files = [("image", (f"{i}.jpg", body, kind)) for i, body in enumerate(bodies)]
    return client.post("/identify-screenshots", files=files, headers=headers)


def test_identify_endpoint_names_characters_from_the_current_models_prints(endpoint):
    response = post(endpoint, screenshot())
    assert response.status_code == 200
    body = response.json()
    assert body["character_model"] == MODEL
    characters = body["screenshots"][0]["characters"]
    assert [(c["result"], c["names"], c["box"]) for c in characters] == [
        ("match", ["Mei Lin"], [0, 0, 8, 8])
    ]


def test_identify_endpoint_answers_every_screenshot_in_the_order_sent(endpoint):
    narrow, wide = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (4, 4)).save(narrow, "JPEG")
    Image.new("RGB", (16, 4)).save(wide, "JPEG")

    response = post(endpoint, wide.getvalue(), narrow.getvalue())

    assert response.status_code == 200
    boxes = [s["characters"][0]["box"] for s in response.json()["screenshots"]]
    assert boxes == [[0, 0, 16, 4], [0, 0, 4, 4]]


def test_identify_endpoint_rejects_missing_tokens_and_non_images(endpoint):
    assert post(endpoint, screenshot(), token=None).status_code == 401
    assert post(endpoint, screenshot(), token="bad").status_code == 401
    assert post(endpoint).status_code == 400
    assert post(endpoint, b"text", kind="text/plain").status_code == 400
    assert post(endpoint, screenshot(), b"not a jpeg").status_code == 400
