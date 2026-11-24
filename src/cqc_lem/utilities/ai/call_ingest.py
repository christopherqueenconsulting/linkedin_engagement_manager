"""Call / voice-note ingest core (issue #2629, Phase 1a) — pure functions, no DB, no LLM.

Pipeline: parse a transcript export into ``[{speaker, t0, t1, text}]``, keep only the USER's own
turns, redact third-party identifiers, and verify that every extracted fact quotes the transcript.

Three rules shape everything here:

* **Never guess whose turn it was.** ``select_own_turns`` returns ``"unknown"`` and no turns when
  the user's speaker label is missing or matches nothing; a wrong guess would put a third party's
  words into the user's story bank as their own.
* **Redaction is deterministic and runs before any LLM sees the text.** Emails, phones, URLs and
  the third-party names the caller supplies become typed tokens (``[EMAIL_1]``).
* **A fact with no verifiable quote is dropped**, so an invented specific never reaches review.
"""

import difflib
import json
import os
import re
from typing import Optional

SOURCE_OTTER = "otter"
SOURCE_WISPR = "wispr"

OWN_KNOWN = "known"
OWN_UNKNOWN = "unknown"

# A quote must match the transcript at least this closely (difflib ratio) to count as verified.
QUOTE_MATCH_THRESHOLD = 0.85

# Redacted transcripts are purged once reviewed; this is the hard cap (issue #2629 owner record).
RETENTION_HARD_CAP_DAYS = 7

_TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")
_SRT_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})")
_VOICE_TAG_RE = re.compile(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$", re.IGNORECASE)
_LEADING_SPEAKER_RE = re.compile(r"^([A-Z][\w .'\-]{0,60}?):\s+(.+)$")
# Otter TXT: "Speaker Name  0:05" / "Speaker 1  1:02:03" on its own line, text on the lines below.
_OTTER_HEADER_RE = re.compile(r"^(.{1,80}?)\s{1,}(\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# Every quantifier is bounded, so the repeated group cannot backtrack catastrophically.
_EMAIL_RE = re.compile(r"[\w.+\-]{1,64}@[\w\-]{1,63}(?:\.[\w\-]{1,63}){1,8}")  # preflight: ok
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![\w])(?:\+?\d[\d\-.() ]{7,}\d)(?![\w])")


def call_ingest_enabled() -> bool:
    """Read at call time. Default OFF; ``CALL_INGEST_KILL_SWITCH`` stops ingest regardless.

    The kill switch stops ingest only — the purge job never consults this function.
    """
    if _truthy(os.getenv("CALL_INGEST_KILL_SWITCH")):
        return False
    return _truthy(os.getenv("CALL_INGEST_ENABLED"))


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _seconds(stamp: str) -> Optional[float]:
    """Parse ``H:MM:SS[.mmm]`` or ``M:SS[.mmm]`` to seconds, or None when unreadable."""
    m = _TIME_RE.fullmatch(stamp.strip())
    if not m:
        return None
    hours, minutes, secs, frac = m.groups()
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
    if frac:
        total += int(frac.ljust(3, "0")[:3]) / 1000
    return float(total)


def _turn(speaker: Optional[str], t0: Optional[float], t1: Optional[float], text: str) -> dict:
    return {"speaker": (speaker or "").strip() or None, "t0": t0, "t1": t1,
            "text": re.sub(r"\s+", " ", text).strip()}


def parse_timed_blocks(raw: str) -> list:
    """Parse SRT or WebVTT into turns. A ``<v Name>`` tag or a ``Name:`` prefix names the speaker."""
    turns: list = []
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n").strip())
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if _SRT_RANGE_RE.search(ln)), None)
        if idx is None:
            continue
        rng = _SRT_RANGE_RE.search(lines[idx])
        t0, t1 = _seconds(rng.group(1)), _seconds(rng.group(2))
        speaker, parts = None, []
        for ln in lines[idx + 1:]:
            tagged = _VOICE_TAG_RE.match(ln.strip())
            if tagged:
                speaker, ln = tagged.group(1), tagged.group(2)
            else:
                prefixed = _LEADING_SPEAKER_RE.match(ln.strip())
                if prefixed and speaker is None:
                    speaker, ln = prefixed.group(1), prefixed.group(2)
            parts.append(_TAG_RE.sub("", ln))
        text = " ".join(parts)
        if text.strip():
            turns.append(_turn(speaker, t0, t1, text))
    _fill_end_times(turns)
    return turns


def parse_otter_txt(raw: str) -> list:
    """Parse an Otter TXT export: ``Speaker  0:05`` header lines followed by the spoken text.

    Falls back to ``Speaker: text`` lines when no timestamp headers are present.
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    turns: list = []
    current: Optional[dict] = None
    saw_header = False
    for ln in lines:
        header = _OTTER_HEADER_RE.match(ln.strip()) if ln.strip() else None
        if header and not ln.rstrip().endswith((".", "?", "!", ",")):
            saw_header = True
            if current and current["text"]:
                turns.append(current)
            current = {"speaker": header.group(1).strip(), "t0": _seconds(header.group(2)),
                       "t1": None, "text": ""}
        elif current is not None and ln.strip():
            current["text"] = (current["text"] + " " + ln.strip()).strip()
    if current and current["text"]:
        turns.append(current)
    if saw_header:
        for t in turns:
            t["text"] = re.sub(r"\s+", " ", t["text"]).strip()
        _fill_end_times(turns)
        return turns
    out = []
    for ln in lines:
        m = _LEADING_SPEAKER_RE.match(ln.strip())
        if m:
            out.append(_turn(m.group(1), None, None, m.group(2)))
        elif ln.strip() and out:
            out[-1]["text"] = (out[-1]["text"] + " " + ln.strip()).strip()
    return out


def _fill_end_times(turns: list) -> None:
    for cur, nxt in zip(turns, turns[1:]):
        if cur.get("t1") is None and nxt.get("t0") is not None:
            cur["t1"] = nxt["t0"]


def parse_wispr_text(raw: str, speaker: str = "me") -> list:
    """Wispr Flow voice notes are the user dictating alone: one turn per paragraph, all theirs."""
    paragraphs = [p for p in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")) if p.strip()]
    return [_turn(speaker, None, None, p) for p in paragraphs]


def parse_transcript(raw: str, source_type: str) -> list:
    """Dispatch on the source. Unknown source types raise ``ValueError`` — never a silent guess."""
    if source_type == SOURCE_WISPR:
        return parse_wispr_text(raw)
    if source_type == SOURCE_OTTER:
        if "-->" in raw:
            return parse_timed_blocks(raw)
        return parse_otter_txt(raw)
    raise ValueError(f"unsupported call source: {source_type!r}")


def speakers(turns: list) -> list:
    """Distinct speaker labels in first-seen order (unlabelled turns excluded)."""
    seen: list = []
    for t in turns:
        s = t.get("speaker")
        if s and s not in seen:
            seen.append(s)
    return seen


def _norm_label(label: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(label or "")).strip().casefold()


def select_own_turns(turns: list, own_speaker: Optional[str]) -> tuple:
    """Keep only the user's turns, as ``(turns, OWN_KNOWN | OWN_UNKNOWN)``.

    Exact (case-insensitive) label match only. A missing label, or one that matches no speaker,
    is UNKNOWN with no turns — the caller asks the user rather than letting this guess.
    """
    wanted = _norm_label(own_speaker)
    if not wanted:
        return [], OWN_UNKNOWN
    own = [t for t in turns if _norm_label(t.get("speaker")) == wanted]
    if not own:
        return [], OWN_UNKNOWN
    return own, OWN_KNOWN


def _name_variants(name: str) -> list:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    variants = [" ".join(parts)] if len(parts) > 1 else []
    variants += [p for p in parts if len(p) >= 3]
    return sorted(set(variants), key=len, reverse=True)


def redact(text: str, names: tuple = ()) -> tuple:
    """Replace emails, URLs, phones and the given third-party names with typed tokens.

    Returns ``(redacted_text, mapping)``; ``mapping`` is token -> original and is for the caller's
    own use only — it must never be logged, stored beside the text, or sent anywhere.
    Names are caller-supplied (the other speakers' labels and participants): a capitalised word
    is not guessed to be a name.
    """
    mapping: dict = {}
    counters: dict = {}

    def token(kind: str, original: str) -> str:
        for tok, orig in mapping.items():
            if tok.startswith(f"[{kind}_") and orig.casefold() == original.casefold():
                return tok
        counters[kind] = counters.get(kind, 0) + 1
        tok = f"[{kind}_{counters[kind]}]"
        mapping[tok] = original
        return tok

    out = _EMAIL_RE.sub(lambda m: token("EMAIL", m.group(0)), text)
    out = _URL_RE.sub(lambda m: token("URL", m.group(0)), out)
    out = _PHONE_RE.sub(lambda m: token("PHONE", m.group(0)), out)
    for name in names:
        for variant in _name_variants(str(name or "")):
            out = re.sub(rf"(?<!\w){re.escape(variant)}(?!\w)",
                         lambda m: token("PERSON", m.group(0)), out, flags=re.IGNORECASE)
    return out, mapping


def other_speaker_names(turns: list, own_speaker: Optional[str]) -> tuple:
    """The labels of every non-own speaker — the names ``redact`` must remove."""
    own = _norm_label(own_speaker)
    return tuple(s for s in speakers(turns) if _norm_label(s) != own)


def redact_turns(turns: list, own_speaker: Optional[str]) -> list:
    """Redact every turn, scrubbing the other speakers' names. Speaker labels are redacted too."""
    names = other_speaker_names(turns, own_speaker)
    out = []
    for t in turns:
        text, _ = redact(t.get("text") or "", names)
        speaker = t.get("speaker")
        if speaker and _norm_label(speaker) != _norm_label(own_speaker):
            speaker, _ = redact(speaker, names)
        out.append({**t, "speaker": speaker, "text": text})
    return out


def _words(text: str) -> list:
    return re.findall(r"[\w']+", text.casefold())


def verify_quote(quote: str, transcript: str, threshold: float = QUOTE_MATCH_THRESHOLD) -> bool:
    """True when ``quote`` appears in ``transcript`` — exactly, or fuzzily at ``threshold``.

    Word-normalised, so case, punctuation and whitespace never decide the result. Slides a window
    the quote's own length across the transcript and keeps the best difflib ratio.
    """
    q = _words(quote)
    t = _words(transcript)
    if not q or not t:
        return False
    qs = " ".join(q)
    if qs in " ".join(t):
        return True
    n = len(q)
    if n > len(t):
        return difflib.SequenceMatcher(None, qs, " ".join(t)).ratio() >= threshold
    for i in range(len(t) - n + 1):
        window = " ".join(t[i:i + n])
        matcher = difflib.SequenceMatcher(None, qs, window)
        if (matcher.real_quick_ratio() >= threshold and matcher.quick_ratio() >= threshold
                and matcher.ratio() >= threshold):
            return True
    return False


def filter_verified(candidates: list, transcript: str,
                    threshold: float = QUOTE_MATCH_THRESHOLD) -> tuple:
    """Split extracted candidates into ``(kept, dropped)`` by whether ``source_quote`` verifies."""
    kept, dropped = [], []
    for c in candidates or []:
        quote = str((c or {}).get("source_quote") or "")
        (kept if verify_quote(quote, transcript, threshold) else dropped).append(c)
    return kept, dropped


def transcript_text(turns: list) -> str:
    """The plain text of a turn list, one line per turn — what a quote is verified against."""
    return "\n".join(t.get("text") or "" for t in turns)


def prepare_upload(raw: str, source_type: str, own_speaker: Optional[str] = None) -> dict:
    """Parse, redact and own-turn-filter an uploaded transcript — nothing else is ever stored.

    Returns ``{"state": OWN_KNOWN, "text", "turn_count", "own_speaker"}`` or
    ``{"state": OWN_UNKNOWN, "speakers": [...]}`` when the user's label is missing or matches no
    speaker (the caller asks, this never guesses). Wispr notes are the user alone, so they are
    always known. Only the user's OWN redacted turns are returned: another speaker's words never
    reach storage or an LLM.
    """
    turns = parse_transcript(raw, source_type)
    if source_type == SOURCE_WISPR:
        own_speaker = turns[0].get("speaker") if turns else "me"
    redacted = redact_turns(turns, own_speaker)
    own, state = select_own_turns(redacted, own_speaker)
    if state != OWN_KNOWN:
        return {"state": OWN_UNKNOWN, "speakers": speakers(turns)}
    return {"state": OWN_KNOWN, "text": transcript_text(own), "turn_count": len(own),
            "own_speaker": own_speaker}


_EXTRACT_KINDS = "anecdote, number, opinion, client_win, mistake, artifact"
EXTRACT_SYSTEM_PROMPT = (
    "You extract citable personal facts from the author's own spoken words. "
    "Return JSON: {\"items\": [{\"kind\": one of [" + _EXTRACT_KINDS + "], \"title\": short, "
    "\"body\": one or two sentences restating the fact, \"source_quote\": an EXACT verbatim "
    "excerpt from the text, \"confidence\": 0 to 1}]}. Only facts the text states. Never add, "
    "infer or invent names, numbers or outcomes. Tokens like [PERSON_1] are redactions: keep them "
    "as written. If nothing qualifies return {\"items\": []}."
)


def parse_extraction(raw_json: str) -> list:
    """Items from the model's JSON reply. Unparseable or wrong-shaped output is an empty list."""
    try:
        data = json.loads(raw_json or "")
    except (TypeError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            conf = max(0.0, min(1.0, float(it.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.0
        out.append({"kind": it.get("kind"), "title": it.get("title"), "body": it.get("body"),
                    "source_quote": it.get("source_quote"), "confidence": conf})
    return out
