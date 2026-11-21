"""What portrait art shows, because neither source describes how characters look.

The WD14 anime image tagger (SmilingWolf/wd-swinv2-tagger-v3, an ONNX model trained on Danbooru
tags) scores each Danbooru tag for a picture. Two things come from those scores:

- Gender, because Prydwen does not state it: a portrait whose ``1girl`` score is at least
  ``FEMALE_THRESHOLD`` is female; every other portrait is male, so there is no unknown value.
  Checked against Danbooru fan-art tags for the 41 characters of November 2026: no disagreement.
- Appearance: the visible traits (hair color and style, eye color, eyewear, horns, weapons,
  outfit) that let Claude tell which character a portrait on a game screen shows. The game's
  portraits are the same art, so the tags match what a screenshot shows. Composition, pose,
  expression, and body tags are dropped (``_NOT_APPEARANCE``); a change to that filter only
  applies to portraits tagged afterwards.

The model (467 MB) is downloaded on first use into ``CZN_TAGGER_DIR``, a directory kept across
runs (a PVC in the cluster), so runs with no new portraits neither download nor load it. Each
time it is needed, the newest revision on Hugging Face is looked up; a new revision replaces
the stored copy, verified against the SHA-256 Hugging Face lists for it. When Hugging Face is
unreachable, the stored copy is used.
"""

import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime
from PIL import Image

log = logging.getLogger(__name__)

FEMALE_THRESHOLD = 0.5
# Lowest score of an appearance tag, and how many tags to keep, most certain first. Key traits
# (hair, eyes, eyewear, horns, ears, weapons) above the threshold are always kept; the limit
# caps only the other tags (outfit, accessories).
APPEARANCE_THRESHOLD = 0.35
APPEARANCE_LIMIT = 15
# Some art scores no hair color above the threshold (white or grey hair in bright light). The
# best hair color is still kept when it scores at least this; checked on the November 2026
# roster, where every such best guess matched the art.
HAIR_COLOR_FALLBACK = 0.15
_HAIR_COLOR = re.compile(r"^(light_|dark_)?(?!long|short|very|medium|hair)[a-z]+_hair$")
_KEY_TRAIT = re.compile(
    r"^(.*_hair|.*hair_.*|twintails|.*ponytail|.*braids?|.*hair_bun|double_bun|ahoge"
    r"|.*_eyes|heterochromia|glasses|.*eyewear.*|eyepatch|horns|.*_ears|halo|wings"
    r"|weapon|sword|katana|gun|rifle|.*_weapon|umbrella|instrument|guitar|mask)$"
)
# General tags that say nothing about which character it is: framing, pose, expression, body.
_NOT_APPEARANCE = re.compile(
    r"^(1girl|1boy|solo|.*_focus|.*_background|looking_.*|.*_shot|upper_body|full_body"
    r"|standing|sitting|leaning_.*|smile|blush|.*_mouth|:.|.*teeth.*|.*_lips|one_eye_closed"
    r"|.*breasts?|.*boob.*|cleavage|navel|collarbone|thighs|pectorals|bara|muscular.*"
    r"|virtual_youtuber"
    r"|hand_.*|.*_hand|hands_.*|arm_.*|.*_arms?|holding.*|sparkle|signature)$"
)
MODEL_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
MODEL_FILES = ("model.onnx", "selected_tags.csv")
_HUB = "https://huggingface.co"


class TaggerUnavailable(Exception):
    """The model could not be found or downloaded; no portrait can be tagged this run."""


class Tagger:
    def __init__(self, model_dir: str | None = None):
        self.model_dir = model_dir or os.environ.get("CZN_TAGGER_DIR")
        self._session: onnxruntime.InferenceSession | None = None
        self._tags: list[str] = []
        self._general: list[bool] = []

    def gender(self, image: bytes) -> str:
        return "female" if self._scores(image)["1girl"] >= FEMALE_THRESHOLD else "male"

    def appearance(self, image: bytes) -> list[str]:
        """Visible traits, most certain first, in words ("blonde hair", "twintails")."""
        scores = self._scores(image)
        kept = [
            tag
            for tag, general in zip(self._tags, self._general, strict=True)
            if general and scores[tag] >= APPEARANCE_THRESHOLD and not _NOT_APPEARANCE.match(tag)
        ]
        kept.sort(key=scores.__getitem__, reverse=True)
        key = [tag for tag in kept if _KEY_TRAIT.match(tag)]
        if not any(_HAIR_COLOR.match(tag) for tag in key):
            best = max(filter(_HAIR_COLOR.match, self._tags), key=scores.__getitem__)
            if scores[best] >= HAIR_COLOR_FALLBACK:
                key.insert(0, best)
        rest = [tag for tag in kept if tag not in key]
        chosen = key + rest[: max(APPEARANCE_LIMIT - len(key), 0)]
        chosen.sort(key=scores.__getitem__, reverse=True)
        return [tag.replace("_", " ") for tag in chosen]

    def _scores(self, image: bytes) -> dict[str, float]:
        session = self._load()
        model_input = session.get_inputs()[0]
        size = model_input.shape[1]
        scores = session.run(None, {model_input.name: _prepare(image, size)})[0][0]
        return dict(zip(self._tags, map(float, scores), strict=True))

    def _load(self) -> onnxruntime.InferenceSession:
        if self._session is not None:
            return self._session
        if not self.model_dir:
            raise TaggerUnavailable("CZN_TAGGER_DIR is not set")
        model_dir = Path(self.model_dir)
        try:
            _update(model_dir)
        except OSError as error:  # urllib errors are OSErrors
            if not (model_dir / "revision").exists():
                raise TaggerUnavailable(f"model download failed: {error!r}") from error
            log.warning("tagger update failed, using stored model", extra={"error": repr(error)})
        with (model_dir / "selected_tags.csv").open(newline="") as tags:
            rows = list(csv.DictReader(tags))
        self._tags = [row["name"] for row in rows]
        # Danbooru category 0 is general tags; the rest are ratings and named characters.
        self._general = [row["category"] == "0" for row in rows]
        self._session = onnxruntime.InferenceSession(
            model_dir / "model.onnx", providers=["CPUExecutionProvider"]
        )
        return self._session


def _update(model_dir: Path) -> None:
    """Make ``model_dir`` hold the newest revision of the model.

    Files are downloaded into a staging directory and moved into place one by one, with the
    ``revision`` marker written last, so an interrupted download is redone on the next run.
    """
    info = _get_json(f"{_HUB}/api/models/{MODEL_REPO}?blobs=true")
    revision = info["sha"]
    marker = model_dir / "revision"
    if marker.exists() and marker.read_text() == revision:
        return
    log.info("downloading tagger model", extra={"repo": MODEL_REPO, "revision": revision})
    files = {f["rfilename"]: f for f in info["siblings"]}
    staging = model_dir / ".download"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name in MODEL_FILES:
        expected = files[name].get("lfs", {}).get("sha256")
        url = f"{_HUB}/{MODEL_REPO}/resolve/{revision}/{name}"
        digest = hashlib.sha256()
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            (staging / name).open("wb") as out,
        ):
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        if expected and digest.hexdigest() != expected:
            raise OSError(f"{name}: SHA-256 {digest.hexdigest()} does not match {expected}")
    marker.unlink(missing_ok=True)
    for name in MODEL_FILES:
        (staging / name).replace(model_dir / name)
    marker.write_text(revision)
    staging.rmdir()


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _prepare(image: bytes, size: int) -> np.ndarray:
    """The model's input: the picture centered on a white square, resized, as BGR floats 0-255
    in one NHWC batch, as the WD14 reference code prepares it."""
    picture = Image.open(io.BytesIO(image)).convert("RGBA")
    side = max(picture.size)
    square = Image.new("RGB", (side, side), (255, 255, 255))
    square.paste(picture, ((side - picture.width) // 2, (side - picture.height) // 2), picture)
    square = square.resize((size, size), Image.Resampling.BICUBIC)
    pixels = np.asarray(square, dtype=np.float32)[:, :, ::-1]
    return np.expand_dims(np.ascontiguousarray(pixels), 0)
