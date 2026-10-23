"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import os
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
def auth_test() -> None:
    """Send one small request through the selected ChatGPT plan route."""
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
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


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
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

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
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
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

    # --- Tier 2 checks ---
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
                            "Connect your ChatGPT account — run 'applypilot auth login'"))
    except Exception as exc:
        results.append(("ChatGPT plan", fail_mark, f"Account settings unavailable: {exc}"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

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
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (run 'applypilot auth login')[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
