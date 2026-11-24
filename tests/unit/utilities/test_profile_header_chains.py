"""Headline and banner locator chains on SDUI-shaped pages (issue #2755). Synthetic fixtures only."""

from bs4 import BeautifulSoup

from cqc_lem.utilities.linkedin import scrapper

URL = "https://www.linkedin.com/in/jane-doe"


def _soup(body: str, title: str = "Jane Doe | LinkedIn") -> BeautifulSoup:
    return BeautifulSoup(f"<html><head><title>{title}</title></head><body>{body}</body></html>",
                         "html.parser")


def _parse(body: str, **kw) -> dict:
    return scrapper.parse_profile_header(_soup(body, **kw), URL, is_main_user=True)


def test_headline_rung_first_text_after_name_without_classes():
    page = ('<main><section><div class="dr0a"><p class="dr0b">Jane Doe</p>'
            '<span>· 1st</span><p class="dr0c">I help founders book calls</p></div></section></main>')
    assert _parse(page)["headline"] == "I help founders book calls"


def test_headline_rung_skips_pronouns_and_punctuation():
    page = '<main><div><h1>Jane Doe</h1><span>·</span><span>She/Her</span><div>Growth lead</div></div></main>'
    assert _parse(page)["headline"] == "Growth lead"


def test_headline_existing_selector_still_wins():
    page = ('<main><div class="mt2 relative"><h1>Jane Doe</h1>'
            '<div class="text-body-medium">Classic headline</div></div><p>other</p></main>')
    assert _parse(page)["headline"] == "Classic headline"


def test_headline_unknown_when_nothing_follows_name():
    assert _parse("<main><p>Jane Doe</p></main>")["headline"] is None


def test_headline_unknown_without_main():
    assert _parse("<div><p>Jane Doe</p><p>Headline</p></div>")["headline"] is None


def test_banner_rung_background_image_is_custom():
    page = ('<main><section><div style="background-image: url(https://x.test/a.jpg)"></div>'
            '<p>Jane Doe</p><p>Headline</p></section></main>')
    assert _parse(page)["banner_state"] == "custom"


def test_banner_rung_img_above_name_is_custom_and_avatar_ignored():
    page = ('<main><section><img src="https://x.test/photo.jpg" alt="Jane Doe profile photo">'
            '<img src="https://x.test/cover.jpg" alt="cover"><p>Jane Doe</p></section></main>')
    assert _parse(page)["banner_state"] == "custom"


def test_banner_rung_default_marker():
    page = ('<main><section><img src="https://x.test/default-cover.png"><p>Jane Doe</p></section></main>')
    assert _parse(page)["banner_state"] == "default"


def test_banner_unknown_when_only_avatar_or_nothing_above_name():
    avatar_only = ('<main><section><img src="https://x.test/p.jpg" alt="profile photo">'
                   '<p>Jane Doe</p></section></main>')
    assert _parse(avatar_only)["banner_state"] == "unknown"
    assert _parse("<main><p>Jane Doe</p></main>")["banner_state"] == "unknown"


def test_banner_ignores_images_outside_main_and_below_name():
    page = ('<header><img src="https://x.test/nav.png"></header>'
            '<main><p>Jane Doe</p><img src="https://x.test/below.jpg"></main>')
    assert _parse(page)["banner_state"] == "unknown"


def test_banner_avatar_whose_alt_is_just_the_name_is_not_a_banner():
    page = ('<main><section><img src="https://x.test/p.jpg" alt="Jane Doe">'
            '<p>Jane Doe</p></section></main>')
    assert _parse(page)["banner_state"] == "unknown"


def test_banner_class_selector_still_first():
    page = ('<main><div class="cover-photo default"></div><p>Jane Doe</p></main>')
    assert _parse(page)["banner_state"] == "default"
