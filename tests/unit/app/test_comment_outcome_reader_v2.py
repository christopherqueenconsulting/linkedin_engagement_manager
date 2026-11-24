"""Comment outcome reader v2 (#2621): sibling replies, expansion chain, author identity."""

from unittest.mock import patch

import pytest

from cqc_lem.app.engagement import posting as P
from cqc_lem.utilities import comment_outcomes as CO


class El:
    def __init__(self, name, left=0, text="", aria="", mention=False):
        self.name, self.left, self.text, self.aria, self.mention = name, left, text, aria, mention
        self.clicks = 0

    def get_attribute(self, _n):
        return self.aria

    def find_elements(self, _by, _css):
        return [object()] if self.mention else []


class Driver:
    def __init__(self, nested=(), actor="", controls=None):
        self.nested = set(nested)  # (parent.name, child.name)
        self.actor = actor
        self.controls = controls or {}
        self.clicked = []

    def execute_script(self, script, *args):
        if script == P._LEFT_JS:
            return args[0].left
        if script == P._ACTOR_HREF_JS:
            return self.actor
        if "contains" in script:
            return (args[0].name, args[1].name) in self.nested
        if "click" in script:
            self.clicked.append(args[0])
            return None
        return None

    def find_elements(self, _by, css):
        return self.controls.get(css, [])


def _items(*specs):
    return [(e, e, a) for e, a in specs]


OURS = El("ours")
ME = "https://www.linkedin.com/in/me"


def test_sibling_replies_counted_until_next_top_level():
    r1, r2, nxt = El("r1", 40), El("r2", 40), El("next", 0)
    items = _items((OURS, ME), (r1, "/in/a"), (r2, "/in/b"), (nxt, "/in/c"))
    out = P._replies_to_comment(Driver(), [OURS], items, "me", siblings=True)
    assert [c.name for c, _ in out] == ["r1", "r2"]


def test_nested_replies_still_work_without_siblings():
    child = El("child", 40)
    items = _items((OURS, ME), (child, "/in/a"))
    out = P._replies_to_comment(Driver(nested={("ours", "child")}), [OURS], items, "me")
    assert [c.name for c, _ in out] == ["child"]


def test_sibling_rule_is_off_for_the_followup_shape():
    r1 = El("r1", 40)
    items = _items((OURS, ME), (r1, "/in/a"))
    assert P._replies_to_comment(Driver(), [OURS], items, "me") == []


def test_mention_only_reply_counted_once_even_if_also_sibling():
    r1 = El("r1", 40, mention=True)
    items = _items((OURS, ME), (r1, "/in/a"))
    out = P._replies_to_comment(Driver(), [OURS], items, "me", siblings=True)
    assert len(out) == 1


def test_mention_only_reply_counted():
    other = El("other", 0, mention=True)
    items = _items((OURS, ME), (other, "/in/a"))
    assert len(P._replies_to_comment(Driver(), [OURS], items, "me")) == 1


def test_expansion_clicks_load_more_without_commentlist_testid():
    more = El("m", text="Load more replies")
    reply = El("r", text="Reply")
    see_more = El("s", text="…see more")
    d = Driver(controls={"main button": [reply, see_more, more]})
    assert P._reply_expansion_controls(d) == [more]


@pytest.mark.parametrize("label", ["Reply", "…more", "…see more", "Reply to Jane's comment"])
def test_expansion_never_matches_reply_or_see_more(label):
    assert not P._REPLY_EXPANSION_LABEL.search(label)


@pytest.mark.parametrize("label", ["Load more comments", "View previous replies", "2 replies",
                                   "Show 3 more replies"])
def test_expansion_matches_load_controls(label):
    assert P._REPLY_EXPANSION_LABEL.search(label)


@pytest.mark.parametrize("href,slug", [("https://www.linkedin.com/company/Acme/?x=1", "acme"),
                                       ("https://www.linkedin.com/in/Jane-Doe", "jane-doe"),
                                       ("https://www.linkedin.com/feed/", "")])
def test_actor_slug(href, slug):
    assert P._actor_slug(href) == slug


def _read(driver, items, monkeypatch, v2=True):
    monkeypatch.setenv("COMMENT_OUTCOME_READER_V2", "1" if v2 else "")
    with patch.object(P, "_load_comment_thread"), patch.object(P, "_comment_items", return_value=items), \
            patch.object(P, "_comment_sort_label", return_value=P._SORT_MOST_RELEVANT), \
            patch.object(P, "_find_our_comment", return_value=OURS), \
            patch.object(P, "_comment_like_count", return_value=0), \
            patch.object(P.time, "sleep"):
        return P._read_comment_outcome(driver, None, 1, "https://x", "me", "text")


@pytest.mark.parametrize("actor,replier,expected", [
    ("https://www.linkedin.com/company/acme", "https://www.linkedin.com/company/acme", True),
    ("https://www.linkedin.com/in/pat", "https://www.linkedin.com/in/pat", True),
    ("https://www.linkedin.com/in/pat", "https://www.linkedin.com/in/someone", False),
])
def test_author_replied_company_and_person(monkeypatch, actor, replier, expected):
    r1 = El("r1", 40)
    d = Driver(actor=actor)
    d.get = lambda url: None
    out = _read(d, _items((OURS, ME), (r1, replier)), monkeypatch)
    assert out["author_replied"] is expected
    assert out["reply_count"] == 1 and out["reader_version"] == 2


def test_our_own_reply_never_counts(monkeypatch):
    r1 = El("r1", 40)
    d = Driver(actor="https://www.linkedin.com/in/me")
    d.get = lambda url: None
    out = _read(d, _items((OURS, ME), (r1, ME)), monkeypatch)
    assert out["reply_count"] == 0 and out["our_reply_sent"] is True
    assert out["author_replied"] is False


def test_flag_off_is_v1_nested_only(monkeypatch):
    r1 = El("r1", 40)
    d = Driver(actor="https://www.linkedin.com/in/pat")
    d.get = lambda url: None
    out = _read(d, _items((OURS, ME), (r1, "https://www.linkedin.com/in/pat")), monkeypatch, v2=False)
    assert out["reply_count"] == 0 and "reader_version" not in out


def test_summary_treats_unscored_as_not_zero():
    rows = [{"status": "checked", "reply_count": None, "author_replied": None, "like_count": 0},
            {"status": "checked", "reply_count": 2, "author_replied": 1, "like_count": 0}]
    s = CO.summarize_outcomes(rows)
    assert s["reply_scored"] == 1 and s["reply_rate"] == 1.0 and s["checked"] == 2


def test_summary_legacy_rows_without_the_key_still_score():
    s = CO.summarize_outcomes([{"status": "checked", "like_count": 0}])
    assert s["reply_scored"] == 1 and s["reply_rate"] == 0.0


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_a):
        pass

    def fetchall(self):
        return self.rows


def _fetch(rows, monkeypatch, v2):
    from contextlib import contextmanager

    from cqc_lem.platform.db.repositories import engagement as E

    @contextmanager
    def cur(**_k):
        yield _Cur(rows)

    monkeypatch.setenv("COMMENT_OUTCOME_READER_V2", "1" if v2 else "")
    with patch.object(E, "db_cursor", cur):
        return E.get_comment_outcomes(1, days=7)


_ROWS = [
    {"log_id": 1, "reader_version": 2, "reply_count": 3, "author_replied": 1},
    {"log_id": 1, "reader_version": 1, "reply_count": 0, "author_replied": 0},
    {"log_id": 2, "reader_version": 1, "reply_count": 0, "author_replied": 0},
]


def test_highest_version_per_log_and_v1_is_unscored(monkeypatch):
    out = {r["log_id"]: r for r in _fetch(_ROWS, monkeypatch, v2=True)}
    assert out[1]["reply_count"] == 3
    assert out[2]["reply_count"] is None and out[2]["author_replied"] is None


def test_v2_rows_dropped_when_flag_off(monkeypatch):
    out = {r["log_id"]: r for r in _fetch(_ROWS, monkeypatch, v2=False)}
    assert out[1]["reader_version"] == 1 and out[1]["reply_count"] is None
