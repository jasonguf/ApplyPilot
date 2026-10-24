<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a job application pipeline with optional AI scoring and tailoring, plus browser automation. Base-resume apply uses your unchanged resume without scoring or tailoring; it still uses the AI browser agent. Browser runs pause when a required answer or outcome is uncertain, and the user remains responsible for reviewing submissions.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, ChatGPT sign-in
applypilot auth login    # connect or reauthorize a ChatGPT account
applypilot auth accounts # list saved registrations
applypilot auth status   # show the active session and model
applypilot auth models   # list models visible to that account
applypilot auth use-model <slug>
applypilot auth test     # send one small connection-check request
applypilot auth test --codex # send one small test request through Codex (uses shared plan usage)
applypilot browser setup # open ApplyPilot's separate Chrome profile for manual sign-in
applypilot auth pending  # review requests with an uncertain outcome
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run discover enrich # discovery only; no ChatGPT sign-in required
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply --preview # local-only queue preview
applypilot apply --limit 5 # submit up to five applications with the base resume
applypilot apply --resume-mode tailored --limit 5 # use prepared tailored resumes
applypilot apply --dry-run # browser rehearsal; employer pages may autosave
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

For browser automation, install Node.js 18+ and the Codex CLI, for example with `npm install -g @openai/codex`. Keep `codex`, `node`, and `npx` available on `PATH`; `applypilot doctor` checks that the launcher entry points can be resolved.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, an eligible ChatGPT plan sign-in, Codex CLI, Node.js (for npx), and Chrome.

Runs discovery and enrichment, optionally uses ChatGPT-backed AI stages, and can then apply with a base or tailored resume. Browser automation uses Codex App Server with the active ChatGPT plan account.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, an eligible ChatGPT account

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Codex App Server navigates an application, uploads the selected resume, and submits only when supplied facts are sufficient |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| ChatGPT plan sign-in | Scoring, tailoring, cover letters, browser-agent inference | Uses subscription-backed Responses access; plan usage is shared with other apps on the account |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Codex CLI | Browser application agent | Starts an isolated Codex App Server; no separate Codex login or API key is used |
| `pypdf` | Base resume PDF validation | Installed with ApplyPilot |

ApplyPilot uses **Sign in with ChatGPT** for its LLM stages. It does not use an OpenAI API key, Gemini key, or local-model endpoint, and it does not silently fall back to another billing route. ChatGPT plan usage is subject to the selected account's eligibility and usage limits. Plus usage is shared with other apps using the same plan. See [OpenAI's SIWC guide](https://developers.openai.com/siwc/token-sharing-open-source) and [plan usage settings](https://chatgpt.com/#settings/usage).

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
Optional runtime settings such as `APPLYPILOT_LLM_MAX_CALLS` and `APPLYPILOT_LLM_CONCURRENCY`. ChatGPT access tokens are stored in the operating-system credential store, not in `.env`. The legacy `GEMINI_API_KEY`, `OPENAI_API_KEY`, `LLM_URL`, and `LLM_MODEL` settings are ignored.

Inspect available models with `applypilot auth models` and save a selection with `applypilot auth use-model <slug>`. The default is the first model visible to the active account. `APPLYPILOT_LLM_MAX_CALLS` sets an optional local request ceiling for ApplyPilot's direct AI stages (`0` means no local ceiling). `APPLYPILOT_LLM_CONCURRENCY` caps simultaneous direct AI requests (default `2`). Browser automation is separately limited to one job at a time and is bounded by `apply --limit`; these local settings do not count App Server's internal model calls or estimate ChatGPT's remaining usage.

ApplyPilot sends resume text, profile details, and job descriptions to OpenAI when an AI stage needs them. OAuth tokens are stored only in supported OS credential stores (Windows Credential Locker, macOS Keychain, Freedesktop Secret Service, or KDE Wallet); ApplyPilot refuses plaintext and unverified keyring backends. On Linux, the credential service must be installed and unlocked. The direct Responses client sets `store: false` and uses streaming as required by the current SIWC preview. Direct AI requests stop on authorization or plan-usage errors; partial or interrupted responses are discarded and are not automatically resent. If a stream ends without a terminal event, ApplyPilot records an uncertain request locally. Review it with `applypilot auth pending`; `applypilot auth retry-unknown <request-id>` requires confirmation before allowing a later run to resend it.

This subscription-backed route handles text inference for discovery, enrichment, scoring, resume tailoring, cover letters, and the Codex browser agent. The browser agent uses Codex App Server configured with the same SIWC access token through the Responses protocol; Playwright MCP supplies browser controls. The job prompt and browser observations are sent through that model route. It does not use a separate Codex sign-in or API key. Direct Responses requests use text inputs and `instructions`; unsupported generation parameters are not sent.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
The base mode uses one validated snapshot of `resume.pdf` (or generates a PDF from `resume.txt`) for the whole run. It does not require scores, tailored resumes, or cover letters. Browser application runs do require the ChatGPT plan sign-in because Codex App Server uses that account for inference. Each application attempt records the resume SHA-256, model, account fingerprint, profile hash, timestamp, result, and verification evidence in the local database. The snapshot, run manifest, and temporary Codex home are removed when the run ends.

On the first run after this update, ApplyPilot makes a timestamped SQLite backup before adding application-history and job-identity columns.

Start with `applypilot apply --preview`. It reads the local queue and resume without launching a browser or contacting an AI or employer. It excludes known postings older than 30 days by default; posts without a date remain visible and the browser checks whether the job is still open. Use `--max-age-days N` to change the known-posting cutoff.

`applypilot apply --dry-run` is a browser rehearsal, not a local-only preview. It sends your profile, resume, and job details to OpenAI through your ChatGPT plan, may download the pinned Playwright MCP package from npm, uploads the resume to the employer's site, and may autosave form data there. It is instructed not to click the final submit control, but autosave is possible. A normal run asks for confirmation before starting; `--yes` is for unattended runs after reviewing the local preview.

The agent never handles passwords, passkeys, one-time codes, email verification, account recovery, consent, or legal attestations. When an application needs login, signup, or a CAPTCHA, ApplyPilot saves it as `awaiting_user` and continues with other eligible jobs. The local SQLite queue and isolated Chrome profile persist across restarts on the same computer; no cloud queue or session-cookie export is used.

Run `applypilot apply --resume` to choose one saved handoff. The handoff AI can inspect the role and destination but has no form-editing or click tools. ApplyPilot then stops Codex and Playwright before handing you the visible Chrome window. Check the address bar and confirm the employer or application-provider domain before entering credentials. Complete account creation/sign-in, email or MFA verification, CAPTCHA when the site permits, and any decisions that require your consent. After you choose Continue and explicitly authorize this one job, ApplyPilot closes Chrome, reopens the original job URL in the same isolated profile, and starts a fresh Codex session. It preserves the saved resume mode, model/account, and dry-run setting. If the outcome after a possible submission is unclear, it records `uncertain`; check the employer site before retrying. Use `applypilot apply --review` to inspect saved and unresolved outcomes.

Handoff is a local same-computer workflow. If you pause, save/exit on the employer site when available; unsaved page fields may be lost because ApplyPilot closes and reopens Chrome before restarting AI. A job that requires unsupported answers or legal decisions remains for manual review. `--reset-failed` resets only failed attempts; awaiting-user, review, and uncertain records stay out of the automatic queue. Use `--mark-applied URL` or `--mark-failed URL` after checking the employer's application status.

ApplyPilot limits each run to one job by default, caps a batch at 100 jobs, and allows one browser/model worker at a time. It defaults to a 30-day posting cutoff, spaces starts on the same domain, and uses a profile-wide lock to avoid concurrent runs. The isolated Codex home disables shell, web search, and analytics; its only MCP server is `@playwright/mcp@0.0.83` with a restricted browser-tool allowlist. Gmail tools, arbitrary browser JavaScript, unsafe code execution, and shell tools are not enabled. A Playwright origin filter adds a request restriction for the initial job origin, but it does not constrain redirects; ApplyPilot instructs Codex to stop when a redirect leaves the expected application flow. Job-page instructions are treated as untrusted content. Profile answers are never inferred, and a required unsupported answer stops for review.

The ApplyPilot Chrome profile is separate from your regular Chrome profile and is not copied from it. Run `applypilot browser setup` to open it, then sign into employer sites yourself. ApplyPilot only closes Chrome and Codex processes it started; it fails if the local debugging port is already occupied. A match on employer, title, and location across different URLs is flagged for manual review. A match on employer and requisition ID is excluded as a duplicate.

```bash
# Utility modes (no Chrome or Codex process needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --reset-previewed    # requeue browser rehearsals after checking them
applypilot apply --review              # inspect waiting, review, uncertain, and manual outcomes
applypilot apply --gen --url URL       # write a local inspection prompt containing personal data
applypilot profile clear-job-password # remove a password stored by older versions
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot browser setup                # Open isolated Chrome profile for manual sign-in
applypilot auth test --codex             # Test Codex App Server with no browser or employer visit
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation and skip the LLM judge (fewer requests)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply --preview              # Local-only queue preview; no browser or AI
applypilot apply --limit 5              # Base resume, up to five live applications
applypilot apply --resume-mode tailored # Use existing tailored resumes (score >= 7)
applypilot apply --workers 1 --limit 6  # One worker, bounded to the requested limit
applypilot apply --dry-run              # Browser rehearsal; site autosave may occur
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot apply --yes                  # Confirm browser automation in unattended runs
applypilot apply --review               # Inspect outcomes that need manual checking
applypilot apply --resume               # Continue one saved login/signup handoff interactively
applypilot apply --reset-previewed      # Requeue rehearsals after manual checking
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
