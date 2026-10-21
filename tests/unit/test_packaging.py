"""Ensure installed wire validators match the reviewed repository contracts."""

from pathlib import Path

from compute_broker.domain.validation import validators


def test_packaged_schemas_match_reviewed_contracts():
    root = Path(__file__).resolve().parents[2]
    for filename in validators():
        assert (root / "contracts" / filename).read_bytes() == (root / "src/compute_broker/schemas" / filename).read_bytes()
