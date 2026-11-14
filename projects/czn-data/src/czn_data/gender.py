"""Character gender from portrait art, because Prydwen does not state it.

The WD14 anime image tagger (SmilingWolf/wd-swinv2-tagger-v3, an ONNX model trained on Danbooru
tags) scores how likely a picture shows one girl (``1girl``). A portrait scoring at least
``FEMALE_THRESHOLD`` is female; every other portrait is male, so there is no unknown value.
Checked against Danbooru fan-art tags for the 41 characters of November 2026: no disagreement.

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
import shutil
import urllib.request
from pathlib import Path

import numpy as np
import onnxruntime
from PIL import Image

log = logging.getLogger(__name__)

FEMALE_THRESHOLD = 0.5
MODEL_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
MODEL_FILES = ("model.onnx", "selected_tags.csv")
_HUB = "https://huggingface.co"


class TaggerUnavailable(Exception):
    """The model could not be found or downloaded; no portrait can be tagged this run."""


class Tagger:
    def __init__(self, model_dir: str | None = None):
        self.model_dir = model_dir or os.environ.get("CZN_TAGGER_DIR")
        self._session: onnxruntime.InferenceSession | None = None
        self._girl_index = -1

    def gender(self, image: bytes) -> str:
        return "female" if self.female_score(image) >= FEMALE_THRESHOLD else "male"

    def female_score(self, image: bytes) -> float:
        session = self._load()
        model_input = session.get_inputs()[0]
        size = model_input.shape[1]
        scores = session.run(None, {model_input.name: _prepare(image, size)})[0][0]
        return float(scores[self._girl_index])

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
            names = [row["name"] for row in csv.DictReader(tags)]
        self._girl_index = names.index("1girl")
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
