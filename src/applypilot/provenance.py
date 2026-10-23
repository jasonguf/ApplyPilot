"""Stable fingerprints for generated AI artifacts."""

import hashlib
import json
from typing import Any


def input_fingerprint(payload: Any) -> str:
    """Hash a canonical JSON representation without persisting prompt contents."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
