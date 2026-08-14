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

    def test_an_html_comment_is_never_read_as_visible_text(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        # bs4 hands a comment back as a nameless NavigableString, so str() returns its TEXT.
        # Reading it is the same fault as reading the visually-hidden a11y twin.
        node = _soup('<div><!-- Jan 1999 - Dec 2001 archived role -->'
                     '<div>Senior Engineer</div><!DOCTYPE html></div>').find("div")

        assert visible_lines(node) == ["Senior Engineer"]


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

    def test_a_date_range_is_never_emitted_as_a_title(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        # The second role carries only a qualifier above its dates, so the walk back for its title
        # lands on the FIRST role's date line. A blank title is the honest read; the date range
        # is the confidently-wrong one.
        parsed = parse_experience_entity(
            ["Acme Corp", "Senior Engineer", "Jan 2020 - Jan 2022", "Full-time",
             "Jan 2018 - Jan 2020"], grouped=True)

        titles = [p.get("title", "") for p in parsed["positions"]]
        assert "Jan 2020 - Jan 2022" not in titles
        assert titles[0] == "Senior Engineer"

    def test_a_leading_date_range_is_never_read_as_the_company(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        parsed = parse_experience_entity(
            ["Jan 2020 - Jan 2022", "Engineering Manager", "Feb 2018 - Dec 2019"], grouped=True)

        assert parsed["company_name"] == ""


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


def _live_role(title: str, dates: str, duration: str, detail: str, skills: str) -> str:
    """The 2026-08-03 live render: no doubled markup, date range across inline spans, comma skills."""
    start, end = dates.split(" - ")
    return ("<li>"
            f"<div>{title}</div>"
            f'<div><span>{start}</span><span> - </span><span>{end}</span>'
            f'<span> · </span><span>{duration}</span></div>'
            f"<div>{detail}</div>"
            f"<div>Skills: {skills}</div>"
            "</li>")


# Rebuilt verbatim from the live /in/christopherqueen/details/experience/ grounding run on PR #984:
# the footer's three help-link `role='listitem'` nodes that the old ladder picked, beside the real
# entries in `main div[role='list'] li` — a company header followed by its roles as SIBLINGS.
LIVE_PAGE = (
    "<html><body><div data-sdui-screen='com.linkedin.sdui.profile.Experience'>"
    "<main>"
    "<h2>Experience</h2>"
    "<div role='list'>"
    "<li><div>Christopher Queen Consulting</div><div>9 yrs 6 mos</div></li>"
    + _live_role("AI Ethics Guardian", "Mar 2019 - Present", "7 yrs 6 mos",
                 "🛡️ Safeguards against biases in deployed models.",
                 "Compliance and Regulations, Artificial Intelligence for Business, +9 skills")
    + _live_role("AI Solutions Sorcerer", "Mar 2019 - Present", "7 yrs 6 mos",
                 "Builds the automation clients ask for.", "Python, LangChain, +4 skills")
    + _live_role("Magento Expert | E-commerce Dominator", "Mar 2017 - Mar 2023", "6 yrs 1 mo",
                 "For the past 6 years, Christopher Queen Consulting has shipped storefronts.",
                 "Magento, PHP")
    + "</div></main>"
    "<div role='listitem'><div>Questions?</div><div>Visit our Help Center.</div></div>"
    "<div role='listitem'><div>Manage your account and privacy</div><div>Go to your Settings.</div></div>"
    "<div role='listitem'><div>Recommendation transparency</div><div>Learn more.</div></div>"
    "</div></body></html>")


class TestLiveGroundedPage:
    """PR #984's live run: the ladder picked the FOOTER and parsed nothing. These pin why."""

    def test_the_dated_rung_wins_over_more_specific_footer_listitems(self):
        from cqc_lem.utilities.linkedin.scrapper import experience_entity_nodes

        nodes, selector = experience_entity_nodes(_soup(LIVE_PAGE))

        assert selector == "main div[role='list'] li"
        assert len(nodes) == 4  # company header + three roles

    def test_help_center_footer_is_never_an_experience(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(LIVE_PAGE))

        assert all("Help Center" not in str(experience) for experience in parsed)

    def test_every_dated_role_parses_with_its_company_title_and_dates(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(LIVE_PAGE))

        assert [e["company_name"] for e in parsed] == ["Christopher Queen Consulting"] * 3
        titles = [e["positions"][0]["title"] for e in parsed]
        assert titles == ["AI Ethics Guardian", "AI Solutions Sorcerer",
                          "Magento Expert | E-commerce Dominator"]
        assert parsed[2]["positions"][0]["start_date"] == "Mar 2017"
        assert parsed[2]["positions"][0]["end_date"] == "Mar 2023"

    def test_description_and_comma_separated_skills_survive_the_parse(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        first = parse_profile_experiences(_soup(LIVE_PAGE))[0]["positions"][0]

        assert first["details"] == ["🛡️ Safeguards against biases in deployed models."]
        assert first["skills"] == ["Compliance and Regulations",
                                   "Artificial Intelligence for Business"]

    def test_the_section_heading_is_never_read_as_the_company(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(LIVE_PAGE))

        assert all(e["company_name"] != "Experience" for e in parsed)


class TestEntityLadder:
    def test_an_undated_rung_is_still_reported_when_no_rung_has_dates(self):
        """The probe needs to see what DID render — an empty tuple explains nothing."""
        from cqc_lem.utilities.linkedin.scrapper import experience_entity_nodes

        page = _soup("<html><body><main><ul><li>Sign in</li></ul></main></body></html>")

        nodes, selector = experience_entity_nodes(page)

        assert selector == "main li"
        assert len(nodes) == 1

    def test_no_entities_at_all_returns_an_empty_selector(self):
        from cqc_lem.utilities.linkedin.scrapper import experience_entity_nodes

        assert experience_entity_nodes(_soup("<html><body><p>nothing</p></body></html>")) == ([], "")


class TestRenderedLines:
    def test_a_date_range_split_across_inline_spans_is_one_line(self):
        """`get_text("\\n")` shatters it into three, and the date anchor with it."""
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        node = _soup("<li><div><span>Mar 2019</span><span> - </span>"
                     "<span>Present</span><span> · </span><span>7 yrs</span></div></li>").find("li")

        assert visible_lines(node) == ["Mar 2019 - Present · 7 yrs"]

    def test_a_decorative_aria_hidden_icon_does_not_become_the_whole_entity(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity, visible_lines

        node = _soup('<li><span aria-hidden="true">•</span><div>Staff Engineer</div>'
                     "<div>Initech · Full-time</div><div>Jan 2021 - Present</div></li>").find("li")

        assert visible_lines(node) == ["Staff Engineer", "Initech · Full-time", "Jan 2021 - Present"]
        assert parse_experience_entity(visible_lines(node))["company_name"] == "Initech"

    def test_a_line_break_inside_a_description_splits_the_line(self):
        from cqc_lem.utilities.linkedin.scrapper import visible_lines

        node = _soup("<li><div>Ran the migration.<br/>Then wrote it up.</div></li>").find("li")

        assert visible_lines(node) == ["Ran the migration.", "Then wrote it up."]


class TestCompanyContext:
    def test_a_role_inherits_the_company_from_its_container(self):
        """Roles selected as `li` carry no company of their own — the grouping does."""
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        page = _soup("<html><body><main><div role='list'>"
                     "<div><div>Globex Corporation</div><div>6 yrs 1 mo</div>"
                     "<li><div>Director of Engineering</div>"
                     "<div>Mar 2022 - Present · 3 yrs 5 mos</div></li>"
                     "</div></div></main></body></html>")

        parsed = parse_profile_experiences(page)

        assert parsed[0]["company_name"] == "Globex Corporation"
        assert parsed[0]["positions"][0]["title"] == "Director of Engineering"

    def test_a_previous_role_is_never_read_as_the_next_role_s_company(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        page = _soup("<html><body><main><div role='list'>"
                     "<li><div>Consultant</div><div>Initech · Contract</div>"
                     "<div>Jan 2015 - Dec 2016</div></li>"
                     "<li><div>Advisor</div><div>Umbrella Inc</div>"
                     "<div>Jan 2017 - Dec 2018</div></li>"
                     "</div></main></body></html>")

        parsed = parse_profile_experiences(page)

        assert [e["company_name"] for e in parsed] == ["Initech", "Umbrella Inc"]

    def test_a_header_without_a_duration_is_not_treated_as_a_company(self):
        """"Experience" is a heading; inheriting it would label every role with it."""
        from cqc_lem.utilities.linkedin.scrapper import _company_header

        assert _company_header(["Experience"]) == ""
        assert _company_header(["Christopher Queen Consulting", "9 yrs 6 mos"]) == (
            "Christopher Queen Consulting")


# The 2026-08-07 live probe on PR #984: `main li` = 8 flat siblings, `main div[role='list'] li` = 0.
# The company header is NOT one of the `li`s — it is a run of divs beside the <ul> holding the roles,
# so only the FIRST role's leading run reached it and 7 of 8 entities came back with company "".
FLAT_LIVE_PAGE = (
    "<html><body><div data-sdui-screen='com.linkedin.sdui.profile.Experience'>"
    "<main>"
    "<h2>Experience</h2>"
    "<div>"
    "<div>Christopher Queen Consulting</div><div>9 yrs 6 mos</div>"
    "<ul>"
    + _live_role("AI Ethics Guardian", "Mar 2019 - Present", "7 yrs 6 mos",
                 "🛡️ Safeguards against biases in deployed models.",
                 "Compliance and Regulations, Artificial Intelligence for Business, +9 skills")
    + _live_role("AI Solutions Sorcerer", "Mar 2019 - Present", "7 yrs 6 mos",
                 "Builds the automation clients ask for.", "Python, LangChain, +4 skills")
    + _live_role("Data Whisperer", "Mar 2017 - Mar 2023", "6 yrs 1 mo",
                 "Turns warehouses into answers.", "SQL, dbt")
    + "</ul></div></main></div></body></html>")

# Two companies, same flat shape: header + <ul> of roles, twice. The naive carry-forward labels
# every Umbrella role "Initech" — the confidently-wrong row #970 exists to kill.
TWO_COMPANY_FLAT_PAGE = (
    "<html><body><main>"
    "<h2>Experience</h2>"
    "<div>"
    "<div>Initech</div><div>4 yrs 2 mos</div>"
    "<ul>"
    + _live_role("Staff Engineer", "Jan 2013 - Dec 2015", "3 yrs",
                 "Kept the TPS reports running.", "COBOL, Patience")
    + _live_role("Senior Engineer", "Jan 2012 - Dec 2012", "1 yr 2 mos",
                 "Shipped the first release.", "Java")
    + "</ul>"
    "<div>Umbrella Inc</div><div>3 yrs 1 mo</div>"
    "<ul>"
    + _live_role("Head of Research", "Jan 2016 - Dec 2017", "2 yrs",
                 "Ran the lab.", "Virology")
    + _live_role("Principal Scientist", "Jan 2018 - Dec 2018", "1 yr 1 mo",
                 "Published the findings.", "Statistics")
    + "</ul></div></main></body></html>")


class TestFlatLiveShape:
    """#1096: the roles are flat `li` siblings and the company header is NOT one of them."""

    def test_the_flat_role_list_is_the_rung_that_wins(self):
        from cqc_lem.utilities.linkedin.scrapper import experience_entity_nodes

        nodes, selector = experience_entity_nodes(_soup(FLAT_LIVE_PAGE))

        assert selector == "main li"
        assert len(nodes) == 3  # the header is a div run beside the <ul>, never an entity

    def test_every_sibling_role_carries_the_company_above_the_list(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(FLAT_LIVE_PAGE))

        assert [e["company_name"] for e in parsed] == ["Christopher Queen Consulting"] * 3
        assert [e["positions"][0]["title"] for e in parsed] == [
            "AI Ethics Guardian", "AI Solutions Sorcerer", "Data Whisperer"]

    def test_a_predecessor_s_description_does_not_blank_the_company(self):
        """The run between two roles holds the previous role's prose, not an empty gap.

        Grading that as "not a sibling" is what left 7 of 8 entities blank.
        """
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(FLAT_LIVE_PAGE))

        assert parsed[1]["positions"][0]["details"] == ["Builds the automation clients ask for."]
        assert parsed[1]["company_name"] == "Christopher Queen Consulting"

    def test_a_second_company_header_starts_a_new_group(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(TWO_COMPANY_FLAT_PAGE))

        assert [e["company_name"] for e in parsed] == ["Initech", "Initech",
                                                       "Umbrella Inc", "Umbrella Inc"]

    def test_the_section_heading_is_never_inherited_as_the_company(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(FLAT_LIVE_PAGE))

        assert all(e["company_name"] != "Experience" for e in parsed)

    def test_a_role_with_no_header_anywhere_stays_blank(self):
        """A blank is honest; a guessed company is a confidently-wrong row."""
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        page = _soup("<html><body><main><ul>"
                     + _live_role("Consultant", "Jan 2015 - Dec 2016", "2 yrs",
                                  "Advised on the migration.", "Strategy")
                     + _live_role("Advisor", "Jan 2017 - Dec 2018", "2 yrs",
                                  "Advised on the next one.", "Strategy")
                     + "</ul></main></body></html>")

        assert [e["company_name"] for e in parse_profile_experiences(page)] == ["", ""]


def _linked_role(title: str, company: str, dates: str, company_id: str = "8736") -> str:
    """One role of the attribute-free render: an `<a>` to the company page wrapping bare divs."""
    return ("<div>"
            f"<div>{company} logo</div>"
            f"<a href='https://www.linkedin.com/company/{company_id}/'>"
            f"<div><div>{title}</div><div>{company}</div><p>{dates}</p></div>"
            "</a></div>")


# The 2026-08-14 live probe for #1465, on a 2nd/3rd-degree profile: `main li` = 0,
# `data-view-name` absent, and the only `role='listitem'` nodes on the page are the SAME three
# footer help-links PR #984 saw. Every role sits in bare divs inside an `<a>` to the company page,
# carrying no role, data-* or aria-* attribute anywhere in the chain — so no rung of the ladder can
# name it, and production warned "rendered dated entries but none parsed" on a page rendering three.
ATTRIBUTE_FREE_PAGE = (
    "<html><body><div data-sdui-screen='com.linkedin.sdui.profile.Experience'>"
    "<main>"
    "<h2>Experience</h2>"
    "<div><div>"
    + _linked_role("Co-chair", "Gaits Foundation", "2000 – Present", "8736")
    + _linked_role("Founder", "Breakthrough Widgets", "2015 – Present", "1234")
    + _linked_role("Co-founder", "Microtouch", "1975 – 2020", "1035")
    + "</div></div></main>"
    "<div role='listitem'><div>Questions?</div><div>Visit our Help Center.</div></div>"
    "<div role='listitem'><div>Manage your account and privacy</div><div>Go to your Settings.</div></div>"
    "<div role='listitem'><div>Recommendation transparency</div><div>Learn more.</div></div>"
    "</div></body></html>")


class TestAttributeFreeRender:
    """#1465: the render served for a 2nd/3rd-degree profile names no markup the ladder can ask for."""

    def test_the_dated_block_fallback_finds_one_node_per_role(self):
        from cqc_lem.utilities.linkedin.scrapper import _DATED_BLOCK_SELECTOR, experience_entity_nodes

        nodes, selector = experience_entity_nodes(_soup(ATTRIBUTE_FREE_PAGE))

        assert selector == _DATED_BLOCK_SELECTOR
        assert len(nodes) == 3

    def test_every_role_parses_with_its_company_title_and_dates(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(ATTRIBUTE_FREE_PAGE))

        assert [e["company_name"] for e in parsed] == ["Gaits Foundation", "Breakthrough Widgets",
                                                       "Microtouch"]
        assert [e["positions"][0]["title"] for e in parsed] == ["Co-chair", "Founder", "Co-founder"]
        assert parsed[2]["positions"][0]["start_date"] == "1975"
        assert parsed[2]["positions"][0]["end_date"] == "2020"

    def test_the_help_center_footer_is_still_never_an_experience(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        parsed = parse_profile_experiences(_soup(ATTRIBUTE_FREE_PAGE))

        assert all("Help Center" not in str(experience) for experience in parsed)

    def test_a_page_the_ladder_can_read_never_reaches_the_fallback(self):
        """A rung that matches is more precise about where an entity STARTS — it must still win."""
        from cqc_lem.utilities.linkedin.scrapper import experience_entity_nodes

        assert experience_entity_nodes(_soup(LIVE_PAGE))[1] == "main div[role='list'] li"
        assert experience_entity_nodes(_soup(FLAT_LIVE_PAGE))[1] == "main li"

    def test_a_role_block_never_climbs_past_the_section_heading(self):
        """One role on the page means nothing above it holds a second date to stop the climb."""
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        page = _soup("<html><body><main><h2>Experience</h2><div><div>"
                     + _linked_role("Consultant", "Initech", "Jan 2015 – Dec 2016")
                     + "</div></div></main></body></html>")

        parsed = parse_profile_experiences(page)

        assert [e["company_name"] for e in parsed] == ["Initech"]
        assert parsed[0]["positions"][0]["title"] == "Consultant"

    def test_a_page_with_no_dated_line_anywhere_still_reports_what_rendered(self):
        """`unknown` is not drift — the probe and the warning path both need the undated rung."""
        from cqc_lem.utilities.linkedin.scrapper import experience_entity_nodes

        page = _soup("<html><body><main><ul><li>Sign in</li></ul></main></body></html>")

        assert experience_entity_nodes(page)[1] == "main li"

    def test_a_grouped_company_header_above_attribute_free_roles_still_attaches(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_profile_experiences

        page = _soup("<html><body><main><h2>Experience</h2><div>"
                     "<div>Initech</div><div>4 yrs 2 mos</div><div>"
                     + _linked_role("Staff Engineer", "Initech", "Jan 2013 – Dec 2015")
                     + _linked_role("Senior Engineer", "Initech", "Jan 2012 – Dec 2012")
                     + "</div></div></main></body></html>")

        assert [e["company_name"] for e in parse_profile_experiences(page)] == ["Initech", "Initech"]


class TestCompanyForLeading:
    """The positional rule itself — `_company_for_leading` decides group membership."""

    def test_a_header_after_the_last_date_line_starts_a_new_group(self):
        from cqc_lem.utilities.linkedin.scrapper import _company_for_leading

        leading = ["Initech", "4 yrs 2 mos", "Staff Engineer", "Jan 2013 - Dec 2015 · 3 yrs",
                   "Kept the TPS reports running.", "Umbrella Inc", "3 yrs 1 mo"]

        assert _company_for_leading(leading) == "Umbrella Inc"

    def test_no_header_since_the_last_role_inherits_that_role_s_company(self):
        from cqc_lem.utilities.linkedin.scrapper import _company_for_leading

        leading = ["Initech", "4 yrs 2 mos", "Staff Engineer", "Jan 2013 - Dec 2015 · 3 yrs",
                   "Kept the TPS reports running."]

        assert _company_for_leading(leading) == "Initech"

    def test_nothing_header_shaped_resolves_to_blank(self):
        from cqc_lem.utilities.linkedin.scrapper import _company_for_leading

        assert _company_for_leading([]) == ""
        assert _company_for_leading(["Experience"]) == ""
        assert _company_for_leading(["Consultant", "Jan 2015 - Dec 2016", "Advised."]) == ""


class TestSkillsLine:
    def test_comma_separated_skills_drop_the_overflow_chip(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        parsed = parse_experience_entity(
            ["Engineer", "Acme", "Jan 2020 - Present",
             "Skills: Compliance and Regulations, Artificial Intelligence, +9 skills"])

        assert parsed["positions"][0]["skills"] == ["Compliance and Regulations",
                                                    "Artificial Intelligence"]

    def test_a_skills_label_on_its_own_line_still_reads_the_names(self):
        from cqc_lem.utilities.linkedin.scrapper import parse_experience_entity

        parsed = parse_experience_entity(
            ["Engineer", "Acme", "Jan 2020 - Present", "Skills:", "Python · Go"])

        assert parsed["positions"][0]["skills"] == ["Python", "Go"]
        assert parsed["positions"][0]["details"] == []


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

    def test_the_attribute_free_render_parses_instead_of_warning(self, monkeypatch):
        """#1465: three roles rendered, nothing parsed, one grouped defect filed per scrape run."""
        from cqc_lem.utilities.linkedin import scrapper

        calls = []
        monkeypatch.setattr(scrapper, "wait_for_ajax", lambda d: None)
        monkeypatch.setattr(scrapper, "window_scroll", lambda d, n, b: None)
        monkeypatch.setattr(scrapper, "log_warning", lambda *a, **k: calls.append(a))
        driver = self._driver(ATTRIBUTE_FREE_PAGE)

        result = scrapper.get_profile_experiences(driver, "https://www.linkedin.com/in/someone/")

        assert [e["company_name"] for e in result] == ["Gaits Foundation", "Breakthrough Widgets",
                                                       "Microtouch"]
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
