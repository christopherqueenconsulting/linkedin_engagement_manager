"""Unit tests for the duplicate-comment consolidation task + helper."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_RA = "cqc_lem.app.run_automation"


class TestDeleteExtraOwnComments:
    def _driver(self, counts, open_ok=True, delete_ok=True, confirm_ok=True):
        """A driver whose execute_script returns the queued values for the count JS in order, and
        fixed truthy/falsy for the open/delete/confirm JS steps.
        """
        driver = MagicMock()
        count_iter = iter(counts)

        def exec_script(js, *args):
            from cqc_lem.app import run_automation as ra
            if js == ra._COUNT_OWN_COMMENT_MENUS_JS:
                return next(count_iter)
            if js == ra._OPEN_LAST_OWN_COMMENT_MENU_JS:
                return open_ok
            if js == ra._CLICK_DELETE_MENUITEM_JS:
                return delete_ok
            if js == ra._CONFIRM_DELETE_MODAL_JS:
                return confirm_ok
            return None

        driver.execute_script.side_effect = exec_script
        return driver

    def test_single_comment_is_noop(self):
        from cqc_lem.app.run_automation import _delete_extra_own_comments_on_post
        driver = self._driver([1])
        found, deleted = _delete_extra_own_comments_on_post(driver, "Chris Q", dry_run=False)
        assert (found, deleted) == (1, 0)

    def test_dry_run_counts_but_does_not_delete(self):
        from cqc_lem.app.run_automation import _delete_extra_own_comments_on_post
        driver = self._driver([3])
        found, deleted = _delete_extra_own_comments_on_post(driver, "Chris Q", dry_run=True)
        assert (found, deleted) == (3, 0)

    @patch(f"{_RA}.time.sleep")
    @patch(f"{_RA}.wait_for_ajax")
    def test_deletes_down_to_one(self, _ajax, _sleep):
        from cqc_lem.app.run_automation import _delete_extra_own_comments_on_post
        # initial count 3, then 2 after first delete, then 1 → stop. Keeps exactly one.
        driver = self._driver([3, 2, 1])
        found, deleted = _delete_extra_own_comments_on_post(driver, "Chris Q", dry_run=False)
        assert found == 3
        assert deleted == 2

    @patch(f"{_RA}.time.sleep")
    def test_stops_when_delete_item_missing(self, _sleep):
        from cqc_lem.app.run_automation import _delete_extra_own_comments_on_post
        driver = self._driver([2, 2], delete_ok=False)
        found, deleted = _delete_extra_own_comments_on_post(driver, "Chris Q", dry_run=False)
        assert found == 2 and deleted == 0


def _run_task(*, dupes, dry_run=True, full_name="Chris Q", profile_raises=False):
    from cqc_lem.app.run_automation import consolidate_duplicate_comments_for_user
    driver = MagicMock()
    wait = MagicMock()
    profile = MagicMock()
    profile.full_name = full_name
    gcp = (MagicMock(side_effect=Exception("login")) if profile_raises
           else MagicMock(return_value=(driver, wait, "e@x.com", profile)))
    with patch(f"{_RA}.get_duplicate_comment_posts", return_value=dupes), \
         patch(f"{_RA}.get_current_profile", gcp), \
         patch(f"{_RA}._delete_extra_own_comments_on_post", return_value=(2, 0 if dry_run else 1)) as dele, \
         patch(f"{_RA}.quit_gracefully"), \
         patch(f"{_RA}.time.sleep"):
        result = consolidate_duplicate_comments_for_user.run(user_id=1, dry_run=dry_run, hours=168)
    return result, dele, driver


class TestConsolidateTask:
    def test_no_duplicates(self):
        result, dele, _ = _run_task(dupes=[])
        assert "No duplicate" in result
        dele.assert_not_called()

    def test_skips_non_url_synthetic_keys(self):
        # Only a synthetic feed key → nothing navigable to delete.
        result, dele, _ = _run_task(dupes=[("feedpost://abc123", 2, None, None)])
        assert "none have a navigable URL" in result
        dele.assert_not_called()

    def test_dry_run_reports_without_deleting(self):
        dupes = [("https://www.linkedin.com/feed/update/urn:li:activity:1/", 3, None, None)]
        result, dele, driver = _run_task(dupes=dupes, dry_run=True)
        dele.assert_called_once()
        assert dele.call_args[0][2] is True  # dry_run passed through
        assert "would delete" in result
        driver.get.assert_called_once()

    def test_actually_deletes_when_not_dry_run(self):
        dupes = [("https://www.linkedin.com/feed/update/urn:li:activity:2/", 2, None, None)]
        result, dele, _ = _run_task(dupes=dupes, dry_run=False)
        dele.assert_called_once()
        assert dele.call_args[0][2] is False
        assert "deleted 1 extra comment" in result

    def test_login_failure_returns_cleanly(self):
        dupes = [("https://www.linkedin.com/feed/update/urn:li:activity:3/", 2, None, None)]
        result, dele, _ = _run_task(dupes=dupes, profile_raises=True)
        assert "Failed to start" in result
        dele.assert_not_called()
