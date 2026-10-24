"""Apply orchestration: acquire jobs, run isolated Codex sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches an isolated Chrome + Codex App Server session,
parses the result, and updates the database.
"""

import atexit
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from filelock import FileLock, Timeout
from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply.codex_runtime import (
    CodexAppServer, CodexRuntimeError, resolve_model, PLAYWRIGHT_MCP_VERSION,
)
from applypilot.apply.identity import extract_requisition_id, normalize_identity_text
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track only Codex App Server processes started by this ApplyPilot process.
_codex_procs: dict[int, subprocess.Popen] = {}
_codex_lock = threading.Lock()
_domain_lock = threading.Lock()
_domain_next_start: dict[str, float] = {}

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
def _reserve_domain_slot(url: str, cooldown_seconds: float) -> float:
    """Reserve a per-domain start time and return the required wait."""
    host = (urlparse(url).hostname or "unknown").casefold()
    now = time.monotonic()
    with _domain_lock:
        start_at = max(now, _domain_next_start.get(host, now))
        _domain_next_start[host] = start_at + max(0.0, cooldown_seconds)
    return max(0.0, start_at - now)


def _recover_interrupted_attempts() -> int:
    """With the run lock held, mark leftovers as uncertain instead of retrying."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT url, apply_phase FROM jobs WHERE apply_status = 'in_progress'"
    ).fetchall()
    if not rows:
        return 0
    for row in rows:
        is_handoff = row["apply_phase"] == "auth_handoff"
        status = "awaiting_user" if is_handoff else "uncertain"
        error = (
            "Interactive handoff was interrupted. Run `applypilot apply --resume` to continue."
            if is_handoff else "Previous run ended before the outcome was confirmed."
        )
        conn.execute("""
            UPDATE jobs SET apply_status = ?, apply_error = ?, apply_phase = NULL, agent_id = NULL
            WHERE url = ?
        """, (status, error, row["url"]))
        conn.execute("""
            UPDATE apply_attempts SET finished_at = ?, outcome = ?, error = ?
            WHERE finished_at IS NULL AND job_url = ?
        """, (now, status, error, row["url"]))
    conn.commit()
    return len(urls)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _candidate_rows(conn, target_url: str | None, min_score: int | None,
                    resume_mode: str, limit: int = 100,
                    max_age_days: int | None = 30) -> list[dict]:
    """Select eligible jobs using the same rules for preview and live runs."""
    if resume_mode not in {"base", "tailored"}:
        raise ValueError("resume_mode must be 'base' or 'tailored'")

    conditions = ["applied_at IS NULL", "(apply_status IS NULL OR apply_status = 'failed')",
                  "(apply_attempts IS NULL OR apply_attempts < ?)",
                  "title IS NOT NULL AND trim(title) <> ''",
                  "COALESCE(NULLIF(application_url, ''), url) <> ''",
                  "NOT EXISTS (SELECT 1 FROM jobs AS prior "
                  "WHERE prior.url <> jobs.url "
                  "AND COALESCE(NULLIF(prior.application_url, ''), prior.url) = "
                  "COALESCE(NULLIF(jobs.application_url, ''), jobs.url) "
                  "AND (prior.applied_at IS NOT NULL OR prior.apply_status IN "
                  "('in_progress', 'applied', 'needs_review', 'awaiting_user', 'uncertain', 'previewed', 'manual', 'expired', 'captcha')))"]
    params: list = [config.DEFAULTS["max_apply_attempts"]]
    if resume_mode == "tailored":
        conditions.append("tailored_resume_path IS NOT NULL")
        if min_score is not None:
            conditions.append("fit_score >= ?")
            params.append(min_score)
    if target_url:
        # Exact URL matching avoids a partial LIKE match selecting the wrong job.
        conditions.append("(url = ? OR application_url = ?)")
        params.extend([target_url, target_url])
    if max_age_days is not None:
        conditions.append("(posted_at IS NULL OR date(posted_at) >= date('now', ?))")
        params.append(f"-{max_age_days} days")

    blocked_sites, blocked_patterns = _load_blocked()
    if blocked_sites:
        conditions.append("(site IS NULL OR site NOT IN (" + ",".join("?" * len(blocked_sites)) + "))")
        params.extend(blocked_sites)
    for pattern in blocked_patterns:
        conditions.append("url NOT LIKE ?")
        params.append(pattern)

    order = (
        "CASE WHEN posted_at IS NULL THEN 1 ELSE 0 END, posted_at DESC, "
        "COALESCE(fit_score, -1) DESC, discovered_at DESC, url"
    ) if resume_mode == "base" else "fit_score DESC, discovered_at DESC, url"
    params.append(limit)
    rows = conn.execute(f"""
        SELECT url, title, company, requisition_id, site, application_url, tailored_resume_path,
               fit_score, location, full_description, description, cover_letter_path,
               posted_at, employment_type, remote_type
        FROM jobs
        WHERE {' AND '.join(conditions)}
        ORDER BY {order}
        LIMIT ?
    """, params).fetchall()
    candidates = [dict(row) for row in rows]
    prior_rows = conn.execute("""
        SELECT url, title, company, requisition_id, location, application_url,
               applied_at, apply_status
        FROM jobs
        WHERE applied_at IS NOT NULL OR apply_status IN
              ('applied', 'in_progress', 'needs_review', 'awaiting_user', 'uncertain', 'previewed', 'manual')
    """).fetchall()
    prior_jobs = [dict(row) for row in prior_rows]
    eligible: list[dict] = []
    for candidate in candidates:
        candidate_company = normalize_identity_text(candidate.get("company"))
        candidate_title = normalize_identity_text(candidate.get("title"))
        candidate_location = normalize_identity_text(candidate.get("location"))
        candidate_req = extract_requisition_id(
            candidate.get("requisition_id"), candidate.get("application_url"), candidate.get("url"),
        )
        strong_duplicate = False
        soft_duplicate = False
        for prior in prior_jobs:
            if prior.get("url") == candidate.get("url"):
                continue
            prior_company = normalize_identity_text(prior.get("company"))
            prior_req = extract_requisition_id(
                prior.get("requisition_id"), prior.get("application_url"), prior.get("url"),
            )
            if (candidate_company and prior_company == candidate_company
                    and candidate_req and prior_req == candidate_req):
                strong_duplicate = True
                break
            prior_title = normalize_identity_text(prior.get("title"))
            prior_location = normalize_identity_text(prior.get("location"))
            same_location = (
                not candidate_location or not prior_location or candidate_location == prior_location
            )
            if (candidate_company and prior_company == candidate_company
                    and candidate_title and prior_title == candidate_title and same_location):
                soft_duplicate = True
        if strong_duplicate:
            continue
        if soft_duplicate:
            candidate["duplicate_review"] = "possible_duplicate"
        eligible.append(candidate)
    return eligible


def list_ready_jobs(target_url: str | None = None, min_score: int | None = None,
                    resume_mode: str = "base", limit: int = 25,
                    max_age_days: int | None = 30) -> list[dict]:
    """Return eligible jobs for a local preview. Does not claim or mutate jobs."""
    candidate_limit = min(max(limit * 10, limit), 10000)
    candidates = _candidate_rows(get_connection(), target_url, min_score, resume_mode,
                                 candidate_limit, max_age_days)
    from applypilot.config import is_manual_ats
    eligible = [job for job in candidates
                if (urlparse(job.get("application_url") or job["url"]).scheme in {"http", "https"}
                    and urlparse(job.get("application_url") or job["url"]).hostname
                    and not is_manual_ats(job.get("application_url") or job["url"]))]
    if resume_mode == "tailored":
        eligible = [job for job in eligible
                    if job.get("tailored_resume_path")
                    and Path(job["tailored_resume_path"]).with_suffix(".txt").is_file()
                    and Path(job["tailored_resume_path"]).with_suffix(".pdf").is_file()]
    return eligible[:limit]


def acquire_job(target_url: str | None = None, min_score: int | None = None,
                worker_id: int = 0, resume_mode: str = "base",
                run_id: str = "manual", resume_sha256: str = "",
                model: str | None = None, account_fingerprint: str | None = None,
                profile_sha256: str | None = None,
                dry_run: bool = False,
                eligible_urls: set[str] | None = None,
                job_snapshots: dict[str, dict] | None = None,
                max_age_days: int | None = 30) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        candidates = _candidate_rows(conn, target_url, min_score, resume_mode,
                                     limit=10000, max_age_days=max_age_days)
        if eligible_urls is not None:
            candidates = [candidate for candidate in candidates if candidate["url"] in eligible_urls]
        if not candidates:
            conn.rollback()
            return None

        # Manual-only ATS entries are marked and skipped without starving the rest
        # of the queue. A specific URL request still stops on that exact job.
        from applypilot.config import is_manual_ats
        row = None
        for candidate in candidates:
            if job_snapshots and candidate["url"] in job_snapshots:
                candidate.update(job_snapshots[candidate["url"]])
            apply_url = candidate.get("application_url") or candidate["url"]
            parsed_url = urlparse(apply_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                conn.execute(
                    "UPDATE jobs SET apply_status = 'failed', apply_error = 'invalid_application_url', "
                    "apply_attempts = 99 WHERE url = ?",
                    (candidate["url"],),
                )
                continue
            if resume_mode == "tailored":
                resume_path = candidate.get("tailored_resume_path")
                if (not resume_path
                        or not Path(resume_path).with_suffix(".txt").is_file()
                        or not Path(resume_path).with_suffix(".pdf").is_file()):
                    conn.execute(
                        "UPDATE jobs SET apply_status = 'failed', apply_error = 'missing_tailored_resume', "
                        "apply_attempts = 99 WHERE url = ?",
                        (candidate["url"],),
                    )
                    continue
            if candidate.get("duplicate_review"):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'needs_review', apply_error = 'possible_duplicate', "
                    "agent_id = NULL WHERE url = ?",
                    (candidate["url"],),
                )
                logger.info("Marking possible duplicate for manual review.")
                if target_url:
                    conn.commit()
                    return None
                continue
            if is_manual_ats(apply_url):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                    (candidate["url"],),
                )
                logger.info("Skipping a manual-only ATS entry.")
                if target_url:
                    conn.commit()
                    return None
                continue
            row = candidate
            break

        if not row:
            conn.commit()
            return None

        if job_snapshots and row["url"] in job_snapshots:
            row.update(job_snapshots[row["url"]])

        now = datetime.now(timezone.utc).isoformat()
        attempt_id = str(uuid4())
        selected_resume_hash = resume_sha256
        if resume_mode == "tailored":
            selected_pdf = Path(row["tailored_resume_path"]).with_suffix(".pdf")
            digest = hashlib.sha256()
            with selected_pdf.open("rb") as selected_file:
                for chunk in iter(lambda: selected_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            selected_resume_hash = digest.hexdigest()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?,
                           apply_resume_mode = ?,
                           apply_resume_sha256 = ?, apply_phase = 'application'
            WHERE url = ?
        """, (f"worker-{worker_id}", now, resume_mode, selected_resume_hash, row["url"]))
        conn.execute("""
            INSERT INTO apply_attempts
                (id, run_id, job_url, resume_mode, resume_sha256, started_at,
                 model, account_fingerprint, profile_sha256, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (attempt_id, run_id, row["url"], resume_mode, selected_resume_hash, now,
              model, account_fingerprint, profile_sha256, int(dry_run)))
        conn.commit()

        row["apply_attempt_id"] = attempt_id
        row["apply_resume_mode"] = resume_mode
        row["apply_resume_sha256"] = selected_resume_hash
        return row
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None, attempt_id: str | None = None,
                verification_evidence: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?, apply_phase = NULL
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    elif status == "failed":
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL, apply_phase = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = ?, apply_error = ?, agent_id = NULL, apply_phase = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error, duration_ms, task_id, url))
    if attempt_id:
        conn.execute("""
            UPDATE apply_attempts SET finished_at = ?, outcome = ?, error = ?,
                                      verification_evidence = ? WHERE id = ?
        """, (now, status, error, verification_evidence, attempt_id))
    conn.commit()


def release_lock(url: str, attempt_id: str | None = None, outcome: str = "abandoned") -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL, apply_phase = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    if attempt_id:
        conn.execute("""
            UPDATE apply_attempts SET finished_at = ?, outcome = ? WHERE id = ?
        """, (datetime.now(timezone.utc).isoformat(), outcome, attempt_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               resume_mode: str = "tailored",
               max_age_days: int | None = 30) -> Path | None:
    """Generate a local prompt for manual inspection; does not claim the job."""
    config.ensure_dirs()
    candidates = list_ready_jobs(
        target_url=target_url,
        min_score=min_score if resume_mode == "tailored" else None,
        resume_mode=resume_mode,
        limit=1,
        max_age_days=max_age_days,
    )
    if not candidates:
        return None
    job = candidates[0]
    run_dir = config.APPLY_RUN_DIR / f"gen-{uuid4().hex}"
    worker_dir = run_dir / "inspection-worker"
    snapshot = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        worker_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(run_dir, 0o700)
            os.chmod(worker_dir, 0o700)
        if resume_mode == "base":
            from applypilot.apply.resume import prepare_base_resume_snapshot
            snapshot = prepare_base_resume_snapshot(run_dir)
            Console().print(f"Base resume snapshot SHA-256: {snapshot.sha256}")
            resume_text = snapshot.text
            resume_pdf = snapshot.pdf_path
        else:
            resume_path = job.get("tailored_resume_path")
            if not resume_path:
                return None
            text_path = Path(resume_path).with_suffix(".txt")
            if not text_path.is_file():
                return None
            resume_text = text_path.read_text(encoding="utf-8").strip()
            resume_pdf = Path(resume_path).with_suffix(".pdf")
        prompt_text = prompt_mod.build_prompt(
            job=job, tailored_resume=resume_text, resume_mode=resume_mode,
            resume_pdf_path=resume_pdf, worker_dir=worker_dir,
        )
        site_slug = re.sub(r"[^a-z0-9_-]+", "_", (job.get("site") or "unknown").casefold())[:20]
        title_slug = re.sub(r"[^a-z0-9_-]+", "_", (job.get("title") or "job").casefold())[:40]
        prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{title_slug}_{uuid4().hex[:8]}.txt"
        prompt_file.write_text(prompt_text, encoding="utf-8")
        if os.name != "nt":
            os.chmod(prompt_file, 0o600)
        return prompt_file
    finally:
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)

def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL, apply_phase = NULL
            WHERE url = ? OR application_url = ?
        """, (now, url, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL, apply_phase = NULL
            WHERE url = ? OR application_url = ?
        """, (reason or "manual", url, url))
    if conn.execute("SELECT changes()").fetchone()[0] == 0:
        raise ValueError(f"No job matched URL: {url}")
    conn.execute("""
        UPDATE apply_attempts SET resolved_at = ?, resolution = ?
        WHERE id = (
            SELECT id FROM apply_attempts
            WHERE job_url IN (SELECT url FROM jobs WHERE url = ? OR application_url = ?)
            ORDER BY started_at DESC LIMIT 1
        )
    """, (now, status, url, url))
    conn.commit()


def list_waiting_jobs() -> list[dict]:
    """Return durable employer handoffs, recovering interrupted handoff windows first."""
    config.ensure_dirs()
    from applypilot.database import init_db
    init_db()
    lock = FileLock(str(config.APP_DIR / "apply.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError("Another ApplyPilot apply run is active for this profile.") from exc
    try:
        _recover_interrupted_attempts()
        rows = get_connection().execute("""
            SELECT url, title, company, site, location, requisition_id,
                   apply_error, application_url,
                   apply_resume_mode, apply_resume_sha256, tailored_resume_path,
                   last_attempted_at
            FROM jobs WHERE apply_status = 'awaiting_user'
            ORDER BY last_attempted_at DESC, title
            LIMIT 100
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        lock.release()


def resume_waiting_job(job_url: str) -> None:
    """Resume one saved handoff with a strict, visible manual-credential boundary."""
    from rich.prompt import Confirm, Prompt

    config.ensure_dirs()
    from applypilot.database import init_db
    init_db()
    lock = FileLock(str(config.APP_DIR / "apply.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError("Another ApplyPilot apply run is active for this profile.") from exc

    console = Console()
    run_id = uuid4().hex
    run_dir = config.APPLY_RUN_DIR / f"resume-{run_id}"
    chrome_proc = None
    attempt_id = None
    job = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(run_dir, 0o700)
        _recover_interrupted_attempts()
        config.check_auto_apply_dependencies()
        conn = get_connection()
        row = conn.execute("""
            SELECT * FROM jobs WHERE url = ? AND apply_status = 'awaiting_user'
        """, (job_url,)).fetchone()
        if not row:
            raise ValueError("This job is no longer waiting for you. Run `applypilot apply --resume` to refresh the list.")
        job = dict(row)
        latest = conn.execute("""
            SELECT model, account_fingerprint, profile_sha256, resume_sha256,
                   resume_mode, dry_run
            FROM apply_attempts WHERE job_url = ?
            ORDER BY started_at DESC LIMIT 1
        """, (job_url,)).fetchone()
        if not latest:
            raise ValueError("No saved application attempt exists for this handoff; review it manually.")

        from applypilot.siwc import ChatGPTAuth
        auth = ChatGPTAuth()
        client_id = auth.active_client_id()
        if not client_id or not auth.has_active_account():
            raise RuntimeError("Connect the same ChatGPT account used by this application with `applypilot auth login`.")
        current_fingerprint = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]
        if latest["account_fingerprint"] and latest["account_fingerprint"] != current_fingerprint:
            raise RuntimeError("The active ChatGPT account differs from the account that started this job. Select the original account and retry.")
        token = auth.get_access_token(
            client_id,
            min_ttl_seconds=config.DEFAULTS["apply_timeout"] + 120,
        )
        model = resolve_model(auth, client_id, latest["model"])

        profile_snapshot = json.loads(json.dumps(config.load_profile(), ensure_ascii=False))
        search_snapshot = json.loads(json.dumps(config.load_search_config(), ensure_ascii=False))
        profile_sha256 = hashlib.sha256(
            json.dumps(profile_snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if latest["profile_sha256"] and latest["profile_sha256"] != profile_sha256:
            if not Confirm.ask("Your applicant profile changed since this job was queued. Continue with the current profile?", default=False):
                console.print("Handoff remains saved. No changes were made.")
                return

        resume_mode = job.get("apply_resume_mode") or latest["resume_mode"] or "base"
        if resume_mode not in {"base", "tailored"}:
            raise ValueError("Saved resume mode is invalid; review this handoff manually.")
        snapshot = None
        if resume_mode == "base":
            from applypilot.apply.resume import prepare_base_resume_snapshot
            snapshot = prepare_base_resume_snapshot(run_dir)
            resume_sha256 = snapshot.sha256
        else:
            resume_path = job.get("tailored_resume_path")
            if not resume_path:
                raise ValueError("The saved tailored resume is missing; review this handoff manually.")
            resume_pdf = Path(resume_path).with_suffix(".pdf")
            resume_txt = Path(resume_path).with_suffix(".txt")
            if not resume_pdf.is_file() or not resume_txt.is_file():
                raise ValueError("The saved tailored resume files are missing; review this handoff manually.")
            resume_sha256 = _sha256_file(resume_pdf)
        if latest["resume_sha256"] and latest["resume_sha256"] != resume_sha256:
            if not Confirm.ask("The resume file changed since this job was queued. Continue with the current resume?", default=False):
                console.print("Handoff remains saved. No changes were made.")
                return

        apply_url = job.get("application_url") or job["url"]
        try:
            parsed_url = urlparse(apply_url)
            parsed_url.port
        except ValueError as exc:
            raise ValueError("The saved application URL has an invalid port; review this handoff manually.") from exc
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise ValueError("The saved application URL is invalid; review this handoff manually.")
        dry_run = bool(latest["dry_run"])
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress', apply_phase = 'auth_handoff',
                            apply_error = NULL, agent_id = 'interactive-handoff',
                            last_attempted_at = ?, apply_resume_mode = ?, apply_resume_sha256 = ?
            WHERE url = ? AND apply_status = 'awaiting_user'
        """, (datetime.now(timezone.utc).isoformat(), resume_mode, resume_sha256, job_url)).rowcount
        if not changed:
            conn.rollback()
            raise ValueError("Another process has already claimed this handoff.")
        attempt_id = uuid4().hex
        conn.execute("""
            INSERT INTO apply_attempts
                (id, run_id, job_url, resume_mode, resume_sha256, started_at,
                 model, account_fingerprint, profile_sha256, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (attempt_id, run_id, job_url, resume_mode, resume_sha256,
              datetime.now(timezone.utc).isoformat(), model, current_fingerprint,
              profile_sha256, int(dry_run)))
        conn.commit()
        job.update({
            "apply_attempt_id": attempt_id,
            "apply_resume_mode": resume_mode,
            "apply_resume_sha256": resume_sha256,
        })

        console.print(f"\n[bold]Saved application:[/bold] {job.get('title') or 'Unknown'} at {job.get('company') or job.get('site') or 'Unknown'}")
        console.print("Opening the saved isolated ApplyPilot Chrome profile. The AI can inspect the role and destination, but cannot edit signup/login fields.")
        chrome_proc = launch_chrome(0, port=BASE_CDP_PORT, headless=False, startup_url=apply_url)
        handoff_result, duration_ms, _ = run_job(
            job, port=BASE_CDP_PORT, worker_id=0, model=model, token=token,
            dry_run=dry_run, resume_mode=resume_mode, resume_snapshot=snapshot,
            profile_snapshot=profile_snapshot, search_config_snapshot=search_snapshot,
            codex_home_root=run_dir / "codex-home", handoff_only=True,
        )

        if handoff_result.startswith("needs_review:"):
            mark_result(job_url, "needs_review", handoff_result.split(":", 1)[1],
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print(f"[yellow]This handoff needs review: {handoff_result.split(':', 1)[1]}[/yellow]")
            return
        handoff_domain = handoff_result.rsplit("@", 1)[1] if "@" in handoff_result else parsed_url.hostname
        handoff_reason = handoff_result.split(":", 1)[1].split("@", 1)[0] if handoff_result.startswith("awaiting_user:") else "login_or_signup"
        if handoff_result.startswith("failed:"):
            handoff_reason = "AI preparation did not complete; handle the employer step manually"
            handoff_domain = parsed_url.hostname
        elif not (handoff_result.startswith("awaiting_user:") or handoff_result.startswith("ready_to_resume@")):
            mark_result(job_url, "needs_review", "unexpected_handoff_result",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("[yellow]The handoff result was not recognized. The job was saved for manual review.[/yellow]")
            return

        console.print(f"\n[bold yellow]AI and Playwright are stopped.[/bold yellow] You now control Chrome. ApplyPilot will not observe your password, passkey, MFA code, or verification step.")
        console.print(f"Check the address bar carefully. Reported destination: [bold]{handoff_domain}[/bold]")
        if not Confirm.ask("Does this page belong to the expected employer or its application provider?", default=False):
            mark_result(job_url, "needs_review", f"destination_not_confirmed:{handoff_domain}",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("[yellow]Saved for manual review. No credentials were requested by ApplyPilot.[/yellow]")
            return
        console.print("Complete signup or sign-in in Chrome. Enter secrets only in the employer site. If the site asks for consent, legal attestations, identity documents, or an unsupported answer, handle that decision yourself.")
        console.print("After signup, use the employer site’s Save/Exit option if it has one. ApplyPilot will close Chrome and reopen the original job URL before the AI resumes, so unsaved form entries may be lost.")
        try:
            handoff_action = Prompt.ask(
                "When ready, choose continue, pause, applied, or manual",
                choices=["continue", "pause", "applied", "manual"], default="pause",
            )
        except KeyboardInterrupt:
            handoff_action = "pause"
        if handoff_action == "applied":
            if Confirm.ask("Did you personally submit this application and see a confirmation?", default=False):
                mark_result(job_url, "applied", duration_ms=duration_ms,
                            attempt_id=attempt_id, verification_evidence="user_confirmed")
                console.print("[green]Recorded as applied based on your confirmation.[/green]")
            else:
                mark_result(job_url, "awaiting_user", "submission_not_confirmed",
                            duration_ms=duration_ms, attempt_id=attempt_id)
            return
        if handoff_action == "manual":
            mark_result(job_url, "manual", "user_chose_to_finish_application_manually",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("Saved as manual. ApplyPilot will not resume this job automatically.")
            return
        if handoff_action == "pause":
            mark_result(job_url, "awaiting_user", "paused_by_user",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("Saved as awaiting you. Resume it later with `applypilot apply --resume`.")
            return

        if not Confirm.ask(
            "Authorize ApplyPilot to resume this one job under its saved settings? It may submit the application if all checks pass.",
            default=False,
        ):
            mark_result(job_url, "awaiting_user", "resume_not_authorized_yet",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("Saved as awaiting you. Resume it later with `applypilot apply --resume`.")
            return

        cleanup_worker(0, chrome_proc)
        chrome_proc = None
        try:
            token = auth.get_access_token(
                client_id,
                min_ttl_seconds=config.DEFAULTS["apply_timeout"] + 120,
            )
        except Exception as exc:
            mark_result(job_url, "awaiting_user", "ChatGPT session expired during the handoff; sign in and resume again.",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            raise RuntimeError("The ChatGPT session expired while you were completing employer sign-in. ApplyPilot saved the job; run `applypilot auth login` if needed, then `applypilot apply --resume` again.") from exc
        # Reopen only the original application URL in the persistent profile. The fresh
        # Codex thread never receives the credential/verification page snapshot.
        chrome_proc = launch_chrome(0, port=BASE_CDP_PORT, headless=False, startup_url=apply_url)
        conn.execute("""
            UPDATE jobs SET apply_phase = 'application', agent_id = 'interactive-resume'
            WHERE url = ? AND apply_status = 'in_progress'
        """, (job_url,))
        conn.commit()
        result, duration_ms, evidence = run_job(
            job, port=BASE_CDP_PORT, worker_id=0, model=model, token=token,
            dry_run=dry_run, resume_mode=resume_mode, resume_snapshot=snapshot,
            profile_snapshot=profile_snapshot, search_config_snapshot=search_snapshot,
            codex_home_root=run_dir / "codex-home", handoff_only=False,
        )
        if result == "applied":
            mark_result(job_url, "applied", duration_ms=duration_ms,
                        attempt_id=attempt_id, verification_evidence=evidence)
            console.print("[green]Application submitted and confirmation verified.[/green]")
        elif result == "dry_run":
            mark_result(job_url, "previewed", "Browser rehearsal completed without submission.",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("[green]Browser rehearsal completed. No application was submitted.[/green]")
        elif result.startswith("awaiting_user:"):
            mark_result(job_url, "awaiting_user", result.split(":", 1)[1],
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("[yellow]Another login or verification step is required. Resume later with `applypilot apply --resume`.[/yellow]")
        elif result.startswith("needs_review:"):
            mark_result(job_url, "needs_review", result.split(":", 1)[1],
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print(f"[yellow]Saved for review: {result.split(':', 1)[1]}[/yellow]")
        elif result.startswith("uncertain"):
            mark_result(job_url, "uncertain", result.split(":", 1)[1] if ":" in result else "unknown",
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print("[red]The submission outcome is uncertain. Check the employer site before retrying.[/red]")
        elif result in {"expired", "captcha"}:
            mark_result(job_url, result, result, permanent=True,
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print(f"[yellow]Job stopped: {result}.[/yellow]")
        else:
            reason = result.split(":", 1)[1] if ":" in result else result
            mark_result(job_url, "failed", reason,
                        permanent=_is_permanent_failure(result),
                        duration_ms=duration_ms, attempt_id=attempt_id)
            console.print(f"[red]Application did not complete: {reason}[/red]")
    except KeyboardInterrupt:
        if job and attempt_id:
            current = get_connection().execute(
                "SELECT apply_phase FROM jobs WHERE url = ?", (job["url"],)
            ).fetchone()
            if current and current["apply_phase"] == "auth_handoff":
                mark_result(job["url"], "awaiting_user", "interactive_handoff_interrupted",
                            attempt_id=attempt_id)
                console.print("\n[yellow]Handoff saved. Resume later with `applypilot apply --resume`.[/yellow]")
            else:
                mark_result(job["url"], "uncertain", "interrupted_during_application; check the employer site before retrying",
                            attempt_id=attempt_id)
                console.print("\n[red]The application was interrupted after AI resumed. Check the employer site before retrying.[/red]")
    finally:
        if chrome_proc is not None:
            cleanup_worker(0, chrome_proc)
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        lock.release()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL, apply_phase = NULL
        WHERE apply_status = 'failed'
    """)
    conn.commit()
    return cursor.rowcount


def reset_previewed() -> int:
    """Requeue browser rehearsals only after the user has reviewed them."""
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL, agent_id = NULL, apply_phase = NULL
        WHERE apply_status = 'previewed'
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

_RESULT_LINE = re.compile(
    r"^RESULT:(APPLIED|DRY_RUN|UNCERTAIN|EXPIRED|CAPTCHA|READY_TO_RESUME|"
    r"WAITING_FOR_USER:[a-z0-9_-]+|"
    r"NEEDS_REVIEW:[a-z0-9_-]+|FAILED:[a-z0-9_-]+)$"
)


def _extract_structured_result(final_text: str) -> tuple[str | None, str | None]:
    """Parse one exact result and require independent evidence for APPLIED."""
    matches = [match.group(1) for line in final_text.splitlines()
               if (match := _RESULT_LINE.fullmatch(line.strip()))]
    if len(matches) != 1:
        return None, None
    code = matches[0]
    if code == "APPLIED":
        lines = [line.strip() for line in final_text.splitlines()]
        evidence = [
            line.partition(":")[2]
            for line in lines
            if line.startswith("SUBMISSION_EVIDENCE:")
        ]
        if (lines.count("JOB_MATCH:CONFIRMED") != 1
                or lines.count("RESUME_UPLOAD:CONFIRMED") != 1
                or len(evidence) != 1
                or evidence[0] not in {
                    "confirmation_page", "application_received", "application_status_updated",
                }):
            return "uncertain:missing_submission_evidence", None
        return "applied", evidence[0]
    if code.startswith("NEEDS_REVIEW:"):
        return "needs_review:" + code.split(":", 1)[1], None
    if code.startswith("WAITING_FOR_USER:"):
        return "awaiting_user:" + code.split(":", 1)[1], None
    if code.startswith("FAILED:"):
        return "failed:" + code.split(":", 1)[1], None
    if code == "READY_TO_RESUME":
        return "ready_to_resume", None
    return code.casefold(), None


def _extract_handoff_domain(final_text: str) -> str | None:
    """Accept one hostname-only handoff destination from the agent result."""
    values = [line.partition(":")[2].strip() for line in final_text.splitlines()
              if line.strip().startswith("HANDOFF_DOMAIN:")]
    if len(values) != 1 or not values[0] or any(char.isspace() for char in values[0]):
        return None
    try:
        parsed = urlparse("//" + values[0])
        port = parsed.port
    except ValueError:
        return None
    if (not parsed.hostname or parsed.username or parsed.password or port
            or parsed.path or parsed.query or parsed.fragment):
        return None
    return parsed.hostname.casefold().rstrip(".")


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "", token: str = "", dry_run: bool = False,
            resume_mode: str = "base", resume_snapshot=None,
            profile_snapshot: dict | None = None,
            search_config_snapshot: dict | None = None,
            codex_home_root: Path | None = None,
            handoff_only: bool = False) -> tuple[str, int, str | None]:
    """Run one isolated Codex browser turn and return a conservative outcome."""
    start = time.monotonic()
    runtime: CodexAppServer | None = None
    worker_dir: Path | None = None
    try:
        worker_dir = reset_worker_dir(worker_id)
        if resume_mode == "base":
            if resume_snapshot is None:
                raise ValueError("Base resume snapshot was not prepared.")
            resume_text = resume_snapshot.text
            resume_pdf_path = resume_snapshot.pdf_path
        else:
            resume_path = job.get("tailored_resume_path")
            if not resume_path:
                raise ValueError("Tailored resume is missing.")
            txt_path = Path(resume_path).with_suffix(".txt")
            if not txt_path.is_file():
                raise ValueError(f"Tailored resume text file is missing: {txt_path}")
            resume_text = txt_path.read_text(encoding="utf-8").strip()
            resume_pdf_path = Path(resume_path).with_suffix(".pdf")

        agent_prompt = prompt_mod.build_prompt(
            job=job,
            tailored_resume=resume_text,
            dry_run=dry_run,
            resume_mode=resume_mode,
            resume_pdf_path=resume_pdf_path,
            worker_dir=worker_dir,
            profile_snapshot=profile_snapshot,
            search_config_snapshot=search_config_snapshot,
            handoff_only=handoff_only,
        )

        update_state(
            worker_id, status="applying", job_title=job.get("title", ""),
            company=job.get("site", ""), score=job.get("fit_score") or 0,
            start_time=time.time(), actions=0, last_action="starting",
        )
        add_event(f"[W{worker_id}] Starting: {job.get('title', 'Unknown')[:40]} @ {job.get('site', '')}")
        if not token or not model:
            raise CodexRuntimeError("A selected ChatGPT account, model, and valid session token are required.")
        if codex_home_root is None:
            raise ValueError("The run-specific Codex home directory is missing.")
        runtime = CodexAppServer(
            token=token,
            model=model,
            codex_home=codex_home_root / f"worker-{worker_id}-{job.get('apply_attempt_id', uuid4().hex)}",
            cwd=worker_dir,
            worker_dir=worker_dir,
            cdp_port=port,
            job_url=job.get("application_url") or job.get("url"),
            handoff_only=handoff_only,
        )
        with _codex_lock:
            _codex_procs[worker_id] = runtime.proc
        turn = runtime.run_turn(
            agent_prompt,
            total_timeout=config.DEFAULTS["apply_timeout"],
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        verification_evidence = None
        if turn.status != "completed":
            runtime_error = turn.error_code or turn.status
            if handoff_only:
                return f"failed:handoff_{runtime_error}", duration_ms, None
            if runtime.tool_calls:
                return f"uncertain:codex_{runtime_error}", duration_ms, None
            return f"failed:codex_{runtime_error}", duration_ms, None
        result, verification_evidence = _extract_structured_result(turn.text)
        if result and (result.startswith("awaiting_user:")
                       or (handoff_only and result == "ready_to_resume")):
            handoff_domain = _extract_handoff_domain(turn.text)
            if not handoff_domain:
                return "needs_review:handoff_domain_missing", duration_ms, None
            result = f"{result}@{handoff_domain}"
        if handoff_only and result in {"applied", "dry_run", "uncertain"}:
            return "failed:unsafe_handoff_result", duration_ms, None
        if not handoff_only and result == "ready_to_resume":
            return "needs_review:unexpected_handoff_result", duration_ms, None
        if dry_run and result == "applied":
            return "uncertain:dry_run_submission_reported", duration_ms, None
        if result and result.startswith("failed:") and runtime.tool_calls:
            # A model's failure label cannot prove that earlier browser actions
            # had no side effects. Keep it out of automatic retries.
            return "uncertain:agent_reported_failure_after_browser_actions", duration_ms, None

        worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
        with worker_log.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"{datetime.now().isoformat()} run={job.get('apply_attempt_id', '')} "
                f"exit=completed outcome={result or 'uncertain'}\n"
            )
        if result is None:
            return "uncertain:missing_or_ambiguous_result", duration_ms, None

        add_event(f"[W{worker_id}] {result.upper()} ({duration_ms // 1000}s): {job.get('title', '')[:30]}")
        update_state(worker_id, status=result.split(":", 1)[0], last_action=result[:35])
        return result, duration_ms, verification_evidence
    except CodexRuntimeError:
        duration_ms = int((time.monotonic() - start) * 1000)
        if handoff_only:
            return "failed:handoff_runtime_error", duration_ms, None
        if runtime is not None and runtime.tool_calls:
            return "uncertain:codex_runtime_error", duration_ms, None
        logger.warning("Codex App Server could not complete the job (worker %d).", worker_id)
        return "failed:codex_runtime_error", duration_ms, None
    except Exception:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception("Apply worker %d failed for attempt %s", worker_id, job.get("apply_attempt_id", "unknown"))
        if handoff_only:
            return "failed:handoff_launcher_error", duration_ms, None
        if runtime is not None and runtime.tool_calls:
            return "uncertain:codex_runtime_error", duration_ms, None
        return "failed:launcher_error", duration_ms, None
    finally:
        with _codex_lock:
            _codex_procs.pop(worker_id, None)
        if runtime is not None:
            runtime.close()
            shutil.rmtree(runtime.codex_home, ignore_errors=True)
        if worker_dir is not None and worker_dir.exists():
            shutil.rmtree(worker_dir, ignore_errors=True)

# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


def _stop_codex_processes() -> None:
    """Stop only Codex App Server process trees owned by this run."""
    with _codex_lock:
        processes = list(_codex_procs.values())
    for process in processes:
        if process.poll() is None:
            _kill_process_tree(process.pid)


atexit.register(_stop_codex_processes)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int | None = None, headless: bool = False,
                model: str = "", client_id: str = "", account_fingerprint: str = "",
                profile_sha256: str = "", token_ttl_seconds: int = 360,
                profile_snapshot: dict | None = None,
                search_config_snapshot: dict | None = None,
                eligible_urls: set[str] | None = None,
                job_snapshots: dict[str, dict] | None = None,
                codex_home_root: Path | None = None,
                dry_run: bool = False,
                resume_mode: str = "base", resume_snapshot=None,
                resume_sha256: str = "", run_id: str = "",
                max_age_days: int | None = 30) -> tuple[int, int, int, int, int]:
    """Run a bounded queue, keeping uncertain and review outcomes out of retries."""
    applied = failed = review = previewed = awaiting_user = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    from applypilot.siwc import ChatGPTAuth
    auth = ChatGPTAuth()

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)
        try:
            token = auth.get_access_token(client_id, min_ttl_seconds=token_ttl_seconds)
        except Exception:
            logger.warning("ChatGPT session is not ready; stopping before another job is acquired.")
            add_event(f"[W{worker_id}] ChatGPT session unavailable; stopped before next job.")
            update_state(worker_id, status="done", last_action="ChatGPT session unavailable")
            break

        job = acquire_job(
            target_url=target_url, min_score=min_score, worker_id=worker_id,
            resume_mode=resume_mode, run_id=run_id, resume_sha256=resume_sha256,
            model=model, account_fingerprint=account_fingerprint, profile_sha256=profile_sha256,
            dry_run=dry_run,
            eligible_urls=eligible_urls, job_snapshots=job_snapshots,
            max_age_days=max_age_days,
        )
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle", last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break
            continue

        empty_polls = 0
        attempt_id = job.get("apply_attempt_id")
        chrome_proc = None
        try:
            apply_url = job.get("application_url") or job["url"]
            cooldown = float(config.DEFAULTS.get("apply_domain_cooldown_seconds", 15))
            delay = _reserve_domain_slot(apply_url, cooldown)
            if delay and _stop_event.wait(delay):
                release_lock(job["url"], attempt_id, outcome="cancelled_before_browser")
                break

            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)
            result, duration_ms, evidence = run_job(
                job, port=port, worker_id=worker_id, model=model, token=token,
                dry_run=dry_run, resume_mode=resume_mode, resume_snapshot=resume_snapshot,
                profile_snapshot=profile_snapshot, search_config_snapshot=search_config_snapshot,
                codex_home_root=codex_home_root,
            )

            if result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms,
                            attempt_id=attempt_id, verification_evidence=evidence)
                applied += 1
                update_state(worker_id, jobs_applied=applied, jobs_done=applied + failed + review + previewed + awaiting_user)
            elif result == "dry_run":
                mark_result(job["url"], "previewed", error="Browser rehearsal completed without submission.",
                            duration_ms=duration_ms, attempt_id=attempt_id)
                previewed += 1
                update_state(worker_id, jobs_done=applied + failed + review + previewed + awaiting_user)
            elif result.startswith("needs_review:"):
                reason = result.split(":", 1)[1]
                mark_result(job["url"], "needs_review", error=reason, duration_ms=duration_ms, attempt_id=attempt_id)
                review += 1
                update_state(worker_id, jobs_done=applied + failed + review + previewed + awaiting_user)
            elif result.startswith("awaiting_user:"):
                reason = result.split(":", 1)[1]
                mark_result(job["url"], "awaiting_user", error=reason,
                            duration_ms=duration_ms, attempt_id=attempt_id)
                awaiting_user += 1
                add_event(f"[W{worker_id}] Waiting for you: {job.get('title', '')[:30]}")
                update_state(worker_id, status="awaiting_user",
                             last_action="resume with applypilot apply --resume",
                             jobs_done=applied + failed + review + previewed + awaiting_user)
            elif result.startswith("uncertain"):
                reason = result.split(":", 1)[1] if ":" in result else "unknown"
                mark_result(job["url"], "uncertain", error=reason, duration_ms=duration_ms, attempt_id=attempt_id)
                review += 1
                add_event(f"[W{worker_id}] Outcome uncertain. Verify manually before retrying.")
                update_state(worker_id, jobs_done=applied + failed + review + previewed + awaiting_user)
                if reason.startswith("codex_"):
                    add_event(f"[W{worker_id}] Codex runtime stopped; no further jobs will be sent this run.")
                    _stop_event.set()
            elif result in {"expired", "captcha"}:
                mark_result(job["url"], result, error=result, permanent=True,
                            duration_ms=duration_ms, attempt_id=attempt_id)
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed + review + previewed + awaiting_user)
            else:
                reason = result.split(":", 1)[1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms, attempt_id=attempt_id)
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed + review + previewed + awaiting_user)
                if reason.startswith("codex_"):
                    add_event(f"[W{worker_id}] Codex runtime stopped; no further jobs will be sent this run.")
                    _stop_event.set()

        except KeyboardInterrupt:
            # The browser agent might have submitted before interruption; never auto-retry.
            mark_result(job["url"], "uncertain", "interrupted; verify application status manually",
                        attempt_id=attempt_id)
            review += 1
            if _stop_event.is_set():
                break
        except Exception as exc:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(exc)[:40]}")
            mark_result(job["url"], "failed", "launcher_error", attempt_id=attempt_id)
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(
        worker_id,
        status="awaiting_user" if awaiting_user else "done",
        last_action=(f"{awaiting_user} handoff(s) saved" if awaiting_user else "finished"),
    )
    return applied, failed, review, previewed, awaiting_user

# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def _run_workers(limit: int, target_url: str | None, min_score: int | None,
                 headless: bool, model: str, dry_run: bool, continuous: bool,
                 poll_interval: int, workers: int, resume_mode: str,
                 resume_snapshot, resume_sha256: str, run_id: str,
                 max_age_days: int | None, client_id: str, account_fingerprint: str,
                 profile_sha256: str, profile_snapshot: dict,
                 search_config_snapshot: dict, codex_home_root: Path,
                 eligible_urls: set[str] | None, job_snapshots: dict[str, dict] | None) -> None:
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()
    _domain_next_start.clear()
    console = Console()
    effective_limit = 0 if continuous else limit
    mode_label = "continuous" if continuous else f"{effective_limit} jobs"

    for worker_id in range(workers):
        init_worker(worker_id)
    worker_label = f"{workers} worker{'s' if workers != 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = mark the current outcome for manual verification | Ctrl+C x2 = stop[/dim]")

    ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal ctrl_c_count
        ctrl_c_count += 1
        if ctrl_c_count == 1:
            console.print("\n[yellow]Stopping the current browser task; verify its outcome before retrying.[/yellow]")
            with _codex_lock:
                for process in list(_codex_procs.values()):
                    if process.poll() is None:
                        _kill_process_tree(process.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _codex_lock:
                for process in list(_codex_procs.values()):
                    if process.poll() is None:
                        _kill_process_tree(process.pid)
            _stop_codex_processes()
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)
    dashboard_running = True
    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            def _refresh():
                while dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            worker_args = {
                "target_url": target_url, "min_score": min_score,
                "headless": headless, "model": model, "dry_run": dry_run,
                "client_id": client_id, "account_fingerprint": account_fingerprint,
                "profile_sha256": profile_sha256,
                "token_ttl_seconds": (
                    config.DEFAULTS["apply_timeout"]
                    + config.DEFAULTS["apply_domain_cooldown_seconds"] + 120
                ),
                "profile_snapshot": profile_snapshot,
                "search_config_snapshot": search_config_snapshot,
                "eligible_urls": eligible_urls,
                "job_snapshots": job_snapshots,
                "codex_home_root": codex_home_root,
                "resume_mode": resume_mode, "resume_snapshot": resume_snapshot,
                "resume_sha256": resume_sha256, "run_id": run_id,
                "max_age_days": max_age_days,
            }
            if workers == 1:
                results = [worker_loop(worker_id=0, limit=effective_limit, **worker_args)]
            else:
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0) for i in range(workers)]
                else:
                    limits = [0] * workers
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(worker_loop, worker_id=i, limit=limits[i], **worker_args): i
                        for i in range(workers)
                    }
                    results = []
                    for future in as_completed(futures):
                        worker_id = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", worker_id)
                            results.append((0, 0, 0, 0, 0))

            dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        total_applied = sum(row[0] for row in results)
        total_failed = sum(row[1] for row in results)
        total_review = sum(row[2] for row in results)
        total_previewed = sum(row[3] for row in results)
        total_awaiting_user = sum(row[4] for row in results)
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed, "
            f"{total_review} need review, {total_awaiting_user} awaiting you, "
            f"{total_previewed} rehearsed.[/bold]"
        )
        console.print("[dim]ChatGPT plan usage is shared with your account; this run does not report API dollar cost.[/dim]")
        console.print(f"Logs: {config.LOG_DIR}")
    except KeyboardInterrupt:
        pass
    finally:
        dashboard_running = False
        _stop_event.set()
        _stop_codex_processes()
        kill_all_chrome()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _freeze_job_inputs(
    candidates: list[dict], run_dir: Path, resume_mode: str, base_resume_sha256: str,
) -> dict[str, dict]:
    """Copy bounded-run resume and cover-letter artifacts into its private snapshot."""
    frozen: dict[str, dict] = {}
    input_dir = run_dir / "inputs"
    resume_dir = input_dir / "resumes"
    cover_dir = input_dir / "cover-letters"
    resume_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        job = dict(candidate)
        if resume_mode == "base":
            job["apply_resume_sha256"] = base_resume_sha256
        else:
            source = Path(job["tailored_resume_path"]).resolve()
            source_txt = source.with_suffix(".txt")
            source_pdf = source.with_suffix(".pdf")
            if not source_txt.is_file() or not source_pdf.is_file():
                continue
            resume_hash = _sha256_file(source_pdf)
            text_hash = _sha256_file(source_txt)
            snapshot_key = hashlib.sha256(f"{resume_hash}:{text_hash}".encode("ascii")).hexdigest()
            destination_stem = resume_dir / snapshot_key
            destination_txt = destination_stem.with_suffix(".txt")
            destination_pdf = destination_stem.with_suffix(".pdf")
            if not destination_txt.exists():
                shutil.copy2(source_txt, destination_txt)
                if os.name != "nt":
                    os.chmod(destination_txt, 0o600)
            if not destination_pdf.exists():
                shutil.copy2(source_pdf, destination_pdf)
                if os.name != "nt":
                    os.chmod(destination_pdf, 0o600)
            if (_sha256_file(destination_txt) != text_hash
                    or _sha256_file(destination_pdf) != resume_hash):
                raise RuntimeError("A tailored resume changed while the run snapshot was being prepared.")
            job["tailored_resume_path"] = str(destination_txt)
            job["apply_resume_sha256"] = resume_hash
            job["tailored_resume_text_sha256"] = text_hash

        source_cover = job.get("cover_letter_path") if resume_mode == "tailored" else None
        if source_cover and Path(source_cover).is_file():
            source_cover = Path(source_cover).resolve()
            source_text = source_cover.with_suffix(".txt") if source_cover.suffix.lower() == ".pdf" else source_cover
            source_pdf = source_cover if source_cover.suffix.lower() == ".pdf" else source_cover.with_suffix(".pdf")
            cover_dir.mkdir(parents=True, exist_ok=True)
            text_hash = _sha256_file(source_text) if source_text.is_file() else ""
            pdf_hash = _sha256_file(source_pdf) if source_pdf.is_file() else ""
            cover_key = hashlib.sha256(f"{text_hash}:{pdf_hash}".encode("ascii")).hexdigest()
            if source_text.is_file():
                destination_text = cover_dir / f"{cover_key}.txt"
                if not destination_text.exists():
                    shutil.copy2(source_text, destination_text)
                    if os.name != "nt":
                        os.chmod(destination_text, 0o600)
                if _sha256_file(destination_text) != text_hash:
                    raise RuntimeError("A cover letter changed while the run snapshot was being prepared.")
            if source_pdf.is_file():
                destination_pdf = cover_dir / f"{cover_key}.pdf"
                if not destination_pdf.exists():
                    shutil.copy2(source_pdf, destination_pdf)
                    if os.name != "nt":
                        os.chmod(destination_pdf, 0o600)
                if _sha256_file(destination_pdf) != pdf_hash:
                    raise RuntimeError("A cover letter changed while the run snapshot was being prepared.")
                job["cover_letter_path"] = str(cover_dir / f"{cover_key}.pdf")
            elif source_text.is_file():
                job["cover_letter_path"] = str(cover_dir / f"{cover_key}.txt")

        frozen[job["url"]] = job
    return frozen


def main(limit: int = 1, target_url: str | None = None,
         min_score: int | None = None, headless: bool = False,
         model: str | None = None, dry_run: bool = False,
         continuous: bool = False, poll_interval: int = 60,
         workers: int = 1, resume_mode: str = "base",
         max_age_days: int | None = 30) -> None:
    """Run a bounded or continuous application queue with a stable resume snapshot."""
    if resume_mode not in {"base", "tailored"}:
        raise ValueError("resume_mode must be 'base' or 'tailored'")
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if workers > config.DEFAULTS["max_apply_workers"]:
        raise ValueError(f"workers cannot exceed {config.DEFAULTS['max_apply_workers']}")
    if max_age_days is not None and max_age_days < 0:
        raise ValueError("max_age_days cannot be negative")
    if not continuous and limit < 1:
        raise ValueError("limit must be at least 1 unless --continuous is used")
    if not continuous and limit > config.DEFAULTS["max_apply_batch"]:
        raise ValueError(f"limit cannot exceed {config.DEFAULTS['max_apply_batch']} for one batch")
    if continuous and target_url:
        raise ValueError("A specific --url cannot be combined with --continuous.")
    if resume_mode == "tailored" and min_score is None:
        min_score = 7
    if resume_mode == "base":
        min_score = None
    if target_url and not continuous:
        limit = 1
    if not continuous:
        workers = min(workers, limit)

    config.ensure_dirs()
    from applypilot.database import init_db
    init_db()
    lock = FileLock(str(config.APP_DIR / "apply.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError("Another ApplyPilot apply run is active for this profile.") from exc

    run_id = uuid4().hex
    run_dir = config.APPLY_RUN_DIR / run_id
    snapshot = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(run_dir, 0o700)
        from applypilot.siwc import ChatGPTAuth

        auth = ChatGPTAuth()
        client_id = auth.active_client_id()
        if not client_id or not auth.has_active_account():
            raise RuntimeError("Connect an eligible ChatGPT account with `applypilot auth login` before applying.")
        auth.get_access_token(
            client_id,
            min_ttl_seconds=(config.DEFAULTS["apply_timeout"]
                             + config.DEFAULTS["apply_domain_cooldown_seconds"] + 120),
        )
        model = resolve_model(auth, client_id, model)
        profile_snapshot = json.loads(json.dumps(config.load_profile(), ensure_ascii=False))
        search_config_snapshot = json.loads(json.dumps(config.load_search_config(), ensure_ascii=False))
        profile_sha256 = hashlib.sha256(
            json.dumps(profile_snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        search_sha256 = hashlib.sha256(
            json.dumps(search_config_snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        account_fingerprint = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]

        interrupted = _recover_interrupted_attempts()
        if interrupted:
            Console().print(
                f"[yellow]Marked {interrupted} unfinished application(s) uncertain. "
                "Check their status before retrying.[/yellow]"
            )
        if resume_mode == "base":
            from applypilot.apply.resume import prepare_base_resume_snapshot
            snapshot = prepare_base_resume_snapshot(run_dir)
        job_snapshots = None
        eligible_urls = None
        if not continuous:
            candidates = list_ready_jobs(
                target_url=target_url,
                min_score=min_score,
                resume_mode=resume_mode,
                limit=10000,
                max_age_days=max_age_days,
            )
            job_snapshots = _freeze_job_inputs(
                candidates, run_dir, resume_mode, snapshot.sha256 if snapshot else "",
            )
            eligible_urls = set(job_snapshots)
        job_snapshot_entries = []
        if job_snapshots is not None:
            for url, job in job_snapshots.items():
                serialized = json.dumps(job, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
                job_snapshot_entries.append(hashlib.sha256(serialized.encode("utf-8")).hexdigest())
        job_snapshot_sha256 = (
            hashlib.sha256("\n".join(sorted(job_snapshot_entries)).encode("ascii")).hexdigest()
            if job_snapshots is not None else None
        )
        manifest = {
            "version": 1,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "runtime": "codex_app_server_siwc",
            "playwright_mcp_version": PLAYWRIGHT_MCP_VERSION,
            "model": model,
            "account_fingerprint": account_fingerprint,
            "profile_sha256": profile_sha256,
            "search_config_sha256": search_sha256,
            "resume_mode": resume_mode,
            "resume_sha256": snapshot.sha256 if snapshot else None,
            "queue_mode": "live" if continuous else "frozen",
            "job_count": len(job_snapshots) if job_snapshots is not None else None,
            "job_snapshot_sha256": job_snapshot_sha256,
            "filters": {
                "target_url_sha256": hashlib.sha256(target_url.encode("utf-8")).hexdigest() if target_url else None,
                "min_score": min_score,
                "max_age_days": max_age_days,
                "limit": limit,
                "continuous": continuous,
                "workers": workers,
                "dry_run": dry_run,
            },
        }
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(manifest_path, 0o600)
        _run_workers(
            limit=limit, target_url=target_url, min_score=min_score,
            headless=headless, model=model, dry_run=dry_run,
            continuous=continuous, poll_interval=poll_interval,
            workers=workers, resume_mode=resume_mode,
            resume_snapshot=snapshot,
            resume_sha256=snapshot.sha256 if snapshot else "",
            run_id=run_id,
            max_age_days=max_age_days,
            client_id=client_id,
            account_fingerprint=account_fingerprint,
            profile_sha256=profile_sha256,
            profile_snapshot=profile_snapshot,
            search_config_snapshot=search_config_snapshot,
            codex_home_root=run_dir / "codex-home",
            eligible_urls=eligible_urls,
            job_snapshots=job_snapshots,
        )
    finally:
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        lock.release()
