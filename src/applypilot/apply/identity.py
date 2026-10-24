"""Conservative job identity helpers used to prevent duplicate applications."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit


_ID_QUERY_KEYS = {
    "gh_jid", "jobid", "job_id", "requisitionid", "requisition_id",
    "reqid", "req_id", "jobreqid", "job_req_id", "positionid", "position_id",
}


def clean_identifier(value: object) -> str | None:
    """Return a stable, printable external identifier, if one is present."""
    if value is None:
        return None
    text = str(value).strip()
    placeholder = re.sub(r"[^a-z0-9]+", "", text.casefold())
    if not text or placeholder in {
        "none", "nan", "nat", "na", "unknown", "notavailable", "unavailable",
        "noid", "norequisitionid", "null",
    }:
        return None
    return text[:160]


def extract_requisition_id(*values: object) -> str | None:
    """Extract only identifiers from well-known ATS fields or URL shapes."""
    for value in values:
        supplied = clean_identifier(value)
        if supplied and not urlsplit(supplied).scheme:
            return supplied
    for value in values:
        url = clean_identifier(value)
        if not url:
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        for key, item in parse_qsl(parts.query, keep_blank_values=False):
            if re.sub(r"[^a-z0-9_]", "", key.casefold()) in _ID_QUERY_KEYS and item.strip():
                return item.strip()[:160]
        path = parts.path.rstrip("/")
        # Workday URLs commonly append a requisition such as _JR123456.
        match = re.search(r"(?:^|[_/-])(JR\d{4,})(?:$|[_/-])", path, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        # Common BambooHR and Greenhouse detail URLs end in an exact numeric ID.
        if re.search(r"/(?:jobs?/detail|job|jobs)/\d{4,}$", path, re.IGNORECASE):
            return path.rsplit("/", 1)[-1]
        # Lever uses a UUID-like or long opaque posting identifier in its final path segment.
        if (parts.hostname or "").casefold().endswith("lever.co"):
            segment = path.rsplit("/", 1)[-1]
            if re.fullmatch(r"[a-z0-9-]{12,64}", segment, re.IGNORECASE):
                return segment
    return None


def normalize_identity_text(value: object) -> str:
    """Normalize human-readable identity fields for conservative comparison."""
    text = clean_identifier(value) or ""
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
