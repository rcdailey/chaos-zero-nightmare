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


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)
