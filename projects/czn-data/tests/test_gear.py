"""Contract: POST /identify-gear takes the MCP Bearer token and a JPEG or PNG screenshot, and
names the equipment in its slots from the icon prints stored for the current print code, each
icon naming every item drawn with it. Identification lists the slots that show a known icon by
row, top row first and left to right; names two icons too close to choose; and leaves out empty
slots and slots whose icon names no item. A slot whose outline an icon covers still counts.

Two route tests replace the identifier with a fake that names one slot per stored icon. The
identification test, also through the route, replaces the model with a fake whose print of a
picture is its middle's average color; how well the real model matches icons is checked by hand, not here.
"""

import io

import numpy as np
import pytest
from mcp.server.auth.settings import AuthSettings
from PIL import Image, ImageDraw
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient
from test_identity import OneTokenVerifier, screenshot

from czn_data.build import build
from czn_data.gear import MODEL, Gear, prints_digest
from czn_data.server import build_server


class OneSlotPerIcon:
    def identify(self, image, prints, names) -> list[Gear]:
        Image.open(io.BytesIO(image)).load()
        return [
            Gear((0, 0, 1, 1), 1, "match", [icon], names[icon], [icon], [0.9]) for icon in prints
        ]


def relic(relic_id: str, name: str, icon: str) -> dict:
    return {
        "id": relic_id,
        "name": name,
        "rarity": "RARITY_RARE",
        "relic_category": "EQUIPMENT",
        "relic_type": "EQUIP_WEAPON",
        "skills": [{"description": f"{name} effect"}],
        "icon_name": icon,
    }


@pytest.fixture
def serve(conn, database_url, monkeypatch):
    """Starts the server's HTTP app over a database holding ``relics`` and gear prints
    (icon name, print code, prints)."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    clients = []

    def start(relics: list[dict], prints: list[tuple[str, str, list[list[float]]]]):
        conn.execute(
            "INSERT INTO e7bot.document (path, body, fetched_at)"
            " VALUES ('czn/relics/list', %s, now())",
            (Jsonb(relics),),
        )
        build(conn)
        for row in prints:
            conn.execute(
                "INSERT INTO e7bot.gear_print (icon_name, digest, prints, tagged_at)"
                " VALUES (%s, %s, %s, now())",
                row,
            )
        auth = AuthSettings(
            issuer_url="https://auth.example.com",
            resource_server_url="http://testserver/mcp",
            validate_token_resource=False,
        )
        app = build_server(auth, OneTokenVerifier()).streamable_http_app(stateless_http=True)
        client = TestClient(app)
        clients.append(client.__enter__())
        return client

    yield start
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def endpoint(serve, monkeypatch):
    """Two items that share one icon, printed by the current code, and a third item's icon
    printed only by older code; the identifier names one slot per stored icon."""
    monkeypatch.setattr("czn_data.server.GearIdentifier", OneSlotPerIcon)
    relics = [
        relic("eq_1", "Big Game Hunter", "relics/relic_1195"),
        relic("eq_2", "Harmonization: Big Game Hunter", "relics/relic_1195"),
        relic("eq_3", "Runic Hammer", "relics/relic_5084"),
    ]
    return serve(
        relics,
        [
            ("relics/relic_1195", prints_digest(), [[1.0]]),
            ("relics/relic_5084", "older", [[1.0]]),
        ],
    )


def post(client, body: bytes, token: str | None = "good", kind: str = "image/jpeg"):
    headers = {"content-type": kind}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return client.post("/identify-gear", content=body, headers=headers)


def test_identify_gear_names_every_item_drawn_with_the_matched_icon(endpoint):
    response = post(endpoint, screenshot())
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == MODEL
    assert [(g["result"], g["row"], g["names"]) for g in body["gear"]] == [
        ("match", 1, ["Big Game Hunter", "Harmonization: Big Game Hunter"])
    ]


def test_identify_gear_rejects_missing_tokens_and_non_images(endpoint):
    assert post(endpoint, screenshot(), token=None).status_code == 401
    assert post(endpoint, b"text", kind="text/plain").status_code == 400
    assert post(endpoint, b"not a jpeg").status_code == 400


RED, BLUE, GREEN, PURPLE = (200, 30, 30), (30, 30, 200), (30, 200, 30), (115, 30, 115)
DARK_RED = (200, 30, 0)
# DINOv2's input normalization (ImageNet's mean and spread), undone by the fake model.
_MEAN = np.array([0.485, 0.456, 0.406])[None, :, None, None]
_STD = np.array([0.229, 0.224, 0.225])[None, :, None, None]


class AverageColor:
    """Stands in for the model's session: a picture's print is the average color of its middle,
    away from a slot's outline."""

    def run(self, outputs, inputs):
        colors = (inputs["pixel_values"] * _STD + _MEAN)[:, :, 56:168, 56:168].mean(axis=(2, 3))
        return [colors[:, None, :]]


def print_of(color) -> list[float]:
    """A stored print, normalized as the real ones are."""
    return list(np.asarray(color) / np.linalg.norm(color))


def test_identification_names_slots_by_row_and_leaves_out_empty_and_unnamed_ones(
    serve, monkeypatch
):
    monkeypatch.setattr("czn_data.gear.hub.session", lambda *args, **kwargs: AverageColor())
    picture = Image.new("RGB", (1600, 1000), (20, 20, 20))
    draw = ImageDraw.Draw(picture)
    side, step = 120, 151
    rows = {300: [RED, BLUE, PURPLE], 600: [GREEN, RED, DARK_RED]}
    for top, colors in rows.items():
        for i, color in enumerate(colors):
            left = 600 + i * step
            draw.rectangle((left, top, left + side, top + side), fill=color, outline="white")
    # An icon drawn across the blue slot's top edge breaks its outline.
    draw.rectangle((600 + step - 10, 280, 600 + step + side + 10, 340), fill=BLUE)
    draw.rectangle((100, 100, 500, 160), outline="white")  # a wide panel, not a slot
    out = io.BytesIO()
    picture.save(out, "PNG")
    # Purple is as similar to red as to blue; green (an empty slot) is like no icon. The dark
    # red icon names no item (such as one printed before the build adds its equipment) and is
    # too close to red to choose: its slot is left out, not named as red's gear, and red slots
    # are close calls naming only red's gear.
    relics = [
        relic("eq_1", "Runic Hammer", "red"),
        relic("eq_2", "Crimson Sword", "blue"),
        relic("eq_3", "Distortion: Crimson Sword", "blue"),
    ]
    code = prints_digest()
    colors = {"red": RED, "blue": BLUE, "dark": DARK_RED}
    client = serve(relics, [(icon, code, [print_of(color)]) for icon, color in colors.items()])

    response = post(client, out.getvalue(), kind="image/png")

    assert response.status_code == 200
    hammer, sword = ["Runic Hammer"], ["Crimson Sword", "Distortion: Crimson Sword"]
    assert [(g["row"], g["result"], g["names"]) for g in response.json()["gear"]] == [
        (1, "close", hammer),
        (1, "match", sword),
        (1, "close", sorted(sword + hammer)),
        (2, "close", hammer),
    ]
