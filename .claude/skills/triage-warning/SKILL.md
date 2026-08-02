---
name: triage-warning
description: Use when triaging a log_warning, a PostHog $exception, or an auto-filed error-tracking GitHub issue (body contains "posthog-issue-") — decide expected no-op vs real defect and apply the escalation contract.
---

# Triaging a warning / $exception

Decision tree — the contract is **"once is a warning, repeatedly is a defect"** (`utilities/log_escalation.py` promotes 3 repeats in 24h to ERROR + ONE grouped `$exception`; the daily cron then files a GitHub issue):

1. **Expected no-op** (working behaviour that merely didn't apply — a group feed with no sort control, a user with no refresh token, an absent optional element)? → downgrade the call to `log_debug` **inside the owning resolver** — never delete the log line, never leave it as a warning. Mark the PostHog issue resolved.
2. **Real defect**? → fix the cause, keep the warning. The escalation exists precisely to surface slow-burn breakage (a selector missing for 3 days ≠ a one-off blip).
3. **Dedup-key hygiene** when writing/editing warning messages: volatile tokens (URLs, ids, numbers, quoted strings) are masked before fingerprinting, and the key includes `module.function`. Keep distinct failures distinct — step names stay quote- and digit-free, and two different broken selectors must not share one message shape.
4. Only the true owner logs a failure — a caller must not restate what the callee already warned about (double-counting forks issues).
5. Never `capture_exception` an `HTTPException` (4xx is a response, not an issue); use `observability.capture_exception(...)` only for caught-and-not-reraised errors.

Auto-filed issues carry `posthog-issue-<id>` (the dedup marker — leave it in the body) and a PostHog link for the stack trace; browser errors also link a session replay. Cron: `scripts/error_to_issues.sh` daily 08:30, wrapping `scripts/posthog_error_issues.py`.

Authoritative: `docs/error-tracking.md`, `src/cqc_lem/utilities/CLAUDE.md` (level table + escalation contract).
