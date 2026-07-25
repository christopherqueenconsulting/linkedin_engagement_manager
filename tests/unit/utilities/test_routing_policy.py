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


# ── the routing decision itself ──

def test_resolve_tier_downroutes_treatment_cohort():
    decision = rp.resolve_tier(rp.TIER_COMPLEX, "content", 1, _policy())
    assert decision == {"tier": rp.TIER_MEDIUM, "base_tier": rp.TIER_COMPLEX,
                        "arm": rp.ARM_TREATMENT, "applied": True, "bucket": "content:lem-complex"}


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
