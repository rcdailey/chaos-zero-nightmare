"""Contract: the portrait tagger calls female art female and everything else male.

Runs the real model from Hugging Face, downloaded into CZN_TAGGER_DIR on first use (mise sets
it); skipped when it is not set.
"""

import os
from pathlib import Path

import pytest

from czn_data.tagger import Tagger

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def tagger():
    if not os.environ.get("CZN_TAGGER_DIR"):
        pytest.skip("CZN_TAGGER_DIR not set; run tests through mise")
    return Tagger()


@pytest.mark.parametrize(
    ("portrait", "gender"),
    [("portrait-female.webp", "female"), ("portrait-male.webp", "male")],
)
def test_portrait_gender(tagger, portrait, gender):
    assert tagger.gender((FIXTURES / portrait).read_bytes()) == gender
