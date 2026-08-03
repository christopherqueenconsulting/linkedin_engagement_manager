"""Unit tests for the rebuilt profile-experience parser (issue #970).

The old parser branched on the number of leading blank strings in an <li>'s split text, so any DOM
change silently produced confidently-wrong companies and titles. These tests pin the shapes the
rebuild has to survive: LinkedIn's doubled a11y markup, a single role, a grouped multi-role company,
and — the one that matters most — a page it does NOT understand returning nothing rather than junk.
"""

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.unit


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _doubled(text: str) -> str:
    """One rendered line, exactly as LinkedIn emits it: visible node + visually-hidden twin."""
    return (f'<span aria-hidden="true">{text}</span>'
            f'<span class="visually-hidden">{text}</span>')


def _entity(*lines: str) -> str:
    inner = "".join(f"<div>{_doubled(line)}</div>" for line in lines)
    return f'<div data-view-name="profile-component-entity">{inner}</div>'


def _page(*entities: str) -> BeautifulSoup:
    return _soup(f"<html><body><main><section>{''.join(entities)}</section></main></body></html>")


SINGLE_ROLE = _entity(
    "Senior Software Engineer",
    "Acme Corp · Full-time",
    "Jan 2020 - Present · 5 yrs 2 mos",
    "San Francisco, CA · Hybrid",
    "Led the payments platform rebuild.",
    "Skills: Python · Kubernetes · Go",
)

GROUPED_ROLES = _entity(
    "Globex Corporation",
    "Full-time · 6 yrs 1 mo",
    "Austin, Texas",
    "Director of Engineering",
    "Mar 2022 - Present · 3 yrs 5 mos",
    "Austin, Texas · On-site",
    "Owns three platform teams.",
    "Engineering Manager",
    "Jul 2019 - Mar 2022 · 2 yrs 9 mos",
    "Grew the team from 4 to 19.",
    "Skills: Coaching · Hiring",
)


class TestVisibleLines:
    def test_drops_the_a11y_twin_of_every_line(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        node = _soup(_entity("Senior Software Engineer", "Acme Corp · Full-time")).find("div")

        assert visible_lines(node) == ["Senior Software Engineer", "Acme Corp · Full-time"]

    def test_falls_back_to_whole_text_when_no_aria_hidden_markup(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        node = _soup("<div><div>Engineer</div><div>Engineer</div><div>Acme</div></div>").find("div")

        # Adjacent duplication is the a11y twin even without the attribute — collapse it once.
        assert visible_lines(node) == ["Engineer", "Acme"]

    def test_drops_chrome_and_logo_alt_lines(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        node = _soup(_entity("Acme Corp logo", "Engineer", "Follow", "…see more")).find("div")

        assert visible_lines(node) == ["Engineer"]

    def test_a_description_sentence_ending_in_logo_is_not_dropped_as_alt_text(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        prose = "Rebuilt the brand system end to end, including a new wordmark and logo"
        node = _soup(_entity("Acme Corp logo", prose)).find("div")

        assert visible_lines(node) == [prose]

    def test_nested_aria_hidden_node_is_not_counted_twice(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        node = _soup('<div><div aria-hidden="true">Engineer'
                     '<span aria-hidden="true">Engineer</span></div></div>').find("div")

        assert visible_lines(node) == ["Engineer"]


class TestParseExperienceEntity:
    def test_single_role_reads_title_company_dates_details_and_skills(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity, visible_lines

        parsed = parse_experience_entity(visible_lines(_soup(SINGLE_ROLE).find("div")))

        assert parsed["company_name"] == "Acme Corp"
        assert len(parsed["positions"]) == 1
        position = parsed["positions"][0]
        assert position["title"] == "Senior Software Engineer"
        assert position["start_date"] == "Jan 2020"
        assert position["end_date"] == "Present"
        assert position["details"] == ["Led the payments platform rebuild."]
        assert position["skills"] == ["Python", "Kubernetes", "Go"]

    def test_location_line_does_not_leak_into_details(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity, visible_lines

        parsed = parse_experience_entity(visible_lines(_soup(SINGLE_ROLE).find("div")))

        assert "San Francisco, CA · Hybrid" not in parsed["positions"][0]["details"]

    def test_grouped_company_splits_into_one_position_per_role(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity, visible_lines

        parsed = parse_experience_entity(visible_lines(_soup(GROUPED_ROLES).find("div")))

        assert parsed["company_name"] == "Globex Corporation"
        titles = [p["title"] for p in parsed["positions"]]
        assert titles == ["Director of Engineering", "Engineering Manager"]
        assert parsed["positions"][0]["details"] == ["Owns three platform teams."]
        assert parsed["positions"][1]["start_date"] == "Jul 2019"
        assert parsed["positions"][1]["end_date"] == "Mar 2022"
        assert parsed["positions"][1]["skills"] == ["Coaching", "Hiring"]

    def test_role_details_are_not_attributed_to_the_wrong_role(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity, visible_lines

        parsed = parse_experience_entity(visible_lines(_soup(GROUPED_ROLES).find("div")))

        assert "Grew the team from 4 to 19." not in parsed["positions"][0]["details"]
        assert parsed["positions"][1]["details"] == ["Grew the team from 4 to 19."]

    def test_employment_type_between_title_and_dates_is_not_read_as_the_title(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        parsed = parse_experience_entity(["Consultant", "Contract", "Feb 2018 - Dec 2019 · 1 yr 11 mos"])

        assert parsed["positions"][0]["title"] == "Consultant"

    def test_a_title_containing_a_comma_is_not_mistaken_for_a_location(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        parsed = parse_experience_entity(["Founder, CEO", "Acme Corp", "Jan 2020 - Present"])

        assert parsed["positions"][0]["title"] == "Founder, CEO"
        assert parsed["company_name"] == "Acme Corp"

    def test_year_only_range_is_still_a_role(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        parsed = parse_experience_entity(["Board Member", "Initech", "2016 - 2019"])

        assert parsed["company_name"] == "Initech"
        assert parsed["positions"][0]["start_date"] == "2016"
        assert parsed["positions"][0]["end_date"] == "2019"

    def test_entity_with_no_date_range_is_not_an_experience(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        assert parse_experience_entity(["Home", "My Network", "Jobs"]) is None

    def test_empty_lines_return_none(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        assert parse_experience_entity([]) is None

    def test_dates_alone_with_no_title_or_company_return_none(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        assert parse_experience_entity(["Jan 2020 - Present · 5 yrs 2 mos"]) is None


class TestParseProfileExperiences:
    def test_parses_every_entity_on_the_page(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        result = parse_profile_experiences(_page(SINGLE_ROLE, GROUPED_ROLES))

        assert [e["company_name"] for e in result] == ["Acme Corp", "Globex Corporation"]

    def test_nested_entities_are_parsed_once_as_the_outer_group(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        # LinkedIn nests one entity per role inside the company entity.
        nested = ('<div data-view-name="profile-component-entity">'
                  f'<div>{_doubled("Globex Corporation")}</div>'
                  '<div data-view-name="profile-component-entity">'
                  f'<div>{_doubled("Director of Engineering")}</div>'
                  f'<div>{_doubled("Mar 2022 - Present · 3 yrs")}</div>'
                  '</div></div>')

        result = parse_profile_experiences(_page(nested))

        assert len(result) == 1
        assert result[0]["company_name"] == "Globex Corporation"
        assert result[0]["positions"][0]["title"] == "Director of Engineering"

    def test_falls_back_to_list_items_when_the_entity_attribute_is_gone(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        li = ("<html><body><main><ul><li>"
              f"<div>{_doubled('Staff Engineer')}</div>"
              f"<div>{_doubled('Initech · Full-time')}</div>"
              f"<div>{_doubled('Jan 2021 - Present · 4 yrs')}</div>"
              "</li></ul></main></body></html>")

        result = parse_profile_experiences(_soup(li))

        assert result[0]["company_name"] == "Initech"
        assert result[0]["positions"][0]["title"] == "Staff Engineer"

    def test_reads_a_fully_sdui_page_with_no_data_view_name_and_no_list_items(self):
        """The catch-up grounding pass found SDUI screens render neither — #970 must survive that."""
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        sdui = ("<html><body><main>"
                "<div data-sdui-screen='com.linkedin.sdui.profile.Experience'>"
                "<div role='listitem'>"
                f"<div>{_doubled('Principal Architect')}</div>"
                f"<div>{_doubled('Umbrella Inc · Full-time')}</div>"
                f"<div>{_doubled('Feb 2017 - Dec 2019 · 2 yrs 11 mos')}</div>"
                "</div></div></main></body></html>")

        result = parse_profile_experiences(_soup(sdui))

        assert result[0]["company_name"] == "Umbrella Inc"
        assert result[0]["positions"][0]["title"] == "Principal Architect"
        assert result[0]["positions"][0]["end_date"] == "Dec 2019"

    def test_unrecognised_page_yields_nothing_rather_than_junk(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        page = _soup("<html><body><main><ul><li>Sign in</li><li>Join now</li></ul></main></body></html>")

        assert parse_profile_experiences(page) == []


class TestGetProfileExperiences:
    def _driver(self, html: str):
        from unittest.mock import MagicMock

        driver = MagicMock()
        driver.current_url = "https://www.linkedin.com/in/someone/"
        driver.page_source = html
        return driver

    def test_returns_parsed_experiences_from_the_details_page(self, monkeypatch):
        from cqc_lem.utilities.linkedin import scrapper

        monkeypatch.setattr(scrapper, "wait_for_ajax", lambda d: None)
        monkeypatch.setattr(scrapper, "window_scroll", lambda d, n, b: None)
        driver = self._driver(f"<html><body><main>{SINGLE_ROLE}</main></body></html>")

        result = scrapper.get_profile_experiences(driver, "https://www.linkedin.com/in/someone/")

        assert result[0]["company_name"] == "Acme Corp"

    def test_profile_with_no_experience_section_is_a_debug_no_op(self, monkeypatch):
        """An empty section is normal — warning on it would file a defect for working behaviour."""
        from cqc_lem.utilities.linkedin import scrapper

        calls = []
        monkeypatch.setattr(scrapper, "wait_for_ajax", lambda d: None)
        monkeypatch.setattr(scrapper, "window_scroll", lambda d, n, b: None)
        monkeypatch.setattr(scrapper, "log_warning", lambda *a, **k: calls.append(a))
        driver = self._driver("<html><body><main><ul><li>Nothing here</li></ul></main></body></html>")

        assert scrapper.get_profile_experiences(driver, "https://www.linkedin.com/in/someone/") == []
        assert calls == []

    def test_dated_page_that_parses_to_nothing_warns_once(self, monkeypatch):
        """Selector rot — the failure mode #970 exists to stop being invisible."""
        from cqc_lem.utilities.linkedin import scrapper

        calls = []
        monkeypatch.setattr(scrapper, "wait_for_ajax", lambda d: None)
        monkeypatch.setattr(scrapper, "window_scroll", lambda d, n, b: None)
        monkeypatch.setattr(scrapper, "log_warning", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(scrapper, "parse_profile_experiences", lambda source: [])
        driver = self._driver(f"<html><body><main>{SINGLE_ROLE}</main></body></html>")

        assert scrapper.get_profile_experiences(driver, "https://www.linkedin.com/in/someone/") == []
        assert len(calls) == 1
