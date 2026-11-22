"""Which characters a game screenshot shows, by comparing them with the stored card art.

Two published deepghs models run on the CPU through onnxruntime:

- An anime person detector (YOLOv8, ``deepghs/anime_person_detection``) finds each character
  figure on the screen: the cards of a team or roster screen, or the small characters walking
  through an event screen.
- CCIP (``deepghs/ccip_onnx``), trained to tell whether two pictures show the same anime
  character, turns a picture into a print: a vector of 768 numbers. A second small model gives
  the difference of two prints; below ``SAME_CHARACTER`` they are the same character (the
  threshold published with the model, the best F1 on its test set).

The daily enrich stores one print per card art (``prydwen.portrait_identity``); a screenshot is
named by comparing each figure's print with every stored one. ``dghs-imgutils``, deepghs' own
library, cannot be installed with this project's Python and numpy, so the pre- and
post-processing here follows that library's code for these two models.

Models are pinned to one revision so the prints stored by the job and the server's screenshot
prints always come from the same weights. They are downloaded on first use into
``CZN_IDENTITY_DIR`` (about 250 MB).
"""

import io
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import onnxruntime
from PIL import Image

from . import hub

log = logging.getLogger(__name__)

MODEL = "ccip-caformer-24-randaug-pruned"
_CCIP_REPO = "deepghs/ccip_onnx"
_CCIP_REVISION = "eb2acdd29af1703388d3d0c04221add322bc9110"
_CCIP_FILES = (f"{MODEL}/model_feat.onnx", f"{MODEL}/model_metrics.onnx")
_DETECTOR_REPO = "deepghs/anime_person_detection"
_DETECTOR_REVISION = "e39c744c22432ad01f91dd254fe2b02c8d878b8c"
_DETECTOR_FILES = ("person_detect_v1.1_m/model.onnx",)

# Detector confidence a figure needs; at 0.35 a team screen's monster icon (0.57) still counts
# as a figure and is then reported unknown, which beats losing a real character.
FIGURE_CONFIDENCE = 0.35
_OVERLAP = 0.5  # boxes overlapping more than this are one figure
# CCIP difference below which two prints show the same character (deepghs' published threshold).
SAME_CHARACTER = 0.17847511429108218
# When the runner-up is this close to the best match, the figure could be either. On the
# November 2026 screenshots every right match led by at least 0.06; the one wrong match (a
# chibi) led by 0.02.
TOO_CLOSE = 0.05
_PRINT_SIZE = 384
_CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)[:, None, None]
_CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)[:, None, None]


@dataclass(frozen=True)
class Figure:
    """One character figure on a screenshot.

    ``result`` is ``match`` (``names`` holds the one character), ``close`` (``names`` holds the
    best two, too close to choose between), or ``unknown`` (no stored character matches, such as
    a monster or a picture too small to compare; ``names`` is empty). ``differences`` are the
    best CCIP differences, smallest first, for the candidates in ``candidates``.
    """

    box: tuple[int, int, int, int]
    result: Literal["match", "close", "unknown"]
    names: list[str]
    candidates: list[str]
    differences: list[float]


class Identifier:
    """Loads the models once and keeps them for every later call; not thread-safe to load."""

    def __init__(self, model_dir: str | None = None):
        self.model_dir = model_dir or os.environ.get("CZN_IDENTITY_DIR")
        self._sessions: dict[str, onnxruntime.InferenceSession] = {}

    def print_of(self, image: bytes) -> list[float]:
        """The print of a picture showing one character, such as card art."""
        return self._prints([_open(image)])[0].tolist()

    def identify(self, image: bytes, prints: Mapping[str, list[float]]) -> list[Figure]:
        """Every character figure on a screenshot, top row first and left to right in a row,
        named from ``prints`` (character name to stored print)."""
        picture = _open(image)
        boxes = self._detect(picture)
        if not boxes:
            return []
        names = list(prints)
        stored = np.asarray([prints[name] for name in names], dtype=np.float32)
        found = self._prints([picture.crop(box) for box in boxes])
        figures = []
        for box, figure_print in zip(boxes, found, strict=True):
            differences = self._differences(figure_print, stored) if names else np.empty(0)
            order = np.argsort(differences)[:3]
            candidates = [names[i] for i in order]
            best = [float(differences[i]) for i in order]
            if not best or best[0] >= SAME_CHARACTER:
                result, named = "unknown", []
            elif len(best) > 1 and best[1] - best[0] < TOO_CLOSE:
                result, named = "close", candidates[:2]
            else:
                result, named = "match", candidates[:1]
            figures.append(Figure(box, result, named, candidates, best))
        return _reading_order(figures)

    def _detect(self, picture: Image.Image) -> list[tuple[int, int, int, int]]:
        session = self._session(_DETECTOR_REPO, _DETECTOR_REVISION, _DETECTOR_FILES[0])
        # The export states its input size in metadata ("[height, width]"); deepghs' library
        # falls back to 640 x 640 without it.
        size = session.get_modelmeta().custom_metadata_map.get("imgsz", "[640, 640]")
        height, width = json.loads(size)
        resized = picture.resize((width, height))  # the detector was trained on stretched input
        data = (np.asarray(resized, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None]
        output = session.run(["output0"], {"images": data})[0][0]  # (4 + classes, candidates)
        scores = output[4:].max(axis=0)
        keep = scores > FIGURE_CONFIDENCE
        cx, cy, w, h = output[:4, keep]
        boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
        kept = _suppress_overlaps(boxes, scores[keep])
        scale = np.array([picture.width / width, picture.height / height] * 2)
        limit = np.array([picture.width, picture.height] * 2)
        return [tuple(int(v) for v in np.clip(boxes[i] * scale, 0, limit).round()) for i in kept]

    def _prints(self, pictures: list[Image.Image]) -> np.ndarray:
        session = self._session(_CCIP_REPO, _CCIP_REVISION, _CCIP_FILES[0])
        data = np.stack([_ccip_input(picture) for picture in pictures])
        return session.run(["output"], {"input": data})[0]

    def _differences(self, figure_print: np.ndarray, stored: np.ndarray) -> np.ndarray:
        """CCIP difference of one print with each stored print; the model compares a batch of
        prints all against all, so only the first row is kept."""
        session = self._session(_CCIP_REPO, _CCIP_REVISION, _CCIP_FILES[1])
        batch = np.concatenate([figure_print[None], stored]).astype(np.float32)
        return session.run(["output"], {"input": batch})[0][0, 1:]

    def _session(self, repo: str, revision: str, name: str) -> onnxruntime.InferenceSession:
        if name in self._sessions:
            return self._sessions[name]
        if not self.model_dir:
            raise hub.ModelUnavailable("CZN_IDENTITY_DIR is not set")
        model_dir = Path(self.model_dir) / repo.split("/")[1]
        files = _CCIP_FILES if repo == _CCIP_REPO else _DETECTOR_FILES
        try:
            hub.download(repo, revision, files, model_dir)
        except OSError as error:  # urllib errors are OSErrors
            raise hub.ModelUnavailable(f"model download failed: {error!r}") from error
        # onnxruntime's CPU memory arena keeps the most memory any run needed and grows again
        # for each new input size (figures per screenshot), so the server held about 1.7 GB
        # instead of about 650 MB with the search models loaded. Turning it off cost no
        # measurable time here and about 5% on a search.
        options = onnxruntime.SessionOptions()
        options.enable_cpu_mem_arena = False
        session = onnxruntime.InferenceSession(
            model_dir / name, options, providers=["CPUExecutionProvider"]
        )
        self._sessions[name] = session
        return session


def download(model_dir: str) -> None:
    """Store both models in ``model_dir``, for building an image that never downloads."""
    for repo, revision, files in (
        (_CCIP_REPO, _CCIP_REVISION, _CCIP_FILES),
        (_DETECTOR_REPO, _DETECTOR_REVISION, _DETECTOR_FILES),
    ):
        hub.download(repo, revision, files, Path(model_dir) / repo.split("/")[1])


def _open(image: bytes) -> Image.Image:
    """The picture in RGB, with transparent parts on white as deepghs' library loads them."""
    picture = Image.open(io.BytesIO(image))
    if picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info:
        rgba = picture.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        white.alpha_composite(rgba)
        return white.convert("RGB")
    return picture.convert("RGB")


def _ccip_input(picture: Image.Image) -> np.ndarray:
    resized = picture.resize((_PRINT_SIZE, _PRINT_SIZE), Image.Resampling.BILINEAR)
    data = np.asarray(resized, dtype=np.float32).transpose(2, 0, 1) / 255.0
    return ((data - _CLIP_MEAN) / _CLIP_STD).astype(np.float32)


def _suppress_overlaps(boxes: np.ndarray, scores: np.ndarray) -> list[int]:
    """Indexes of the boxes kept, most confident first, dropping any box that overlaps a more
    confident one by more than ``_OVERLAP`` (intersection over union)."""
    x0, y0, x1, y1 = boxes.T
    areas = (x1 - x0 + 1) * (y1 - y0 + 1)
    order = scores.argsort()[::-1]
    kept = []
    while order.size:
        best, rest = order[0], order[1:]
        kept.append(int(best))
        width = np.maximum(0.0, np.minimum(x1[best], x1[rest]) - np.maximum(x0[best], x0[rest]) + 1)
        height = np.maximum(
            0.0, np.minimum(y1[best], y1[rest]) - np.maximum(y0[best], y0[rest]) + 1
        )
        overlap = width * height / (areas[best] + areas[rest] - width * height)
        order = rest[overlap <= _OVERLAP]
    return kept


def _reading_order(figures: list[Figure]) -> list[Figure]:
    """Rows from the top, left to right within a row. A figure joins the current row when its
    height overlaps the row's by at least half its own height, so a small icon beside tall
    cards stays in their row even when it sits above their middle."""
    figures = sorted(figures, key=lambda f: f.box[1])
    rows: list[list[Figure]] = []
    row_top = row_bottom = 0
    for figure in figures:
        top, bottom = figure.box[1], figure.box[3]
        overlap = min(bottom, row_bottom) - max(top, row_top)
        if rows and overlap >= (bottom - top) / 2:
            rows[-1].append(figure)
            row_top, row_bottom = min(row_top, top), max(row_bottom, bottom)
        else:
            rows.append([figure])
            row_top, row_bottom = top, bottom
    return [figure for row in rows for figure in sorted(row, key=lambda f: f.box[0])]
