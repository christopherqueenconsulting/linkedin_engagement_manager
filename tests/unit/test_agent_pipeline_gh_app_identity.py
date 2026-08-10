"""Regression tests for the pipeline's GitHub App identity (lib/gh_app_token.sh + tick.sh).

Swapping the runner's credential from the owner's PAT to an App installation token changes WHO the
pipeline is, and tick.sh's trust boundary is written in terms of logins. Two failure modes are
covered here because neither is visible until the flag is flipped in production:

* the runner re-applies `agent:ready` itself (stale-claim reaper, answered Decision Comment) and
  MODE=phasefix files follow-up issues carrying it — as the bot, those writes must still be
  admissible, or the issues they create can never be dispatched again;
* an installation token lives ~1h and is handed to a `claude -p` run that may take CLAUDE_TIMEOUT
  before it pushes, so the refresh skew has to exceed one whole run.

The gates are lifted verbatim from the shipped scripts and executed against stubs, following the
harness in test_agent_pipeline_trust_boundary.py.
"""

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "agent-pipeline"
TICK_SH = PIPELINE / "tick.sh"
LIB_SH = PIPELINE / "lib" / "gh_app_token.sh"
TICK = TICK_SH.read_text(encoding="utf-8")
LIB = LIB_SH.read_text(encoding="utf-8")


def _gates() -> str:
    """The trust-boundary block, lifted verbatim so the test runs the shipped code."""
    block = re.search(r"\nTRUSTED_ASSOCIATIONS=.*?\npr_admissible\(\) \{.*?\n\}\n", TICK, re.S)
    assert block, "trust-boundary helpers not found in tick.sh"
    return block.group(0)


# `gh api repos/../issues/N` -> "<association>\t<login>", the shape author_trusted parses.
_GH_ISSUE = '''
    printf '%s\\t%s\\n' "${ASSOC:-}" "${AUTHOR:-somebody}"
'''

# `gh api repos/../issues/N/timeline` -> one page holding a single `labeled` event by $ACTOR.
_GH_TIMELINE = '''
    printf '[[{"event":"labeled","label":{"name":"%s"},"actor":{"login":"%s"}}]]\\n' \\
      "${LABEL:-agent:ready}" "${ACTOR:-owner-person}"
'''


def _run(tmp_path: Path, body: str, gh_impl: str, env: dict = None) -> subprocess.CompletedProcess:
    """Run the real gates against a stub `gh` that returns whatever the test scripted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(gh_impl), encoding="utf-8")
    gh.chmod(0o755)

    script = (
        'set -uo pipefail\n'
        'SLUG="acme/widget"; OWNER="acme"; ASSIGNEE="owner-person"\n'
        'log() { echo "LOG: $*" >&2; }\n'
        f'{_gates()}\n{textwrap.dedent(body)}'
    )
    run_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    run_env.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env)


class TestTheBotIsTrustedForItsOwnWrites:
    def test_the_bot_login_joins_the_label_allowlist_when_the_app_is_active(self, tmp_path):
        # The reaper and the answered-Decision-Comment lane both re-apply `agent:ready`. Under the
        # app those writes are the bot's; refusing them strands the issue permanently.
        r = _run(tmp_path, 'label_actor_trusted 7 "agent:ready" && echo YES || echo NO',
                 _GH_TIMELINE,
                 {"ACTOR": "lem-agent-pipeline[bot]", "GH_APP_IDENTITY_ACTIVE": "1",
                  "GH_APP_BOT_LOGIN": "lem-agent-pipeline[bot]"})
        assert "YES" in r.stdout

    def test_it_does_not_join_when_the_app_is_not_active(self, tmp_path):
        # PAT mode must be byte-for-byte the old gate: some other bot claiming the login is nobody.
        r = _run(tmp_path, 'label_actor_trusted 7 "agent:ready" && echo YES || echo NO',
                 _GH_TIMELINE,
                 {"ACTOR": "lem-agent-pipeline[bot]", "GH_APP_BOT_LOGIN": "lem-agent-pipeline[bot]"})
        assert "NO" in r.stdout

    def test_a_stranger_is_still_refused_under_the_app(self, tmp_path):
        r = _run(tmp_path, 'label_actor_trusted 7 "agent:ready" && echo YES || echo NO',
                 _GH_TIMELINE,
                 {"ACTOR": "drive-by-contributor", "GH_APP_IDENTITY_ACTIVE": "1",
                  "GH_APP_BOT_LOGIN": "lem-agent-pipeline[bot]"})
        assert "NO" in r.stdout

    def test_an_issue_the_bot_filed_is_workable_despite_its_association(self, tmp_path):
        # A GitHub App is not a repo collaborator, so phasefix's follow-up issues come back NONE.
        r = _run(tmp_path, 'author_trusted 7 && echo YES || echo NO', _GH_ISSUE,
                 {"ASSOC": "NONE", "AUTHOR": "lem-agent-pipeline[bot]",
                  "GH_APP_BOT_LOGIN": "lem-agent-pipeline[bot]"})
        assert "YES" in r.stdout

    def test_an_outsider_issue_is_still_refused(self, tmp_path):
        r = _run(tmp_path, 'author_trusted 7 && echo YES || echo NO', _GH_ISSUE,
                 {"ASSOC": "NONE", "AUTHOR": "drive-by-contributor",
                  "GH_APP_BOT_LOGIN": "lem-agent-pipeline[bot]"})
        assert "NO" in r.stdout

    def test_an_unreadable_association_still_refuses(self, tmp_path):
        r = _run(tmp_path, 'author_trusted 7 && echo YES || echo NO', _GH_ISSUE,
                 {"ASSOC": "", "AUTHOR": "lem-agent-pipeline[bot]",
                  "GH_APP_BOT_LOGIN": "lem-agent-pipeline[bot]"})
        assert "NO" in r.stdout

    def test_the_owner_is_unaffected(self, tmp_path):
        r = _run(tmp_path, 'author_trusted 7 && echo YES || echo NO', _GH_ISSUE,
                 {"ASSOC": "OWNER", "AUTHOR": "owner-person"})
        assert "YES" in r.stdout


class TestTokenLifetime:
    def test_the_refresh_skew_is_less_than_the_timer_interval(self):
        # Under secret isolation the runner is a cache CONSUMER, not a minter. The root timer
        # refreshes the cache every 45 min; the runner's skew only decides whether the cached
        # token is still worth handing out. The skew must be STRICTLY smaller than the timer
        # interval, otherwise the runner could reject a token the timer has not yet refreshed.
        skew = int(re.search(r'GH_APP_TOKEN_SKEW="\$\{GH_APP_TOKEN_SKEW:-(\d+)\}"', LIB).group(1))
        timer_min = int(re.search(r'OnUnitActiveSec=(\d+)min', (PIPELINE / "systemd" / "lem-gh-token.timer").read_text()).group(1))
        assert skew < timer_min * 60, "skew must be smaller than the timer interval to avoid false gaps"

    def test_a_fresh_cached_token_is_reused(self, tmp_path):
        cache = tmp_path / "tok"
        cache.write_text("99999999999 ghs_cached\n", encoding="utf-8")
        r = subprocess.run(
            ["bash", "-c", f'set -uo pipefail\nBASE="{tmp_path}"\n'
                           f'GH_APP_TOKEN_CACHE="{cache}"\n. "{LIB_SH}"\ngh_app_token'],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "BASE": str(tmp_path),
                 "GH_APP_TOKEN_CACHE": str(cache)})
        assert r.stdout == "ghs_cached"

    def test_an_expiring_cached_token_is_not_reused(self, tmp_path):
        # Inside the skew window the cache must miss; with no key present the mint then fails soft
        # (rc 1, nothing printed) rather than handing back a credential about to die.
        cache = tmp_path / "tok"
        cache.write_text("1 ghs_stale\n", encoding="utf-8")
        r = subprocess.run(
            ["bash", "-c", f'set -uo pipefail\nBASE="{tmp_path}"\n'
                           f'GH_APP_TOKEN_CACHE="{cache}"\n. "{LIB_SH}"\ngh_app_token'],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "BASE": str(tmp_path),
                 "GH_APP_TOKEN_CACHE": str(cache)})
        assert r.returncode == 1
        assert "ghs_stale" not in r.stdout


class TestWiring:
    def test_the_credential_path_does_not_source_secrets_in_the_runner(self):
        # Under secret isolation the runner never holds the private key and therefore never needs
        # GH_APP_ID/GH_APP_INSTALLATION_ID from secrets.env. Those secrets are sourced by the root
        # systemd unit via EnvironmentFile=/etc/lem/github-app.env. The runner lib must NOT read
        # $BASE/secrets.env itself, because that file is reachable by the agent's uid.
        assert '[ -f "$BASE/secrets.env" ] && . "$BASE/secrets.env"' not in LIB
        service = (PIPELINE / "systemd" / "lem-gh-token.service").read_text()
        assert "EnvironmentFile=/etc/lem/github-app.env" in service

    def test_the_scope_probe_short_circuits_under_the_app(self):
        # `gh api user` 403s for an installation token, so the OAuth-scope probe would pass by
        # accident. It must say what is true instead.
        block = re.search(r"assert_agent_token_scoped\(\) \{.*?\n\}\n", TICK, re.S).group(0)
        assert 'GH_APP_IDENTITY_ACTIVE' in block

    def test_a_mint_failure_falls_back_instead_of_stopping_the_tick(self):
        assert "falling back to AGENT_GH_TOKEN" in TICK
        assert 'export GH_TOKEN="$tok"' in LIB
