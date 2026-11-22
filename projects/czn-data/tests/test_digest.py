"""Contract: a step's code fingerprint changes when its own source or the source of a czn_data
module it imports changes, and stays the same otherwise, so steps reprocess after code edits.

The step modules are written to a temporary directory added to the czn_data package.
"""

import czn_data
from czn_data import digest


def test_code_fingerprint_follows_edits_to_the_step_and_its_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(czn_data, "__path__", [*czn_data.__path__, str(tmp_path)])
    step = tmp_path / "_probe_step.py"
    helper = tmp_path / "_probe_helper.py"
    step.write_text("from ._probe_helper import split\n")
    helper.write_text("def split(text):\n    return text.split()\n")

    first = digest.code("czn_data._probe_step")
    assert digest.code("czn_data._probe_step") == first

    helper.write_text("def split(text):\n    return text.split(',')\n")
    after_import_edit = digest.code("czn_data._probe_step")
    assert after_import_edit != first

    step.write_text("from ._probe_helper import split  # comment\n")
    assert digest.code("czn_data._probe_step") != after_import_edit
