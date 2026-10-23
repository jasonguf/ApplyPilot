"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.llm_errors import LLMOutputError
from applypilot.provenance import input_fingerprint

log = logging.getLogger(__name__)
SCORE_PROMPT_VERSION = "score-v1"


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = None
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                if not 1 <= score <= 10:
                    raise ValueError("score must be from 1 to 10")
            except (AttributeError, ValueError):
                score = None
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    if score is None:
        raise LLMOutputError("ChatGPT returned a completed scoring response without a valid 1–10 score.")
    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def _score_input_hash(resume_text: str, job: dict, model: str) -> str:
    return input_fingerprint({
        "prompt_version": SCORE_PROMPT_VERSION,
        "model": model,
        "resume": resume_text,
        "job": {
            "title": job.get("title"),
            "site": job.get("site"),
            "location": job.get("location"),
            "full_description": job.get("full_description"),
        },
    })


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    client = get_client()
    response = client.chat(
        messages,
        request_key=f"score:{job.get('url', '')}",
        request_label=f"job score for {job.get('title', 'unknown job')}",
    )
    result = _parse_score_response(response)
    result.update({
        "model": client.model,
        "prompt_version": SCORE_PROMPT_VERSION,
        "input_hash": _score_input_hash(resume_text, job, client.model),
    })
    return result


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    client = get_client()

    # Invalidate only outputs that have provenance. Older results without a
    # fingerprint are kept intact because their inputs cannot be reconstructed.
    saved = conn.execute(
        "SELECT * FROM jobs WHERE fit_score IS NOT NULL AND score_input_hash IS NOT NULL"
    ).fetchall()
    stale_urls = []
    for row in saved:
        job = dict(row)
        if job["score_input_hash"] != _score_input_hash(resume_text, job, client.model):
            stale_urls.append(job["url"])
    if stale_urls:
        conn.executemany(
            "UPDATE jobs SET fit_score=NULL, score_reasoning=NULL, scored_at=NULL, "
            "score_model=NULL, score_prompt_version=NULL, score_input_hash=NULL WHERE url=?",
            [(url,) for url in stale_urls],
        )
        conn.commit()
        log.info("Marked %d saved scores stale after their inputs or model changed.", len(stale_urls))

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0

    for job in jobs:
        result = score_job(resume_text, job)
        completed += 1
        conn.execute(
            "UPDATE jobs SET fit_score=?, score_reasoning=?, scored_at=?, score_model=?, "
            "score_prompt_version=?, score_input_hash=? WHERE url=?",
            (
                result["score"],
                f"{result['keywords']}\n{result['reasoning']}",
                datetime.now(timezone.utc).isoformat(),
                result["model"],
                result["prompt_version"],
                result["input_hash"],
                job["url"],
            ),
        )
        conn.commit()
        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", completed, elapsed, completed / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": completed,
        "errors": 0,
        "elapsed": elapsed,
        "distribution": distribution,
    }
