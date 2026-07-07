"""Coverage tests for the LinkedInProfile pydantic model and message generation."""

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
    def test_full_name_required(self):
        from cqc_lem.utilities.linkedin.profile import LinkedInProfile
        with pytest.raises(ValidationError):
            LinkedInProfile(full_name="")

    def test_profile_url_must_be_linkedin(self):
        with pytest.raises(ValidationError):
            _profile(profile_url="https://evil.example.com/in/jane")

    def test_valid_linkedin_url_accepted(self):
        p = _profile(profile_url="https://www.linkedin.com/in/jane/")
        assert "linkedin.com/in/jane" in str(p.profile_url)


class TestProperties:
    def test_first_and_last_name(self):
        p = _profile()
        assert p.first_name == "Jane"
        assert p.last_name == "Doe"

    def test_is_1st_connection(self):
        assert _profile(connection="1st").is_1st_connection is True
        assert _profile(connection="2nd").is_1st_connection is False
        assert _profile().is_1st_connection is False

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

    def test_single_mutual_connection(self):
        p = _profile(mutual_connections=["Bob Smith"])
        msg = p.generate_personalized_message()
        assert "mutual connection to Bob Smith" in msg

    def test_multiple_mutual_connections_counts_others(self):
        p = _profile(mutual_connections=["Bob", "Carol", "Dave"])
        msg = p.generate_personalized_message()
        assert "mutual connections like" in msg
        assert "and 1 others" in msg

    def test_mutual_connection_profile_objects_use_full_name(self):
        friend = _profile(full_name="Bob Smith")
        p = _profile(mutual_connections=[friend])
        msg = p.generate_personalized_message()
        assert "Bob Smith" in msg
