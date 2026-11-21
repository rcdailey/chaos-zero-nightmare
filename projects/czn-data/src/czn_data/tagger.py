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
import io
import logging
import os
from pathlib import Path

import numpy as np
import onnxruntime
from PIL import Image

from . import hub

log = logging.getLogger(__name__)

FEMALE_THRESHOLD = 0.5
MODEL_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
MODEL_FILES = ("model.onnx", "selected_tags.csv")


class Tagger:
    def __init__(self, model_dir: str | None = None):
        self.model_dir = model_dir or os.environ.get("CZN_TAGGER_DIR")
        self._session: onnxruntime.InferenceSession | None = None
        self._tags: list[str] = []

    def gender(self, image: bytes) -> str:
        return "female" if self._scores(image)["1girl"] >= FEMALE_THRESHOLD else "male"

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
            raise hub.ModelUnavailable("CZN_TAGGER_DIR is not set")
        model_dir = Path(self.model_dir)
        try:
            hub.download(MODEL_REPO, hub.newest_revision(MODEL_REPO), MODEL_FILES, model_dir)
        except OSError as error:  # urllib errors are OSErrors
            if not hub.stored_revision(model_dir):
                raise hub.ModelUnavailable(f"model download failed: {error!r}") from error
            log.warning("tagger update failed, using stored model", extra={"error": repr(error)})
        with (model_dir / "selected_tags.csv").open(newline="") as tags:
            rows = list(csv.DictReader(tags))
        self._tags = [row["name"] for row in rows]
        self._session = onnxruntime.InferenceSession(
            model_dir / "model.onnx", providers=["CPUExecutionProvider"]
        )
        return self._session


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
