"""Profile audit rubric, scraper fields, and the beat worker (issue #2631, Phase 1)."""

from datetime import datetime, timezone
from unittest.mock import patch

from bs4 import BeautifulSoup

from cqc_lem.utilities import profile_audit as pa, profile_audit_job as job
from cqc_lem.utilities.linkedin import scrapper
from cqc_lem.utilities.linkedin.profile import (
    LinkedInExperience,
    LinkedInPosition,
    LinkedInProfile,
)

GOOD_ABOUT = ("Most agencies post and pray. We build a repeatable LinkedIn engine that books calls. " * 3
              + "Message me to see how it works.")


def _profile(**kw) -> LinkedInProfile:
    return LinkedInProfile(full_name="Jane Doe", **kw)


def test_weights_sum_to_100():
    assert sum(pa.CHECK_WEIGHTS.values()) == 100
    assert len(pa.CHECK_WEIGHTS) == 7


def test_headline_pass_fail_unknown():
    assert pa.check_headline("I help founders book more sales calls", None).status == "pass"
    assert pa.check_headline("Marketing Manager", "Marketing Manager").status == "fail"
    assert pa.check_headline(None).status == "unknown"
    assert pa.check_headline("Senior regional manager of digital operations excellence").status == "warn"
    # headline and job_title share one page element: equality must not turn a long headline into FAIL
    long_title = "Senior regional manager of digital operations excellence"
    assert pa.check_headline(long_title, long_title).status == "warn"


def test_about_pass_warn_fail_unknown():
    assert pa.check_about(GOOD_ABOUT).status == "pass"
    assert pa.check_about(None).status == "unknown"
    assert pa.check_about("Short").status == "fail"
    no_cta = "Most agencies post and pray. " * 10
    assert pa.check_about(no_cta).status == "warn"
    weak = "I am a passionate marketer. " + "We do things for clients every day of the week. " * 8
    assert pa.check_about(weak).status == "fail"


def test_featured_banner_url():
    assert pa.check_featured(3).status == "pass"
    assert pa.check_featured(1).status == "warn"
    assert pa.check_featured(0).status == "fail"
    assert pa.check_featured(None).status == "unknown"
    assert pa.check_banner("custom").status == "pass"
    assert pa.check_banner("default").status == "fail"
    assert pa.check_banner("unknown").status == "unknown"
    assert pa.check_banner(None).status == "unknown"
    assert pa.check_custom_url("jane-doe").status == "pass"
    assert pa.check_custom_url("jane-doe-4a1b2c3d").status == "fail"
    assert pa.check_custom_url(None).status == "unknown"


def test_keywords():
    assert pa.check_keywords("sales", "pipeline", None).status == "unknown"
    assert pa.check_keywords("b2b sales", "pipeline growth", ["b2b", "pipeline"]).status == "pass"
    assert pa.check_keywords("b2b sales", "x", ["b2b", "saas", "crm"]).status == "warn"
    assert pa.check_keywords("hello", "world", ["saas"]).status == "fail"


def test_consistency():
    exp = LinkedInExperience(company_name="Acme", positions=[
        LinkedInPosition(title="Demand generation lead", details=["Built outbound pipeline"])])
    p = _profile(experiences=[exp], skills=["Outbound", "Pipeline"])
    assert pa.check_consistency("Outbound pipeline for SaaS", p).status == "pass"
    assert pa.check_consistency("Pipeline coach", p).status == "warn"
    assert pa.check_consistency("Gardening wizard", p).status == "fail"
    assert pa.check_consistency("Gardening wizard", _profile()).status == "unknown"


def test_unknown_is_not_fail_in_score():
    results = pa.audit_profile(_profile())
    assert all(r.status == "unknown" for r in results)
    assert pa.score_results(results) is None
    only_banner = [pa.check_banner("custom")] + [r for r in results if r.check_id != "banner_custom"]
    assert pa.score_results(only_banner) == 100


def test_score_and_rerun_is_stable():
    prof = _profile(headline="I help founders book sales calls", about=GOOD_ABOUT, featured_count=2,
                    banner_state="custom", custom_url="jane-doe")
    first = pa.audit_profile(prof)
    second = pa.audit_profile(prof)
    assert pa.score_results(first) == pa.score_results(second)
    delta = pa.diff_audits([{"check_id": r.check_id, "status": r.status} for r in first], second)
    assert delta == {"closed": [], "opened": [], "still_open": []}


def test_fixed_finding_closes():
    bad = pa.audit_profile(_profile(featured_count=0))
    prev = [{"check_id": r.check_id, "status": r.status} for r in bad]
    fixed = pa.audit_profile(_profile(featured_count=3))
    delta = pa.diff_audits(prev, fixed)
    assert delta["closed"] == ["featured_proof"]
    assert pa.diff_audits(None, bad)["opened"] == ["featured_proof"]
    assert pa.diff_audits(prev, bad)["still_open"] == ["featured_proof"]


# --- scraper -----------------------------------------------------------------------------------

PAGE = """<html><head><title>Jane Doe | LinkedIn</title></head><body><main>
<div class="profile-background-image"><img src="https://media/cover.jpg"></div>
<div class="mt2 relative"><h1>Jane Doe</h1><div class="text-body-medium">I help founders book calls</div></div>
<section><h2>About</h2><span aria-hidden="true">About</span><span aria-hidden="true">{about}</span></section>
<section><h2>Featured</h2><ul><li>One</li><li>Two</li></ul></section>
</main></body></html>"""


def test_scraper_reads_audit_fields_for_main_user_only():
    soup = BeautifulSoup(PAGE.format(about="Hook line. Message me."), "html.parser")
    url = "https://www.linkedin.com/in/jane-doe/"
    own = scrapper.parse_profile_header(soup, url, is_main_user=True)
    assert own["headline"] == "I help founders book calls"
    assert own["about"] == "Hook line. Message me."
    assert own["featured_count"] == 2
    assert own["banner_state"] == "custom"
    assert own["custom_url"] == "jane-doe"
    other = scrapper.parse_profile_header(soup, url)
    assert not {"headline", "about", "featured_count", "banner_state", "custom_url"} & set(other)


def test_scraper_missing_fields_are_unknown_not_fail():
    soup = BeautifulSoup("<html><head><title>Jane Doe | LinkedIn</title></head><body><main>"
                         "<h1>Jane Doe</h1></main></body></html>", "html.parser")
    own = scrapper.parse_profile_header(soup, "https://www.linkedin.com/in/jane-doe/",
                                        is_main_user=True)
    assert own["about"] is None
    assert own["featured_count"] is None
    assert own["banner_state"] == "unknown"
    prof = LinkedInProfile(**own)
    assert all(r.status != "fail" for r in pa.audit_profile(prof)
               if r.check_id in ("about_hook_cta", "featured_proof", "banner_custom"))


def test_default_banner_and_no_featured_on_rendered_page():
    html = ("<main><h1>Jane Doe</h1><div class='profile-background-image--default'></div>"
            "<section><h2>About</h2><span aria-hidden='true'>x</span></section></main>")
    own = scrapper.parse_profile_header(BeautifulSoup(html, "html.parser"),
                                        "https://www.linkedin.com/in/jane-doe-4a1b2c3d/",
                                        is_main_user=True)
    assert own["banner_state"] == "default"
    assert own["featured_count"] == 0


# --- job ---------------------------------------------------------------------------------------

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def test_period_keys():
    assert job.period_key("weekly", NOW) == "2026-W48"
    assert job.period_key("monthly", NOW) == "2026-11"


def _raw(**kw):
    return (LinkedInProfile(full_name="Jane Doe", **kw).model_dump_json(),)


def test_audit_user_stores_and_skips_existing_and_missing():
    with patch("cqc_lem.utilities.db.profile_audit_exists", return_value=True):
        assert job.audit_user(1, "weekly", NOW) == "exists"
    with patch("cqc_lem.utilities.db.profile_audit_exists", return_value=False), \
            patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id", return_value=None):
        assert job.audit_user(1, "weekly", NOW) == "no_profile"
    with patch("cqc_lem.utilities.db.profile_audit_exists", return_value=False), \
            patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id",
                  return_value=_raw(featured_count=3)), \
            patch("cqc_lem.utilities.db.get_latest_profile_audit",
                  return_value={"id": 7}), \
            patch("cqc_lem.utilities.db.get_profile_audit_findings",
                  return_value=[{"check_id": "featured_proof", "status": "fail"}]), \
            patch("cqc_lem.utilities.db.insert_profile_audit", return_value=9) as ins:
        assert job.audit_user(1, "weekly", NOW) == "audited"
    args, kwargs = ins.call_args
    assert args[1:3] == ("weekly", "2026-W48")
    assert kwargs["previous_audit_id"] == 7
    assert kwargs["closed_count"] == 1


def test_audit_user_failed_insert():
    with patch("cqc_lem.utilities.db.profile_audit_exists", return_value=False), \
            patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id", return_value=_raw()), \
            patch("cqc_lem.utilities.db.get_latest_profile_audit", return_value=None), \
            patch("cqc_lem.utilities.db.insert_profile_audit", return_value=None):
        assert job.audit_user(1, "monthly", NOW) == "failed"


def test_unparseable_profile_reads_no_profile():
    with patch("cqc_lem.utilities.db.profile_audit_exists", return_value=False), \
            patch("cqc_lem.utilities.db.get_linked_in_profile_by_user_id",
                  return_value=("{not json",)):
        assert job.audit_user(1, "weekly", NOW) == "no_profile"


def test_run_profile_audits_respects_flag_and_isolates_errors():
    def fake_flag(key, uid):
        return uid != 2

    def fake_audit(uid, kind, now):
        if uid == 3:
            raise RuntimeError("boom")
        return "audited"

    with patch("cqc_lem.utilities.db.get_active_user_ids", return_value=[1, 2, 3]), \
            patch("cqc_lem.utilities.flags.flag_enabled", side_effect=fake_flag), \
            patch.object(job, "audit_user", side_effect=fake_audit):
        counts = job.run_profile_audits("weekly", NOW)
    assert counts["audited"] == 1 and counts["skipped"] == 1 and counts["failed"] == 1
