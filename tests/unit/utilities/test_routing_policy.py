"""Unit tests for the shared routing decision core (issue #494)."""
import pytest

from cqc_lem.utilities import routing_policy as rp

pytestmark = pytest.mark.unit


def _bucket(**overrides):
    bucket = {
        "id": "content:lem-complex#1",
        "feature": "content",
        "from_tier": rp.TIER_COMPLEX,
        "to_tier": rp.TIER_MEDIUM,
        "state": rp.STATE_EXPERIMENT,
        "cohort_pct": 1.0,
    }
    bucket.update(overrides)
    return bucket


def _policy(bucket=None, enabled=True):
    bucket = bucket or _bucket()
    return {"version": rp.POLICY_VERSION, "enabled": enabled,
            "buckets": {rp.bucket_key(bucket["feature"], bucket["from_tier"]): bucket}}


# ── complexity tier (pre-existing behaviour, now testable) ──

@pytest.mark.parametrize("prompt,expected", [
    ("write a thought leadership post with a framework", rp.TIER_COMPLEX),
    ("industry news commentary for the buyer stage", rp.TIER_COMPLEX),
    ("write a personal story", rp.TIER_MEDIUM),
    ("refine this", rp.TIER_SIMPLE),
    ("summarize briefly as a comma separated short list", rp.TIER_SIMPLE),
])
def test_complexity_tier_signals(prompt, expected):
    assert rp.complexity_tier(prompt) == expected


def test_complexity_tier_escalates_on_length():
    assert rp.complexity_tier("word " * 400) == rp.TIER_MEDIUM
    assert rp.complexity_tier("word " * 900) == rp.TIER_COMPLEX


def test_prompt_text_skips_non_string_content():
    text = rp.prompt_text([{"role": "user", "content": "Hello"},
                           {"role": "user", "content": [{"type": "image_url"}]},
                           "not-a-message"])
    assert text == "hello"


def test_cheaper_tier_bottoms_out():
    assert rp.cheaper_tier(rp.TIER_COMPLEX) == rp.TIER_MEDIUM
    assert rp.cheaper_tier(rp.TIER_MEDIUM) == rp.TIER_SIMPLE
    assert rp.cheaper_tier(rp.TIER_SIMPLE) is None
    assert rp.cheaper_tier("gpt-4o") is None


# ── policy normalization ──

@pytest.mark.parametrize("raw", [None, "nope", {}, {"version": 99, "buckets": {}},
                                {"version": "x"}])
def test_normalize_policy_rejects_unusable(raw):
    assert rp.normalize_policy(raw) == {}


def test_normalize_policy_drops_non_mapping_buckets():
    policy = rp.normalize_policy({"version": 1, "enabled": True,
                                  "buckets": {"a": {"state": "experiment"}, "b": "junk"}})
    assert list(policy["buckets"]) == ["a"]


# ── A/B arm assignment ──

def test_assign_arm_is_deterministic_and_respects_cohort():
    bucket = _bucket(cohort_pct=0.5)
    first = rp.assign_arm(7, bucket)
    assert first == rp.assign_arm(7, bucket)
    assert rp.assign_arm(7, _bucket(cohort_pct=0)) == rp.ARM_CONTROL
    assert rp.assign_arm(7, _bucket(cohort_pct=1.0)) == rp.ARM_TREATMENT


def test_assign_arm_reshuffles_on_a_new_generation():
    """A re-run experiment must not silently re-test the same cohort — the generation salts it."""
    users = range(200)
    first = {u: rp.assign_arm(u, _bucket(cohort_pct=0.5, id="content:lem-complex#1")) for u in users}
    second = {u: rp.assign_arm(u, _bucket(cohort_pct=0.5, id="content:lem-complex#2")) for u in users}
    assert first != second


def test_assign_arm_cohort_share_is_roughly_the_configured_pct():
    bucket = _bucket(cohort_pct=0.25)
    treated = sum(1 for u in range(2000) if rp.assign_arm(u, bucket) == rp.ARM_TREATMENT)
    assert 0.20 < treated / 2000 < 0.30


def test_assign_arm_without_user_is_control():
    assert rp.assign_arm(None, _bucket(cohort_pct=0.5)) == rp.ARM_CONTROL
    assert rp.assign_arm("", _bucket(cohort_pct=0.5)) == rp.ARM_CONTROL
    assert rp.assign_arm(1, None) == rp.ARM_CONTROL
    assert rp.assign_arm(1, _bucket(cohort_pct="junk")) == rp.ARM_CONTROL


def test_assign_arm_treats_the_analytics_sentinel_as_no_user():
    """Requests carry user_id="system" so PostHog has a distinct_id (issue #647). It is not a user,
    so hashing it would drop ALL unattributed traffic into one arm instead of control.
    """
    assert rp.assign_arm(rp.SYSTEM_USER_ID, _bucket(cohort_pct=1.0)) == rp.ARM_CONTROL
    assert rp.assign_arm("System", _bucket(cohort_pct=1.0)) == rp.ARM_CONTROL


def test_a_user_id_is_bucketed_the_same_as_an_int_or_a_string():
    """client.py may send the id as an int; the sentinel forces the field's type to vary. Cohort
    assignment must not flip when it does, or a user changes arm mid-experiment.
    """
    bucket = _bucket(cohort_pct=0.5)
    assert all(rp.assign_arm(u, bucket) == rp.assign_arm(str(u), bucket) for u in range(50))


# ── the routing decision itself ──

def test_resolve_tier_downroutes_treatment_cohort():
    decision = rp.resolve_tier(rp.TIER_COMPLEX, "content", 1, _policy())
    assert decision == {"tier": rp.TIER_MEDIUM, "base_tier": rp.TIER_COMPLEX,
                        "arm": rp.ARM_TREATMENT, "assignment": rp.ASSIGNMENT_HASH,
                        "applied": True, "bucket": "content:lem-complex"}


def test_resolve_tier_leaves_control_cohort_alone():
    decision = rp.resolve_tier(rp.TIER_COMPLEX, "content", 1, _policy(_bucket(cohort_pct=0.0)))
    assert decision["tier"] == rp.TIER_COMPLEX
    assert decision["applied"] is False
    assert decision["arm"] == rp.ARM_CONTROL


@pytest.mark.parametrize("policy", [
    None,
    {},
    _policy(enabled=False),                                    # master switch off
    _policy(_bucket(state=rp.STATE_ROLLED_BACK)),              # rolled back → incumbent tier
    _policy(_bucket(state=rp.STATE_HOLD)),
    _policy(_bucket(feature="comment")),                       # different bucket than the call
    _policy(_bucket(to_tier=rp.TIER_COMPLEX)),                 # never route UP
    _policy(_bucket(to_tier="gpt-4o")),                        # never route off-tier
])
def test_resolve_tier_fails_open(policy):
    decision = rp.resolve_tier(rp.TIER_COMPLEX, "content", 1, policy)
    assert decision["tier"] == rp.TIER_COMPLEX
    assert decision["applied"] is False


def test_resolve_tier_ignores_non_tier_models():
    decision = rp.resolve_tier("lem-image", "content", 1, _policy())
    assert decision["tier"] == "lem-image"
    assert decision["applied"] is False


def test_resolve_tier_matches_the_bucket_by_feature_and_tier():
    policy = _policy(_bucket(feature="comment", from_tier=rp.TIER_MEDIUM,
                             to_tier=rp.TIER_SIMPLE, id="comment:lem-medium#1"))
    assert rp.resolve_tier(rp.TIER_MEDIUM, "comment", 3, policy)["tier"] == rp.TIER_SIMPLE
    assert rp.resolve_tier(rp.TIER_MEDIUM, "content", 3, policy)["tier"] == rp.TIER_MEDIUM
    assert rp.resolve_tier(rp.TIER_COMPLEX, "comment", 3, policy)["tier"] == rp.TIER_COMPLEX


# ── flag-decided arms (issue #652) ──
# The PostHog experiment resolves each user's arm APP-SIDE and hands it to this stdlib-only module
# inside the policy document, so these tests are the whole contract of that hand-off.

def test_flag_arm_reads_the_document_map_with_string_keys():
    """The document round-trips through Redis as JSON, so an int user id comes back as "7". Reading
    it back as 7 would silently lose every assignment.
    """
    bucket = _bucket(cohort_pct=0.1, arms={"7": rp.ARM_TREATMENT, "8": rp.ARM_CONTROL})
    assert rp.flag_arm(7, bucket) == rp.ARM_TREATMENT
    assert rp.flag_arm("7", bucket) == rp.ARM_TREATMENT
    assert rp.flag_arm(8, bucket) == rp.ARM_CONTROL
    assert rp.flag_arm(9, bucket) is None


@pytest.mark.parametrize("arms", [None, "junk", {}, {"7": "TREATMENT"}, {"7": None},
                                  {"7": "experiment"}])
def test_flag_arm_falls_back_rather_than_guessing(arms):
    assert rp.flag_arm(7, _bucket(cohort_pct=0.1, arms=arms)) is None


def test_flag_arm_ignores_the_analytics_sentinel_and_no_user():
    bucket = _bucket(cohort_pct=0.1, arms={rp.SYSTEM_USER_ID: rp.ARM_TREATMENT,
                                           "7": rp.ARM_TREATMENT})
    assert rp.flag_arm(rp.SYSTEM_USER_ID, bucket) is None
    assert rp.flag_arm(None, bucket) is None
    assert rp.flag_arm(7, None) is None


def test_flag_assignment_beats_the_hash_in_both_directions():
    """The flag decides WHO: it can pull a user the hash left in control into the treatment, and it
    can keep the holdout the ramp would have swept up.
    """
    hashed = {u: rp.assign_arm(u, _bucket(cohort_pct=0.5)) for u in range(50)}
    control_user = next(u for u, arm in hashed.items() if arm == rp.ARM_CONTROL)
    treated_user = next(u for u, arm in hashed.items() if arm == rp.ARM_TREATMENT)
    assert rp.assign_arm(control_user, _bucket(cohort_pct=0.5,
                                               arms={str(control_user): rp.ARM_TREATMENT})) \
        == rp.ARM_TREATMENT
    assert rp.assign_arm(treated_user, _bucket(cohort_pct=0.5,
                                               arms={str(treated_user): rp.ARM_CONTROL})) \
        == rp.ARM_CONTROL
    # Even at full cohort the flag's holdout survives — that holdout is what keeps the §D.3 quality
    # gate measurable after adoption.
    assert rp.assign_arm(7, _bucket(cohort_pct=1.0, arms={"7": rp.ARM_CONTROL})) == rp.ARM_CONTROL


def test_a_parked_bucket_cannot_be_started_by_a_flag():
    """cohort_pct is the optimizer's ramp and it decides WHETHER: a rolled-back bucket routes nothing
    no matter what the experiment flag says.
    """
    assert rp.assign_arm(7, _bucket(cohort_pct=0.0, arms={"7": rp.ARM_TREATMENT})) == rp.ARM_CONTROL


def test_resolve_tier_reports_which_side_assigned_the_arm():
    flagged = rp.resolve_tier(rp.TIER_COMPLEX, "content", 7,
                              _policy(_bucket(cohort_pct=0.1, arms={"7": rp.ARM_TREATMENT})))
    assert flagged["applied"] is True
    assert flagged["assignment"] == rp.ASSIGNMENT_FLAG
    unflagged = rp.resolve_tier(rp.TIER_COMPLEX, "content", 7, _policy(_bucket(cohort_pct=1.0)))
    assert unflagged["assignment"] == rp.ASSIGNMENT_HASH


def test_normalize_policy_preserves_the_arms_map():
    """The router reads the document through normalize_policy — dropping `arms` there would silently
    revert every flag assignment to the hash.
    """
    policy = rp.normalize_policy(_policy(_bucket(cohort_pct=0.1, arms={"7": rp.ARM_TREATMENT})))
    assert policy["buckets"]["content:lem-complex"]["arms"] == {"7": rp.ARM_TREATMENT}


def test_the_decision_core_stays_stdlib_only():
    """This file is MOUNTED into the LiteLLM container, which has no LEM package and none of LEM's
    dependencies. #652 moved cohorting to PostHog, which makes `from cqc_lem.utilities.experiments
    import ...` a tempting one-liner here — it would break routing in production the moment the proxy
    reloaded the hook, and nothing else in CI would notice.
    """
    import ast
    import sys
    from pathlib import Path

    tree = ast.parse(Path(rp.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level:
            raise AssertionError("routing_policy.py must not use relative imports — it is mounted "
                                 "outside the package")
    assert "cqc_lem" not in imported
    assert imported <= set(sys.stdlib_module_names), (
        f"non-stdlib imports in routing_policy.py: {imported - set(sys.stdlib_module_names)}")
