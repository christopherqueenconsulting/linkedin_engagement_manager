"""User-declared likeness attributes → the ONE canonical subject phrase for image prompts.

Issue #744 (Phase 2 of #548), decision 3A. FLUX has no attribute API: an avatar LoRA is
conditioned entirely through prompt text, and at `steps=1000` it does not reliably override an
explicit contradicting gender noun the prompt-authoring LLM invented. So the user's own declared
attributes have to be written into the prompt.

Two rules hold this module together:
  * NOTHING here infers anything. There is no classifier, no heuristic on the user's photos, no
    guess from a name. Every value comes from what the user picked in the SPA.
  * An undeclared attribute renders NOTHING. `subject_clause()` returns "" so the prompt is left
    exactly as it was — an empty clause is the honest answer, an invented one is the bug.
"""
from typing import Optional

# value -> (noun phrase, possessive pronoun). "prefer-not-to-say" is a real, storable choice that
# deliberately contributes no noun — declining to declare must not be read as "unset, ask again".
GENDER_PRESENTATIONS: dict[str, tuple[str, str]] = {
    "man": ("a man", "his"),
    "woman": ("a woman", "her"),
    "non-binary": ("a non-binary person", "their"),
    "prefer-not-to-say": ("", ""),
}

AGE_BANDS: tuple[str, ...] = ("20s", "30s", "40s", "50s", "60s", "70+")

_NEUTRAL_NOUN = "a person"
_NEUTRAL_POSSESSIVE = "their"


def normalize_gender_presentation(value: Optional[str]) -> Optional[str]:
    """Canonical gender-presentation key, or None when unset/unrecognized."""
    key = (value or "").strip().lower().replace("_", "-")
    return key if key in GENDER_PRESENTATIONS else None


def normalize_age_band(value: Optional[str]) -> Optional[str]:
    """Canonical age band, or None when unset/unrecognized."""
    band = (value or "").strip().lower()
    return band if band in AGE_BANDS else None


def _age_phrase(age_band: str, possessive: str) -> str:
    # "70+" is a band, not a decade — spelling it "in his 70+" reads as a typo to the model.
    if age_band == "70+":
        return f"in {possessive} 70s or older"
    return f"in {possessive} {age_band}"


def subject_clause(avatar: Optional[dict]) -> str:
    """One canonical subject phrase from stored, user-declared values — "" when unset.

    e.g. ``"a man in his 40s"``, ``"a woman"``, ``"a person in their 30s"``. The neutral noun is
    used when only the age band was declared: stating an age is not a claim about gender.
    """
    if not avatar:
        return ""
    gender = normalize_gender_presentation(avatar.get("gender_presentation"))
    age_band = normalize_age_band(avatar.get("age_band"))

    noun, possessive = GENDER_PRESENTATIONS.get(gender or "", ("", ""))
    if not noun:
        if not age_band:
            return ""
        noun, possessive = _NEUTRAL_NOUN, _NEUTRAL_POSSESSIVE

    if not age_band:
        return noun
    return f"{noun} {_age_phrase(age_band, possessive)}"


def subject_directive(avatar: Optional[dict]) -> str:
    """The instruction handed to the prompt-authoring LLM so it never invents a conflicting subject.

    Empty when no attributes are declared — the prompt then reads exactly as it did before #744.
    """
    clause = subject_clause(avatar)
    if not clause:
        return ""
    return (
        f"When a person appears in the image it IS the author — {clause}. "
        "Describe that person consistently and never contradict this description.\n\n"
    )


def apply_subject_clause(prompt: str, avatar: Optional[dict]) -> str:
    """Prefix the trigger word + declared subject clause onto a Replicate prompt.

    The trigger word has to lead (it is what activates the LoRA) and the clause sits immediately
    beside it, before the free-form scene description that may otherwise describe someone else.
    """
    trigger = ((avatar or {}).get("trigger_word") or "").strip()
    clause = subject_clause(avatar)
    parts = [p for p in (trigger, clause, (prompt or "").strip()) if p]
    return ", ".join(parts)
