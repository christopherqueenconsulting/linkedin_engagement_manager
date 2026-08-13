"""Coverage tests for the LinkedInProfile pydantic model and message generation.

Validation and the derived properties are input→output contracts, so they are
parametrized tables (issue #1216); the message-body assertions stay plain where each
names a different fragment of the copy.
"""

from datetime import datetime

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _profile(**kw):
    from cqc_lem.utilities.linkedin.profile import LinkedInProfile
    defaults = {"full_name": "Jane Ann Doe"}
    defaults.update(kw)
    return LinkedInProfile(**defaults)


class TestValidators:
    # A profile is only usable if it names someone and, when it carries a URL, that URL is
    # actually LinkedIn — an off-site URL would be scraped as if it were a member page.
    @pytest.mark.parametrize("case_id,fields", [
        ("blank_full_name", {"full_name": ""}),
        ("non_linkedin_profile_url", {"profile_url": "https://evil.example.com/in/jane"}),
    ], ids=["blank_full_name", "non_linkedin_profile_url"])
    def test_rejected_fields(self, case_id, fields):
        with pytest.raises(ValidationError):
            _profile(**fields)

    def test_valid_linkedin_url_accepted(self):
        p = _profile(profile_url="https://www.linkedin.com/in/jane/")
        assert "linkedin.com/in/jane" in str(p.profile_url)


class TestProperties:
    def test_first_and_last_name(self):
        p = _profile()
        assert p.first_name == "Jane"
        assert p.last_name == "Doe"

    # (case id, the connection degree LinkedIn showed, whether it counts as 1st)
    @pytest.mark.parametrize("case_id,connection,expected", [
        ("first_degree", "1st", True),
        ("second_degree", "2nd", False),
        ("unknown_degree", None, False),
    ], ids=["first_degree", "second_degree", "unknown_degree"])
    def test_is_1st_connection(self, case_id, connection, expected):
        kwargs = {"connection": connection} if connection else {}
        assert _profile(**kwargs).is_1st_connection is expected

    def test_profile_summary_full(self):
        p = _profile(job_title="CTO", company_name="Acme", industry="Tech")
        s = p.profile_summary
        assert "working as a CTO" in s and "at Acme" in s and "Tech industry" in s

    def test_profile_summary_minimal_ends_with_period(self):
        assert _profile().profile_summary == "Jane Ann Doe."

    def test_activity_posted_on_format(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInActivity
        a = LinkedInActivity(text="hi", posted=datetime(2026, 7, 6, 12, 0))
        assert a.posted_on == "Jul 06 2026"


class TestGeneratePersonalizedMessage:
    def test_includes_job_company_and_activity(self):
        p = _profile(job_title="CTO", company_name="Acme")
        msg = p.generate_personalized_message(
            recent_activity_message="loved your AI post.", from_name="Chris")
        assert "Hi Jane Ann Doe" in msg
        assert "working as CTO" in msg
        assert "Acme" in msg
        assert "loved your AI post." in msg
        assert msg.endswith("Best regards,\nChris")

    def test_without_activity_uses_fallback_compliment(self):
        msg = _profile().generate_personalized_message()
        assert ("found it insightful" in msg) or ("professional background" in msg)

    # (case id, the mutual connections known, the phrasing that must appear)
    @pytest.mark.parametrize("case_id,mutuals,fragment", [
        ("single", ["Bob Smith"], "mutual connection to Bob Smith"),
        ("multiple_counts_the_rest", ["Bob", "Carol", "Dave"], "and 1 others"),
        # A mutual may arrive as a whole profile rather than a name string.
        ("profile_objects_use_full_name", "<profiles>", "Bob Smith"),
    ], ids=["single", "multiple_counts_the_rest", "profile_objects_use_full_name"])
    def test_mutual_connections_phrasing(self, case_id, mutuals, fragment):
        if mutuals == "<profiles>":
            mutuals = [_profile(full_name="Bob Smith")]
        msg = _profile(mutual_connections=mutuals).generate_personalized_message()
        assert fragment in msg
        if case_id == "multiple_counts_the_rest":
            assert "mutual connections like" in msg
