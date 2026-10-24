"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
auth_app = typer.Typer(name="auth", help="Manage ChatGPT plan sign-in and models.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
browser_app = typer.Typer(name="browser", help="Manage the isolated ApplyPilot Chrome profile.", no_args_is_help=True)
app.add_typer(browser_app, name="browser")
profile_app = typer.Typer(name="profile", help="Manage applicant profile data.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@profile_app.command("clear-job-password")
def profile_clear_job_password() -> None:
    """Remove an older saved job-site password from profile.json."""
    import json
    from applypilot.config import PROFILE_PATH

    if not PROFILE_PATH.is_file():
        console.print("[yellow]No profile.json found.[/yellow]")
        raise typer.Exit(code=1)
    try:
        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]Could not read profile.json:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    personal = profile.get("personal")
    if not isinstance(personal, dict) or "password" not in personal:
        console.print("No saved job-site password was present.")
        return
    personal.pop("password", None)
    temporary_path = PROFILE_PATH.with_suffix(".json.tmp")
    try:
        temporary_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary_path.replace(PROFILE_PATH)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        console.print(f"[red]Could not update profile.json:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print("[green]Removed the saved job-site password from profile.json.[/green]")


@auth_app.command("login")
def auth_login(
    account: str | None = typer.Option(None, "--account", help="Reauthorize a saved issued client ID."),
    new_account: bool = typer.Option(False, "--new-account", help="Register another ChatGPT account/workspace."),
) -> None:
    """Connect or reauthorize a ChatGPT account for subscription-backed AI."""
    from applypilot.llm_errors import ChatGPTAuthError
    from applypilot.siwc import ChatGPTAuth

    if not Confirm.ask(
        "ApplyPilot will send your resume, profile details, and job descriptions to OpenAI for AI stages. "
        "ChatGPT plan usage is shared with other apps on the account and subject to its limits. Continue?",
        default=True,
    ):
        raise typer.Exit()

    auth = ChatGPTAuth()
    try:
        accounts = auth.accounts()
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not read saved ChatGPT accounts:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    selected_id = account
    add_account = new_account
    if selected_id and new_account:
        console.print("[red]Choose either --account or --new-account.[/red]")
        raise typer.Exit(code=2)
    if not selected_id and not add_account:
        if len(accounts) == 1:
            selected_id = accounts[0]["client_id"]
        elif len(accounts) > 1:
            choices = [item["client_id"] for item in accounts] + ["new"]
            try:
                active_id = auth.active_client_id()
            except ChatGPTAuthError as exc:
                console.print(f"[red]Could not read the active ChatGPT account:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            default = active_id if active_id in choices else "new"
            choice = Prompt.ask("Choose a saved registration or add one", choices=choices, default=default)
            add_account = choice == "new"
            if not add_account:
                selected_id = choice

    try:
        saved = auth.login(client_id=selected_id, new_account=add_account)
    except ChatGPTAuthError as exc:
        console.print(f"[red]ChatGPT sign-in failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if auth.is_plan_enabled(saved):
        console.print(f"[green]Connected:[/green] {saved.get('email') or 'ChatGPT account'}")
        try:
            models = auth.list_models(saved["client_id"])
            console.print(f"[green]Available models:[/green] {len(models)}. Run `applypilot auth models` to view them.")
        except ChatGPTAuthError as exc:
            console.print(f"[yellow]Sign-in saved, but model discovery failed:[/yellow] {exc}")
    else:
        console.print(
            "[yellow]Sign-in saved, but ChatGPT did not grant plan inference permission. "
            "Run `applypilot auth login --account <client-id>` and approve the requested plan scope.[/yellow]"
        )


@auth_app.command("accounts")
def auth_accounts() -> None:
    """List saved ChatGPT registrations without displaying credentials."""
    from applypilot.siwc import ChatGPTAuth

    auth = ChatGPTAuth()
    try:
        accounts = auth.accounts()
        active_id = auth.active_client_id()
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not read saved ChatGPT accounts:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not accounts:
        console.print("No ChatGPT registrations saved. Run `applypilot auth login`.")
        return
    table = Table(title="ChatGPT Registrations", show_header=True, header_style="bold cyan")
    table.add_column("Active")
    table.add_column("Account")
    table.add_column("Issued client ID")
    table.add_column("Plan access")
    for item in accounts:
        table.add_row(
            "*" if item.get("client_id") == active_id else "",
            item.get("email") or "Email unavailable",
            item["client_id"],
            "enabled" if auth.is_plan_enabled(item) else "needs sign-in/permission",
        )
    console.print(table)


@auth_app.command("status")
def auth_status() -> None:
    """Show the active ChatGPT account and local session status."""
    from applypilot.llm_errors import ChatGPTAuthError
    from applypilot.siwc import ChatGPTAuth, USAGE_SETTINGS_URL

    auth = ChatGPTAuth()
    try:
        account = auth.active_account()
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not read the local ChatGPT account:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not account:
        console.print("[yellow]No ChatGPT account connected.[/yellow] Run `applypilot auth login`.")
        return
    expires_at = account.get("expires_at", 0)
    if account.get("refresh_state") in {"in_progress", "unknown"}:
        expires_text = "refresh outcome unconfirmed; sign in again"
    else:
        expires_text = (
            datetime.fromtimestamp(expires_at).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            if expires_at else "needs sign-in"
        )
    try:
        state = "enabled" if auth.has_active_account() else "not enabled or needs sign-in"
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not inspect the local ChatGPT session:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Account:      {account.get('email') or 'Email unavailable'}")
    console.print(f"Plan access:  {state}")
    console.print(f"Session:      {expires_text}")
    console.print(f"Model:        {account.get('selected_model') or 'first model visible to this account'}")
    console.print(f"Usage:        {USAGE_SETTINGS_URL}")


@auth_app.command("pending")
def auth_pending() -> None:
    """Show requests whose outcome was not confirmed."""
    from applypilot.llm_errors import LLMError
    from applypilot.llm_outcomes import pending

    try:
        outcomes = pending()
    except LLMError as exc:
        console.print(f"[red]Could not read request outcomes:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not outcomes:
        console.print("No uncertain or in-flight LLM requests.")
        return
    table = Table(title="Requests Requiring Review", header_style="bold yellow")
    table.add_column("Request ID")
    table.add_column("State")
    table.add_column("Stage/item")
    table.add_column("Recorded")
    for outcome in outcomes:
        table.add_row(
            outcome["id"],
            outcome.get("state", "unknown"),
            outcome.get("label", "LLM request"),
            outcome.get("updated_at") or outcome.get("created_at", ""),
        )
    console.print(table)
    console.print(
        "ApplyPilot will not resend these requests until you explicitly release a guard with "
        "`applypilot auth retry-unknown <request-id>`."
    )


@auth_app.command("retry-unknown")
def auth_retry_unknown(request_id: str = typer.Argument(..., help="Full or unique prefix shown by `auth pending`.")) -> None:
    """Explicitly release one uncertain-request guard so a future run may retry it."""
    from applypilot.llm_errors import LLMError
    from applypilot.llm_outcomes import pending, release

    try:
        outcomes = pending()
        matches = [item for item in outcomes if item["id"] == request_id or item["id"].startswith(request_id)]
    except LLMError as exc:
        console.print(f"[red]Could not read request outcomes:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if len(matches) != 1:
        console.print("[red]Request ID was unmatched or ambiguous.[/red] Run `applypilot auth pending`.")
        raise typer.Exit(code=1)
    match = matches[0]
    if not Confirm.ask(
        f"The prior {match.get('label', 'LLM')} request may already have completed. "
        "Clear the guard so a later pipeline run may send it again and use plan capacity?",
        default=False,
    ):
        raise typer.Exit()
    try:
        released = release(match["id"])
    except LLMError as exc:
        console.print(f"[red]Could not release request guard:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[yellow]Released guard {released['id']}.[/yellow] The matching stage may resend this request "
        "the next time you run it."
    )


@auth_app.command("models")
def auth_models() -> None:
    """List models visible to the active ChatGPT account."""
    from applypilot.llm_errors import ChatGPTAuthError
    from applypilot.siwc import ChatGPTAuth

    auth = ChatGPTAuth()
    try:
        account = auth.active_account()
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not read the active ChatGPT account:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if not account:
        console.print("[yellow]No ChatGPT account connected.[/yellow] Run `applypilot auth login`.")
        raise typer.Exit(code=1)
    try:
        models = auth.list_models(account["client_id"])
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not load models:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    selected = account.get("selected_model")
    table = Table(title="Models Visible to the Active ChatGPT Account", header_style="bold cyan")
    table.add_column("Selected")
    table.add_column("Model slug")
    table.add_column("Name")
    for model in models:
        table.add_row("*" if model["slug"] == selected else "", model["slug"], model["display_name"])
    console.print(table)
    console.print("Select one with `applypilot auth use-model <slug>`. Model access can vary by account.")


@auth_app.command("use-account")
def auth_use_account(account: str = typer.Argument(..., help="Full issued client ID or unique account email.")) -> None:
    """Select the active ChatGPT registration."""
    from applypilot.llm_errors import ChatGPTAuthError
    from applypilot.siwc import ChatGPTAuth

    try:
        selected = ChatGPTAuth().select_account(account)
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not select account:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Active ChatGPT account:[/green] {selected.get('email') or selected['client_id']}")


@auth_app.command("use-model")
def auth_use_model(model: str = typer.Argument(..., help="Visible model slug from `applypilot auth models`.")) -> None:
    """Save a model selection for the active ChatGPT account."""
    from applypilot.llm_errors import ChatGPTAuthError
    from applypilot.siwc import ChatGPTAuth

    try:
        selected = ChatGPTAuth().select_model(model)
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not select model:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Selected model:[/green] {selected['display_name']} ({selected['slug']})")


@auth_app.command("test")
def auth_test(
    codex: bool = typer.Option(
        False, "--codex",
        help="Send one small request through Codex App Server; uses shared ChatGPT plan usage and opens no browser.",
    ),
) -> None:
    """Test the selected ChatGPT plan route, optionally through Codex App Server."""
    if codex:
        import shutil
        from uuid import uuid4

        from applypilot.apply.codex_runtime import resolve_model, run_smoke_request
        from applypilot.config import APPLY_RUN_DIR, ensure_dirs
        from applypilot.llm_errors import ChatGPTAuthError
        from applypilot.siwc import ChatGPTAuth

        ensure_dirs()
        auth = ChatGPTAuth()
        try:
            client_id = auth.active_client_id()
            if not client_id or not auth.has_active_account():
                raise ChatGPTAuthError("No eligible ChatGPT plan account is active. Run `applypilot auth login`.")
            token = auth.get_access_token(client_id, min_ttl_seconds=120)
            model = resolve_model(auth, client_id)
            run_dir = APPLY_RUN_DIR / f"codex-smoke-{uuid4().hex}"
            response = run_smoke_request(token, model, run_dir)
        except Exception as exc:
            console.print(f"[red]Codex App Server self-test failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        finally:
            if "run_dir" in locals() and run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
        console.print(f"[green]Codex App Server completed with {model}:[/green] {response}")
        console.print("No browser was launched and no employer site was contacted.")
        return

    from applypilot.llm import get_client
    from applypilot.llm_errors import LLMError

    try:
        client = get_client()
        response = client.ask(
            "Say exactly: ApplyPilot ChatGPT connection is working.",
            request_key="auth-test",
            request_label="ChatGPT connection self-test",
        )
    except LLMError as exc:
        console.print(f"[red]ChatGPT request failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Completed with {client.model}:[/green] {response}")
    console.print(f"Requests this process: {client.request_count}")


@browser_app.command("setup")
def browser_setup() -> None:
    """Open the isolated ApplyPilot Chrome profile for manual employer sign-in."""
    import sys

    from filelock import FileLock, Timeout
    from applypilot.config import APP_DIR, ensure_dirs
    from applypilot.apply.chrome import (
        BASE_CDP_PORT, cleanup_worker, launch_chrome,
        setup_worker_profile,
    )

    if not sys.stdin.isatty():
        console.print("[red]Run `applypilot browser setup` from an interactive terminal.[/red]")
        raise typer.Exit(code=2)
    ensure_dirs()
    profile_dir = setup_worker_profile(0)
    lock = FileLock(str(APP_DIR / "apply.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        console.print("[red]Another ApplyPilot browser/apply run is active.[/red]")
        raise typer.Exit(code=1) from exc
    chrome = None
    try:
        chrome = launch_chrome(0, port=BASE_CDP_PORT, headless=False)
        console.print(f"[green]ApplyPilot Chrome profile opened:[/green] {profile_dir}")
        console.print("Sign into employer sites yourself in this window. Do not enter passwords in the ApplyPilot prompt.")
        console.print("This command does not start Codex, contact OpenAI, or navigate to an employer site.")
        Prompt.ask("Press Enter here when you are done; the browser will close and keep its profile cookies")
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]Could not open the ApplyPilot Chrome profile:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        if chrome is not None:
            cleanup_worker(0, chrome)
        lock.release()


@auth_app.command("logout")
def auth_logout(
    account: str | None = typer.Option(None, "--account", help="Issued client ID to disconnect."),
    all_accounts: bool = typer.Option(False, "--all", help="Disconnect every saved registration."),
) -> None:
    """Revoke and remove the selected ChatGPT registration."""
    from applypilot.llm_errors import ChatGPTAuthError
    from applypilot.siwc import ChatGPTAuth

    auth = ChatGPTAuth()
    try:
        targets = [item["client_id"] for item in auth.accounts()] if all_accounts else [account]
        if not all_accounts and not account:
            targets = [auth.active_client_id()]
    except ChatGPTAuthError as exc:
        console.print(f"[red]Could not read saved ChatGPT accounts:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    targets = [item for item in targets if item]
    if not targets:
        console.print("No saved ChatGPT registrations to disconnect.")
        return
    if not Confirm.ask(f"Disconnect {len(targets)} ChatGPT registration(s)?", default=False):
        raise typer.Exit()
    for client_id in targets:
        try:
            revoked, revoke_error = auth.logout(client_id)
        except ChatGPTAuthError as exc:
            console.print(f"[red]Could not remove {client_id}:[/red] {exc}")
            continue
        if revoked:
            console.print(f"[green]Disconnected and revoked:[/green] {client_id}")
        else:
            console.print(
                f"[yellow]Removed locally:[/yellow] {client_id}. Remote revocation was not confirmed. "
                f"Disconnect it in ChatGPT Settings if needed. {revoke_error or ''}"
            )


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Maximum jobs to process (default: 1)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Browser/model workers (currently limited to 1)."),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum fit score in tailored mode (default: 7)."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Visible ChatGPT model slug; defaults to the active account selection."),
    resume_mode: str = typer.Option("base", "--resume-mode", help="Resume mode: base or tailored."),
    max_age_days: int = typer.Option(30, "--max-age-days", help="Exclude postings older than this when their posting date is known."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run continuously, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Browser rehearsal only; may autosave form data on employer sites."),
    preview: bool = typer.Option(False, "--preview", help="Show eligible jobs locally without launching a browser or contacting AI."),
    preview_limit: int = typer.Option(25, "--preview-limit", help="Maximum candidates shown by --preview."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm outbound browser automation without an interactive prompt."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific exact job URL."),
    gen: bool = typer.Option(False, "--gen", help="Write a local prompt file for manual inspection."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset only failed jobs for retry."),
    reset_previewed: bool = typer.Option(False, "--reset-previewed", help="Requeue browser rehearsals after manual review."),
    review: bool = typer.Option(False, "--review", help="List application outcomes that need manual review."),
    resume_waiting: bool = typer.Option(False, "--resume", help="Resume one saved employer login/signup handoff interactively."),
) -> None:
    """Apply with the unchanged base resume by default; tailored mode is optional."""
    _bootstrap()
    import sys
    from uuid import uuid4

    from applypilot.config import DEFAULTS, PROFILE_PATH
    from applypilot.apply.launcher import list_ready_jobs

    mode = resume_mode.strip().casefold()
    if mode not in {"base", "tailored"}:
        console.print("[red]--resume-mode must be either 'base' or 'tailored'.[/red]")
        raise typer.Exit(code=2)
    if (workers < 1 or workers > DEFAULTS["max_apply_workers"]
            or max_age_days < 0 or preview_limit < 1
            or (limit is not None and not continuous and
                (limit < 1 or limit > DEFAULTS["max_apply_batch"]))):
        console.print(
            f"[red]Use 1-{DEFAULTS['max_apply_workers']} workers, a batch limit of "
            f"1-{DEFAULTS['max_apply_batch']}, a positive preview limit, and a nonnegative max age.[/red]"
        )
        raise typer.Exit(code=2)
    if min_score is not None and not 0 <= min_score <= 10:
        console.print("[red]--min-score must be between 0 and 10.[/red]")
        raise typer.Exit(code=2)

    utility_modes = sum(bool(value) for value in (mark_applied, mark_failed, reset_failed, reset_previewed, review, resume_waiting))
    if utility_modes > 1 or (utility_modes and (preview or gen or url or dry_run)):
        console.print("[red]Choose only one apply utility mode at a time.[/red]")
        raise typer.Exit(code=2)

    if resume_waiting:
        if continuous or headless or yes or limit is not None or model is not None:
            console.print("[red]--resume is interactive and cannot be combined with --continuous, --headless, --yes, --limit, or --model.[/red]")
            raise typer.Exit(code=2)
        if not sys.stdin.isatty():
            console.print("[red]--resume requires an interactive terminal because you must complete employer login/signup yourself.[/red]")
            raise typer.Exit(code=2)
        from applypilot.apply.launcher import list_waiting_jobs, resume_waiting_job
        try:
            waiting_jobs = list_waiting_jobs()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if not waiting_jobs:
            console.print("No saved employer handoffs are waiting. Run `applypilot apply --review` to inspect other outcomes.")
            return
        table = Table(title="Saved Employer Handoffs", header_style="bold yellow")
        table.add_column("#", justify="right")
        table.add_column("Job")
        table.add_column("Company/source")
        table.add_column("Location")
        table.add_column("Requisition")
        table.add_column("Reason")
        for index, job in enumerate(waiting_jobs, start=1):
            table.add_row(str(index), job.get("title") or "Unknown",
                          job.get("company") or job.get("site") or "Unknown",
                          job.get("location") or "Unknown",
                          job.get("requisition_id") or "—",
                          job.get("apply_error") or "login/signup required")
        console.print(table)
        selection = Prompt.ask("Choose one handoff number (or q to cancel)",
                               choices=[str(i) for i in range(1, len(waiting_jobs) + 1)] + ["q"],
                               default="q")
        if selection == "q":
            return
        try:
            resume_waiting_job(waiting_jobs[int(selection) - 1]["url"])
        except (RuntimeError, OSError, ValueError, ImportError) as exc:
            console.print(f"[red]Could not resume this handoff:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        return

    if mark_applied or mark_failed:
        from applypilot.apply.launcher import mark_job
        try:
            mark_job(mark_applied or mark_failed, "applied" if mark_applied else "failed", reason=fail_reason)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]Updated job status:[/green] {mark_applied or mark_failed}")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if reset_previewed:
        from applypilot.apply.launcher import reset_previewed as do_reset_previewed
        count = do_reset_previewed()
        console.print(f"[green]Requeued {count} browser-rehearsed job(s).[/green]")
        return

    if review:
        from applypilot.database import get_connection
        rows = get_connection().execute("""
            SELECT title, company, site, apply_status, apply_error,
                   COALESCE(application_url, url) AS review_url, last_attempted_at
            FROM jobs
            WHERE apply_status IN ('needs_review', 'awaiting_user', 'uncertain', 'previewed', 'manual', 'captcha', 'expired', 'failed')
            ORDER BY last_attempted_at DESC, title
            LIMIT 100
        """).fetchall()
        if not rows:
            console.print("No application outcomes need review.")
            return
        table = Table(title="Application Outcomes for Manual Review", header_style="bold yellow")
        table.add_column("Status")
        table.add_column("Job")
        table.add_column("Company/source")
        table.add_column("Reason")
        table.add_column("Application URL", overflow="fold")
        for row in rows:
            table.add_row(row["apply_status"], row["title"] or "Unknown", row["company"] or row["site"] or "Unknown",
                          row["apply_error"] or "", row["review_url"] or "")
        console.print(table)
        console.print("Use `applypilot apply --resume` for awaiting-user handoffs. After checking other outcomes, resolve with --mark-applied URL or --mark-failed URL.")
        return

    if not PROFILE_PATH.is_file():
        console.print("[red]Profile not found.[/red] Run applypilot init first.")
        raise typer.Exit(code=1)
    from applypilot.config import load_profile
    try:
        profile_data = load_profile()
        personal = profile_data.get("personal", {})
        if (not isinstance(personal, dict) or not personal.get("full_name")
                or not personal.get("email")
                or not isinstance(profile_data.get("work_authorization"), dict)
                or not isinstance(profile_data.get("compensation"), dict)):
            raise ValueError("profile.json needs personal name/email, work_authorization, and compensation sections")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        console.print(f"[red]Profile is not ready for apply:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if preview and (gen or dry_run or url):
        console.print("[red]--preview is local-only and cannot be combined with --gen, --dry-run, or --url.[/red]")
        raise typer.Exit(code=2)
    if gen and not url:
        console.print("[red]--gen requires --url to specify one job.[/red]")
        raise typer.Exit(code=2)
    if continuous and url:
        console.print("[red]--url cannot be combined with --continuous.[/red]")
        raise typer.Exit(code=2)

    effective_min_score = (7 if min_score is None else min_score) if mode == "tailored" else None

    if preview:
        from applypilot.apply.resume import prepare_base_resume_snapshot
        from applypilot.config import APPLY_RUN_DIR
        run_dir = APPLY_RUN_DIR / f"preview-{uuid4().hex}"
        try:
            snapshot = prepare_base_resume_snapshot(run_dir) if mode == "base" else None
            candidates = list_ready_jobs(
                min_score=effective_min_score, resume_mode=mode,
                limit=preview_limit, max_age_days=max_age_days,
            )
            console.print(f"[bold]Local apply preview ({mode} resume)[/bold]")
            if snapshot:
                console.print(f"Resume SHA-256: {snapshot.sha256}")
            console.print(f"Candidates shown: {len(candidates)} (maximum {preview_limit})")
            if candidates:
                table = Table(title="Eligible Jobs", header_style="bold cyan")
                for heading in ("Title", "Company/source", "Location", "Posted", "Score", "Review"):
                    table.add_column(heading)
                for job in candidates:
                    table.add_row(
                        (job.get("title") or "Unknown")[:50],
                        (job.get("company") or job.get("site") or "Unknown")[:28],
                        (job.get("location") or "Unknown")[:30],
                        job.get("posted_at") or "unknown",
                        str(job.get("fit_score") if job.get("fit_score") is not None else "not scored"),
                        job.get("duplicate_review") or "",
                    )
                console.print(table)
            else:
                console.print("[yellow]No eligible jobs matched the queue filters.[/yellow]")
            console.print("No browser was launched and no external service was contacted.")
        except (OSError, ValueError, ImportError) as exc:
            console.print(f"[red]Preview could not be prepared:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        finally:
            if run_dir.exists():
                import shutil
                shutil.rmtree(run_dir, ignore_errors=True)
        return

    if mode == "tailored" and not (gen and url):
        ready = list_ready_jobs(min_score=effective_min_score, resume_mode="tailored",
                                limit=1, max_age_days=max_age_days)
        if not ready:
            console.print("[red]No tailored resumes are ready.[/red] Run applypilot run score tailor first.")
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        try:
            prompt_file = gen_prompt(
                url, min_score=effective_min_score or 0,
                resume_mode=mode, max_age_days=max_age_days,
            )
        except (OSError, ValueError, ImportError) as exc:
            console.print(f"[red]Could not generate the prompt:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        if not prompt_file:
            console.print("[red]No matching eligible job was found.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote local prompt containing personal data to:[/green] {prompt_file}")
        console.print("This file is for local inspection only; it is not a runnable browser-agent command.")
        return

    candidates = list_ready_jobs(
        target_url=url, min_score=effective_min_score, resume_mode=mode,
        limit=1000, max_age_days=max_age_days,
    )
    if not candidates and not continuous:
        console.print("[red]No eligible jobs matched the selected resume mode and queue filters.[/red]")
        raise typer.Exit(code=1)

    from applypilot.config import check_auto_apply_dependencies
    check_auto_apply_dependencies()
    if mode == "base":
        from applypilot.apply.resume import prepare_base_resume_snapshot
        from applypilot.config import APPLY_RUN_DIR
        preflight_dir = APPLY_RUN_DIR / f"preflight-{uuid4().hex}"
        try:
            snapshot = prepare_base_resume_snapshot(preflight_dir)
            console.print(f"Base resume SHA-256: {snapshot.sha256}")
        except (OSError, ValueError, ImportError) as exc:
            console.print(f"[red]Base resume is not ready:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        finally:
            if preflight_dir.exists():
                import shutil
                shutil.rmtree(preflight_dir, ignore_errors=True)

    effective_limit = limit if limit is not None else (0 if continuous else 1)
    if not continuous and effective_limit < 1:
        console.print("[red]--limit must be at least 1 unless --continuous is used.[/red]")
        raise typer.Exit(code=2)
    jobs_in_scope = "unlimited as eligible jobs arrive" if continuous else str(min(effective_limit, len(candidates)))
    action = "rehearse in browser" if dry_run else "send applications"
    console.print(
        f"This will {action} for {jobs_in_scope} job(s), using the {mode} resume. "
        "ApplyPilot sends your profile, resume, and job details to OpenAI through the active ChatGPT plan, "
        "then uses an isolated Chrome profile to visit the selected employer sites. ChatGPT plan usage is "
        "shared with other apps on your account. The pinned Playwright MCP package may be downloaded from npm. "
        "Employer pages may autosave form data."
    )
    if not yes:
        if not sys.stdin.isatty():
            console.print("[red]Use --yes for an unattended run after reviewing applypilot apply --preview.[/red]")
            raise typer.Exit(code=2)
        if not Confirm.ask("Start browser automation now?", default=False):
            raise typer.Exit()

    from applypilot.apply.launcher import main as apply_main
    try:
        apply_main(
            limit=effective_limit, target_url=url, min_score=effective_min_score,
            headless=headless, model=model, dry_run=dry_run, continuous=continuous,
            workers=workers, resume_mode=mode, max_age_days=max_age_days,
        )
    except (RuntimeError, OSError, ValueError, ImportError) as exc:
        console.print(f"[red]Apply run could not start:[/red] {exc}")
        raise typer.Exit(code=1) from exc

@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Potential base-resume candidates", str(stats["ready_to_apply_base"]))
    summary.add_row("Potential tailored-resume candidates", str(stats["ready_to_apply_tailored"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))
    summary.add_row("Needs review", str(stats["needs_review"]))
    summary.add_row("Awaiting you", str(stats["awaiting_user"]))
    summary.add_row("Uncertain outcome", str(stats["uncertain"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import re
    import shutil
    import subprocess
    from applypilot.config import (
        load_env, load_profile, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
        try:
            saved_profile = load_profile()
            if saved_profile.get("personal", {}).get("password"):
                results.append(("Saved job-site password", warn_mark,
                                "Not used for applications; remove it with 'applypilot profile clear-job-password'"))
        except Exception as exc:
            results.append(("profile.json contents", fail_mark, str(exc)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- ChatGPT plan check for AI stages and the Codex browser agent ---
    from applypilot.siwc import ChatGPTAuth
    try:
        chatgpt_account = ChatGPTAuth().active_account()
        auth = ChatGPTAuth()
        if chatgpt_account and auth.has_active_account():
            model = chatgpt_account.get("selected_model") or "account default"
            results.append(("ChatGPT plan", ok_mark, f"{chatgpt_account.get('email') or 'Connected'} ({model})"))
        elif chatgpt_account:
            results.append(("ChatGPT plan", fail_mark,
                            "Plan permission or secure session unavailable — run 'applypilot auth login'"))
        else:
            results.append(("ChatGPT plan", fail_mark,
                            "Needed for the Codex browser agent and AI stages — run 'applypilot auth login'"))
    except Exception as exc:
        results.append(("ChatGPT plan", fail_mark, f"Account settings unavailable: {exc}"))

    # --- Browser-application requirements ---
    codex_bin = shutil.which("codex")
    if codex_bin:
        results.append(("Codex CLI", ok_mark, codex_bin))
    else:
        results.append(("Codex CLI", fail_mark,
                        "Install Codex and make `codex` available on PATH (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for the pinned Playwright MCP package)
    npx_bin = shutil.which("npx")
    node_bin = shutil.which("node")
    if npx_bin and node_bin:
        try:
            node_version = subprocess.run(
                [node_bin, "--version"], capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            version_match = re.match(r"v?(\d+)", node_version)
            if version_match and int(version_match.group(1)) >= 18:
                results.append(("Node.js / npx", ok_mark, f"{node_version}; {npx_bin}"))
            else:
                results.append(("Node.js / npx", fail_mark, f"Node.js 18+ required; found {node_version or 'unknown'}"))
        except (OSError, subprocess.SubprocessError):
            results.append(("Node.js / npx", fail_mark, "Could not verify Node.js 18 or newer"))
    elif npx_bin:
        results.append(("Node.js / npx", fail_mark, "Node.js 18+ executable not found on PATH"))
    else:
        results.append(("Node.js / npx", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    if codex_bin and npx_bin and node_bin:
        try:
            from applypilot.apply.codex_runtime import validate_runtime_paths
            validate_runtime_paths()
            results.append(("Codex / npx launch paths", ok_mark, "App Server and Playwright MCP entry points found"))
        except Exception as exc:
            results.append(("Codex / npx launch paths", fail_mark, str(exc)))

    try:
        import pypdf  # noqa: F401
        if RESUME_PDF_PATH.exists():
            from applypilot.apply.resume import _extract_pdf_text
            try:
                _extract_pdf_text(RESUME_PDF_PATH)
                results.append(("Base resume PDF", ok_mark, "PDF text extraction is available"))
            except ValueError as exc:
                results.append(("Base resume PDF", fail_mark, str(exc)))
        elif RESUME_PATH.exists():
            results.append(("Base resume", ok_mark, "Text source found; PDF is generated for each apply run"))
    except ImportError:
        results.append(("pypdf", fail_mark, "Install the project dependencies to validate resume PDFs"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → ChatGPT sign-in unlocks AI stages and the Codex browser agent.[/dim]")
    elif tier == 2:
        console.print("[dim]  → Browser automation also needs Codex CLI + Chrome + Node.js.[/dim]")

    console.print()


if __name__ == "__main__":
    app()
