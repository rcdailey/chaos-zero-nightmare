"""Which equipment a game screenshot shows as icons in square slots, such as the gear each
character wears on the Equipment screen, which the game shows without names.

DINOv2 small (``onnx-community/dinov2-small-ONNX``), a general image model, turns a picture into
a print: a vector of 384 numbers, where similar pictures get similar vectors. The daily enrich
stores prints of every e7bot equipment icon drawn on each rarity background, as the game draws
it in a slot (``e7bot.gear_print``). A screenshot's square boxes are found from their outlines
and each is compared with every stored print.

On the Equipment screen of 2026-11-22, every filled slot matched its icon at 0.83 or more and
led the next icon by 0.14 or more; empty slots and other squares scored 0.61 or less. Several
items share one icon (a Harmonization or Mutation form uses its base item's icon), so a match
names every item drawn with that icon.
"""

import functools
import io
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from PIL import Image

from . import digest, hub
from .identity import reading_rows

_REPO = "onnx-community/dinov2-small-ONNX"
_REVISION = "08c606e3123472a388efa59181b677d428f69bbd"
_FILES = ("onnx/model.onnx",)
MODEL = "dinov2-small"


@functools.cache
def prints_digest() -> str:
    """Fingerprint of the code that makes prints (``czn_data.digest``). Stored prints are kept
    under it, the server compares only prints of its own code, and enrich prints every icon
    again after any change to that code or the packages it uses."""
    return digest.code(__name__)


# A box names an icon when its print is this similar (cosine) to one of the icon's prints...
SAME_ICON = 0.7
# ...and leads every other icon by this much; otherwise it names the best two.
TOO_CLOSE = 0.1
# Slot sides, as a share of the screenshot's height, and how far from square a box may be.
_SIDE = (0.04, 0.2)
_SQUARE = (0.85, 1.18)
# Slots in a row are this many sides apart (the Equipment screen's spacing).
_STEP = 1.26
# Boxes overlapping more than this share of the smaller one are the same slot.
_SAME_SLOT = 0.5
_CANVAS, _ICON = 150, 120  # how large an icon is drawn on its background for printing
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)[:, None, None]
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)[:, None, None]


@dataclass(frozen=True)
class Gear:
    """One slot on a screenshot that shows a known icon.

    ``result`` is ``match`` (``icons`` holds the one icon) or ``close`` (the best two, too
    similar to choose between). ``names`` are the equipment drawn with those icons. ``row``
    numbers the slot's row from the top, starting at 1. ``candidates`` are the three most
    similar icons, most similar first, with their ``scores``.
    """

    box: tuple[int, int, int, int]
    row: int
    result: Literal["match", "close"]
    icons: list[str]
    names: list[str]
    candidates: list[str]
    scores: list[float]


class GearIdentifier:
    """Loads the model once and keeps it for every later call; not thread-safe to load."""

    def __init__(self, model_dir: str | None = None):
        self.model_dir = model_dir or os.environ.get("CZN_IDENTITY_DIR")
        self._session = None

    def prints_of(self, icon: bytes, backgrounds: list[bytes]) -> list[list[float]]:
        """The icon's print on each background, as a slot of that rarity draws it."""
        picture = Image.open(io.BytesIO(icon)).convert("RGBA").resize((_ICON, _ICON))
        offset = (_CANVAS - _ICON) // 2
        drawn = []
        for background in backgrounds:
            canvas = _square(Image.open(io.BytesIO(background)).convert("RGBA"))
            canvas = canvas.resize((_CANVAS, _CANVAS))
            canvas.alpha_composite(picture, (offset, offset))
            drawn.append(canvas)
        return self._prints(drawn).tolist()

    def identify(
        self,
        image: bytes,
        prints: Mapping[str, list[list[float]]],
        names: Mapping[str, list[str]],
    ) -> list[Gear]:
        """Every slot on a screenshot that shows a stored icon, top row first and left to right
        in a row. ``prints`` maps icon names to their prints, ``names`` icon names to the
        equipment drawn with them. Slots no icon matches (empty ones), and slots whose icon names no
        item, are left out."""
        picture = Image.open(io.BytesIO(image)).convert("RGB")
        boxes = slot_boxes(np.asarray(picture))
        icons = list(prints)
        if not boxes or not icons:
            return []
        stored = np.asarray([prints[icon] for icon in icons], dtype=np.float32)
        found = self._prints([picture.crop(box) for box in boxes])
        similarity = np.einsum("ibd,sd->sib", stored, found).max(axis=2)  # (slot, icon)
        slots = []
        for box, scores in zip(boxes, similarity, strict=True):
            order = np.argsort(-scores)[:3]
            best = [float(scores[i]) for i in order]
            if best[0] < SAME_ICON:
                continue
            close = len(best) > 1 and best[0] - best[1] < TOO_CLOSE
            slots.append((box, best, [icons[i] for i in order], close))
        gear = []
        for box, best, candidates, close in sorted(slots, key=lambda s: -s[1][0]):
            if any(_overlap(box, other.box) > _SAME_SLOT for other in gear):
                continue
            chosen = candidates[: 2 if close else 1]
            named = sorted({name for icon in chosen for name in names.get(icon, [])})
            if not names.get(chosen[0]):
                named = []
            result = "close" if close else "match"
            # A slot whose best icon names no item still claims its box, so neither a weaker
            # box over the same slot nor a close second icon can name it as other gear; it is
            # left out below.
            gear.append(Gear(box, 0, result, chosen, named, candidates, best))
        named_gear = [slot for slot in gear if slot.names]
        return [
            replace(slot, row=number)
            for number, row in enumerate(reading_rows(named_gear), start=1)
            for slot in row
        ]

    def _prints(self, pictures: list[Image.Image]) -> np.ndarray:
        if self._session is None:
            self._session = hub.session(_REPO, _REVISION, _FILES, _FILES[0], self.model_dir)
        data = np.stack([_model_input(picture) for picture in pictures])
        # The first token of the last layer is the image's summary (DINOv2's pooled output).
        found = self._session.run(["last_hidden_state"], {"pixel_values": data})[0][:, 0]
        return found / np.linalg.norm(found, axis=1, keepdims=True)


def download(model_dir: str) -> None:
    """Store the model in ``model_dir``, for building an image that never downloads."""
    hub.download(_REPO, _REVISION, _FILES, Path(model_dir) / _REPO.split("/")[1])


def slot_boxes(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Square boxes on a screenshot that may be equipment slots, as (left, top, right, bottom).

    Boxes are closed outlines of slot size. An icon drawn over a slot's border breaks its
    outline, so a gap between two equal boxes in a row that fits whole slot steps is filled
    with the missing slots. Nested and overlapping boxes are all kept; the comparison keeps the
    best-matching one."""
    height = rgb.shape[0]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    found = set()
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (_SIDE[0] * height <= w <= _SIDE[1] * height and _SQUARE[0] <= w / h <= _SQUARE[1]):
            continue
        if cv2.contourArea(contour) >= 0.8 * w * h:  # an outline, not a stroke of a drawing
            found.add((x, y, w, h))
    filled = set(found)
    for x, y, w, h in found:
        for x2, y2, w2, _ in found:
            if x2 <= x or abs(y2 - y) > 0.1 * h or abs(w2 - w) > 0.1 * w:
                continue
            gap = x2 - x
            steps = round(gap / (w * _STEP))
            if steps >= 2 and abs(gap / steps - w * _STEP) < 0.15 * w:
                filled.update((round(x + gap * i / steps), y, w, h) for i in range(1, steps))
    return sorted((x, y, x + w, y + h) for x, y, w, h in filled)


def _square(picture: Image.Image) -> Image.Image:
    """The middle square of a background drawn for a taller card."""
    side = min(picture.size)
    left, top = (picture.width - side) // 2, (picture.height - side) // 2
    return picture.crop((left, top, left + side, top + side))


def _model_input(picture: Image.Image) -> np.ndarray:
    """As DINOv2's preprocessor prepares a picture: shortest side to 256 (bicubic), the middle
    224 x 224, scaled to 0-1 and normalized with ImageNet's mean and spread."""
    picture = picture.convert("RGB")
    scale = 256 / min(picture.size)
    picture = picture.resize(
        (round(picture.width * scale), round(picture.height * scale)), Image.Resampling.BICUBIC
    )
    left, top = (picture.width - 224) // 2, (picture.height - 224) // 2
    picture = picture.crop((left, top, left + 224, top + 224))
    data = np.asarray(picture, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return ((data - _MEAN) / _STD).astype(np.float32)


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Shared area as a share of the smaller box."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return width * height / smaller
