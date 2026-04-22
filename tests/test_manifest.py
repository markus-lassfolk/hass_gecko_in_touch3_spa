"""Validate integration metadata."""

from __future__ import annotations

import json
from pathlib import Path


def test_manifest_json() -> None:
    """manifest.json must be valid JSON with required Home Assistant keys."""
    manifest_path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "gecko"
        / "manifest.json"
    )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("domain", "name", "version", "documentation", "requirements"):
        assert key in data, f"manifest missing {key}"
    assert data["domain"] == "gecko"
    assert isinstance(data["requirements"], list)
    assert len(data["requirements"]) >= 1
