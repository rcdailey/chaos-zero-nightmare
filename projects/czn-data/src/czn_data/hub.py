"""Model files from Hugging Face, kept in a local directory across runs.

A directory holds one revision of a repository's chosen files, marked by a ``revision`` file.
Files are downloaded into a staging directory, checked against the SHA-256 Hugging Face lists for
them, and moved into place with the marker written last, so an interrupted download is redone on
the next call.
"""

import hashlib
import json
import logging
import shutil
import urllib.request
from pathlib import Path

import onnxruntime

log = logging.getLogger(__name__)

HUB = "https://huggingface.co"


class ModelUnavailable(Exception):
    """A model could not be found or downloaded; nothing that needs it can run this time."""


def newest_revision(repo: str) -> str:
    return get_json(f"{HUB}/api/models/{repo}")["sha"]


def stored_revision(model_dir: Path) -> str | None:
    marker = model_dir / "revision"
    return marker.read_text() if marker.exists() else None


def download(repo: str, revision: str, files: tuple[str, ...], model_dir: Path) -> None:
    """Make ``model_dir`` hold ``files`` of ``repo`` at ``revision``; raises OSError on failure.

    Each file keeps its path inside the repository."""
    if stored_revision(model_dir) == revision:
        return
    log.info("downloading model", extra={"repo": repo, "revision": revision})
    info = get_json(f"{HUB}/api/models/{repo}/revision/{revision}?blobs=true")
    hashes = {f["rfilename"]: f.get("lfs", {}).get("sha256") for f in info["siblings"]}
    staging = model_dir / ".download"
    shutil.rmtree(staging, ignore_errors=True)
    for name in files:
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        url = f"{HUB}/{repo}/resolve/{revision}/{name}"
        with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        expected = hashes.get(name)
        if expected and digest.hexdigest() != expected:
            raise OSError(f"{name}: SHA-256 {digest.hexdigest()} does not match {expected}")
    (model_dir / "revision").unlink(missing_ok=True)
    for name in files:
        (model_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (staging / name).replace(model_dir / name)
    (model_dir / "revision").write_text(revision)
    shutil.rmtree(staging)


def session(
    repo: str, revision: str, files: tuple[str, ...], name: str, root: str | None
) -> onnxruntime.InferenceSession:
    """An onnxruntime CPU session for ``name``, one of ``files`` of ``repo`` kept under
    ``root``/<repository name> and downloaded first when missing; raises ModelUnavailable."""
    if not root:
        raise ModelUnavailable("CZN_IDENTITY_DIR is not set")
    model_dir = Path(root) / repo.split("/")[1]
    try:
        download(repo, revision, files, model_dir)
    except OSError as error:  # urllib errors are OSErrors
        raise ModelUnavailable(f"model download failed: {error!r}") from error
    # onnxruntime's CPU memory arena keeps the most memory any run needed and grows again
    # for each new input size (figures per screenshot), so the server held about 1.7 GB
    # instead of about 650 MB with the search models loaded. Turning it off cost no
    # measurable time here and about 5% on a search.
    options = onnxruntime.SessionOptions()
    options.enable_cpu_mem_arena = False
    return onnxruntime.InferenceSession(
        model_dir / name, options, providers=["CPUExecutionProvider"]
    )


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)
