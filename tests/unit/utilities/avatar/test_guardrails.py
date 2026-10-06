"""Unit tests for the avatar usage guardrails (issue #744, decision 4A).

These assert the precedence order AND the fail-closed posture: every ambiguous or broken state
must resolve to "don't use the avatar", because the failure mode on the other side is publishing
a synthetic likeness of a real person.
"""
from unittest.mock import patch

import pytest

from cqc_lem.utilities.avatar.guardrails import (
    AVATAR_SURFACE_CAROUSEL,
    AVATAR_SURFACE_NEWSLETTER,
    AVATAR_SURFACE_POST_IMAGE,
    AVATAR_SURFACE_VIDEO,
    avatar_allowed_for,
    avatar_is_usable,
    resolve_avatar_for,
)

pytestmark = pytest.mark.unit

_APPROVED = {"id": 3, "status": "succeeded", "model_ref": "owner/lora:v1",
             "trigger_word": "TOK", "approval_status": "approved"}

_ALL_ON = {"avatar_disabled": False, "avatar_use_post_image": True,
           "avatar_use_carousel": True, "avatar_use_video": True,
           "avatar_use_newsletter": True}
_ALL_OFF = {"avatar_disabled": False, "avatar_use_post_image": False,
            "avatar_use_carousel": False, "avatar_use_video": False,
            "avatar_use_newsletter": False}


def _resolve(surface=AVATAR_SURFACE_POST_IMAGE, *, prefs=None, post_choice=None,
             avatar=_APPROVED, post_id=None, user_id=7, prefs_exc=None):
    with patch("cqc_lem.utilities.db.get_avatar_preferences",
               side_effect=prefs_exc, return_value=prefs if prefs is not None else _ALL_ON), \
         patch("cqc_lem.utilities.db.get_post_use_avatar", return_value=post_choice), \
         patch("cqc_lem.utilities.db.get_active_avatar", return_value=avatar):
        return resolve_avatar_for(user_id, surface=surface, post_id=post_id)


class TestAvatarIsUsable:
    def test_approved_succeeded_with_model_ref(self):
        assert avatar_is_usable(_APPROVED) is True

    @pytest.mark.parametrize("override", [
        {"status": "processing"},
        {"model_ref": None},
        {"approval_status": "pending"},
        {"approval_status": "rejected"},
    ])
    def test_anything_missing_is_unusable(self, override):
        assert avatar_is_usable({**_APPROVED, **override}) is False

    def test_none_is_unusable(self):
        assert avatar_is_usable(None) is False


class TestResolveAvatarFor:
    def test_happy_path_returns_the_avatar(self):
        assert _resolve() == _APPROVED

    def test_no_user_id_is_none(self):
        assert resolve_avatar_for(None, surface=AVATAR_SURFACE_POST_IMAGE) is None
        assert resolve_avatar_for(0, surface=AVATAR_SURFACE_VIDEO) is None

    def test_unknown_surface_raises(self):
        with pytest.raises(ValueError, match="Unknown avatar surface"):
            resolve_avatar_for(7, surface="billboard")

    def test_master_switch_beats_everything(self):
        """'Don't use my avatar' overrides the per-surface opt-ins AND the per-post choice."""
        assert _resolve(prefs={**_ALL_ON, "avatar_disabled": True},
                        post_choice=True, post_id=9) is None

    def test_surface_opt_in_off_is_none(self):
        assert _resolve(AVATAR_SURFACE_CAROUSEL, prefs=_ALL_OFF) is None

    def test_surface_opt_ins_are_independent(self):
        prefs = {**_ALL_OFF, "avatar_use_video": True}
        assert _resolve(AVATAR_SURFACE_VIDEO, prefs=prefs) == _APPROVED
        assert _resolve(AVATAR_SURFACE_POST_IMAGE, prefs=prefs) is None

    def test_newsletter_surface_follows_its_own_opt_in(self):
        assert _resolve(AVATAR_SURFACE_NEWSLETTER, prefs=_ALL_OFF) is None
        prefs = {**_ALL_OFF, "avatar_use_newsletter": True}
        assert _resolve(AVATAR_SURFACE_NEWSLETTER, prefs=prefs) == _APPROVED

    def test_post_level_opt_out_wins_over_an_enabled_surface(self):
        assert _resolve(prefs=_ALL_ON, post_choice=False, post_id=9) is None

    def test_post_level_opt_in_overrides_a_disabled_surface(self):
        assert _resolve(prefs=_ALL_OFF, post_choice=True, post_id=9) == _APPROVED

    def test_post_choice_is_not_read_without_a_post_id(self):
        with patch("cqc_lem.utilities.db.get_avatar_preferences", return_value=_ALL_ON), \
             patch("cqc_lem.utilities.db.get_post_use_avatar") as post_choice, \
             patch("cqc_lem.utilities.db.get_active_avatar", return_value=_APPROVED):
            assert resolve_avatar_for(7, surface=AVATAR_SURFACE_VIDEO) == _APPROVED
        post_choice.assert_not_called()

    def test_unapproved_avatar_is_none_even_with_every_opt_in_on(self):
        """The approval gate is not overridable by a preference — that is the whole point."""
        assert _resolve(avatar={**_APPROVED, "approval_status": "pending"},
                        post_choice=True, post_id=9) is None

    def test_no_active_avatar_is_none(self):
        assert _resolve(avatar=None) is None

    def test_db_failure_fails_closed(self):
        assert _resolve(prefs_exc=RuntimeError("db down")) is None


class TestAvatarAllowedFor:
    def test_boolean_form(self):
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=_APPROVED):
            assert avatar_allowed_for(7, surface=AVATAR_SURFACE_CAROUSEL) is True
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for", return_value=None):
            assert avatar_allowed_for(7, surface=AVATAR_SURFACE_CAROUSEL) is False


class TestAvatarFitsTheConcept:
    """#2249 gauntlet: the likeness only goes in frame when the piece is about the author."""

    @staticmethod
    def _concept(**overrides):
        from types import SimpleNamespace
        fields = dict(treatment="people_scene", audience="agency owners",
                      emotional_beat="quiet dread", thesis="Late invoices starve cash flow.",
                      treatment_rationale="a team in a real situation", chosen_idea="")
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_no_concept_never_fits(self):
        from cqc_lem.utilities.avatar.guardrails import avatar_fits_concept
        assert avatar_fits_concept(None, "I did this myself and my team saw me") is False

    @pytest.mark.parametrize("treatment", ["concrete_scene", "editorial_graphic",
                                           "metaphor_last_resort"])
    def test_only_a_people_scene_can_fit(self, treatment):
        from cqc_lem.utilities.avatar.guardrails import avatar_fits_concept
        concept = self._concept(treatment=treatment, audience="the author")
        assert avatar_fits_concept(concept) is False

    @pytest.mark.parametrize("field,value", [
        ("audience", "founders like the author"),
        ("emotional_beat", "my relief after saying no"),
        ("thesis", "The writer's own mistake cost a client."),
        ("treatment_rationale", "a personal story about a hiring decision"),
        ("chosen_idea", "the author at a whiteboard, mid-explanation"),
    ])
    def test_a_concept_about_the_author_fits(self, field, value):
        from cqc_lem.utilities.avatar.guardrails import avatar_fits_concept
        assert avatar_fits_concept(self._concept(**{field: value})) is True

    def test_a_people_scene_about_other_people_does_not_fit(self):
        from cqc_lem.utilities.avatar.guardrails import avatar_fits_concept
        assert avatar_fits_concept(self._concept(), "Agencies wait 90 days to get paid.") is False

    def test_a_first_person_post_fits(self):
        from cqc_lem.utilities.avatar.guardrails import avatar_fits_concept
        text = "Last year I fired my biggest client. I was scared. It was my best decision."
        assert avatar_fits_concept(self._concept(), text) is True

    def test_one_stray_first_person_word_in_a_long_post_does_not(self):
        from cqc_lem.utilities.avatar.guardrails import avatar_fits_concept
        text = "I think " + "agencies wait ninety days for payment " * 30 + "my take."
        assert avatar_fits_concept(self._concept(), text) is False


class TestResolveAvatarForConcept:
    def _resolve(self, *, avatar, fits, explicit=None, post_id=9):
        from cqc_lem.utilities.avatar.guardrails import resolve_avatar_for_concept
        with patch("cqc_lem.utilities.avatar.guardrails.resolve_avatar_for",
                   return_value=avatar) as policy, \
             patch("cqc_lem.utilities.avatar.guardrails.avatar_fits_concept", return_value=fits), \
             patch("cqc_lem.utilities.db.get_post_use_avatar",
                   side_effect=explicit if isinstance(explicit, Exception) else None,
                   return_value=explicit) as choice:
            out = resolve_avatar_for_concept(1, surface=AVATAR_SURFACE_POST_IMAGE,
                                             concept=object(), source_text="t", post_id=post_id)
        return out, policy, choice

    def test_policy_says_no_is_no(self):
        out, _, choice = self._resolve(avatar=None, fits=True)
        assert out is None
        choice.assert_not_called()

    def test_policy_yes_and_fit_is_the_avatar(self):
        out, policy, choice = self._resolve(avatar=_APPROVED, fits=True)
        assert out == _APPROVED
        assert policy.call_args.kwargs == {"surface": AVATAR_SURFACE_POST_IMAGE, "post_id": 9}
        choice.assert_not_called()

    def test_no_fit_is_none(self):
        out, _, _ = self._resolve(avatar=_APPROVED, fits=False, explicit=None)
        assert out is None

    def test_an_explicit_compose_time_opt_in_still_wins(self):
        out, _, _ = self._resolve(avatar=_APPROVED, fits=False, explicit=True)
        assert out == _APPROVED

    def test_an_unreadable_choice_fails_closed(self):
        out, _, _ = self._resolve(avatar=_APPROVED, fits=False, explicit=RuntimeError("db"))
        assert out is None

    def test_no_post_means_no_explicit_choice(self):
        out, _, choice = self._resolve(avatar=_APPROVED, fits=False, post_id=None)
        assert out is None
        choice.assert_not_called()
