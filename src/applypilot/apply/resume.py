"""Prepare a stable, locally staged copy of the applicant's base resume."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from applypilot import config


@dataclass(frozen=True)
class ResumeSnapshot:
    pdf_path: Path
    text: str
    sha256: str


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except Exception as exc:
        raise ValueError(f"Could not read resume PDF {path}: {exc}") from exc
    if not text.strip():
        raise ValueError(
            "The resume PDF has no extractable text. OCR it or replace it with a text-based PDF; "
            "ApplyPilot cannot verify that a separate text file matches a scanned PDF."
        )
    return text.strip()


def _normalize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _check_companion_text(pdf_text: str, text_path: Path) -> str:
    text = text_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Resume text file is empty: {text_path}")
    pdf_tokens, text_tokens = _normalize(pdf_text), _normalize(text)
    similarity = SequenceMatcher(None, pdf_tokens, text_tokens, autojunk=False).ratio()
    if similarity < 0.55:
        raise ValueError(
            "resume.pdf and resume.txt appear to contain different resume versions. "
            "Make them match before using base-resume mode."
        )
    return text


def prepare_base_resume_snapshot(snapshot_dir: Path) -> ResumeSnapshot:
    """Create and validate a per-run PDF/text snapshot without changing source files."""
    snapshot_dir = Path(snapshot_dir).resolve()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(snapshot_dir, 0o700)
    staged_pdf = snapshot_dir / "base-resume.pdf"

    if config.RESUME_PDF_PATH.exists():
        source_pdf = config.RESUME_PDF_PATH
        with source_pdf.open("rb") as source_file:
            signature = source_file.read(5)
        if source_pdf.stat().st_size < 8 or signature != b"%PDF-":
            raise ValueError(f"Resume file is not a valid PDF: {source_pdf}")
        pdf_text = _extract_pdf_text(source_pdf)
        text = _check_companion_text(pdf_text, config.RESUME_PATH) if config.RESUME_PATH.exists() else pdf_text
        shutil.copy2(source_pdf, staged_pdf)
    elif config.RESUME_PATH.exists():
        text = config.RESUME_PATH.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Resume text file is empty: {config.RESUME_PATH}")
        from applypilot.scoring.pdf import convert_to_pdf
        try:
            convert_to_pdf(config.RESUME_PATH, staged_pdf)
        except Exception as exc:
            raise ValueError(
                "Could not render resume.txt to PDF. Provide resume.pdf or install the Playwright Chromium browser."
            ) from exc
        pdf_text = _extract_pdf_text(staged_pdf)
        _check_companion_text(pdf_text, config.RESUME_PATH)
    else:
        raise ValueError(
            f"No base resume found. Add {config.RESUME_PDF_PATH} or {config.RESUME_PATH}."
        )

    if not staged_pdf.exists() or staged_pdf.stat().st_size < 8:
        raise ValueError("Base resume PDF generation failed or produced an empty file.")
    if os.name != "nt":
        os.chmod(staged_pdf, 0o600)
    digest_state = hashlib.sha256()
    with staged_pdf.open("rb") as staged_file:
        for chunk in iter(lambda: staged_file.read(1024 * 1024), b""):
            digest_state.update(chunk)
    digest = digest_state.hexdigest()
    return ResumeSnapshot(pdf_path=staged_pdf, text=text, sha256=digest)
