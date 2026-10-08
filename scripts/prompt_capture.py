#!/usr/bin/env python3
"""Capture LEM's prompts exactly as production renders them, and version them.

The second half of prompt tracking (docs/prompt-evals.md); `scripts/prompt_registry.py` finds the
call sites, this file pins what each one SENDS.

Capture calls the real production builder named in a registry entry's ``capture.target`` with the
entry's canonical inputs, under three controls:

* every hermetic guard in ``tests/hermetic.py`` — no DB, Redis, broker, Grid, network, and no second
  OpenAI client — so a builder whose spec forgot a patch fails instead of touching the host;
* a recorder on the shared ``AttributedOpenAI`` client's ``chat.completions.create`` (the one seam
  every call site reaches), answering with the entry's canned ``reply``;
* a pinned environment: flags off, the date frozen, ``random`` seeded — capture is deterministic.

What a prompt's VERSION follows, in ``prompts.lock.json``:

* captured prompt → ``content_hash``: the rendered messages plus the parameters that change behaviour
  (model tier, ``response_format``, ``max_tokens``). Sampling parameters are excluded — several
  builders draw temperature per call, and a resample is not a new prompt.
* not yet captured → ``source_hash`` (`prompt_registry.source_hash`): the enclosing function and the
  module constants it names. Coarser — a refactor moves it — but it means EVERY tracked prompt has a
  version from day one.

``--write`` re-renders, bumps the version of each prompt whose hash moved (keeping the old hash in
``history``), prunes prompts that no longer exist and regenerates the inventory. ``--check`` fails
when the committed lock is stale; the unit lane runs the same check.

Usage:
    python scripts/prompt_capture.py --check
    python scripts/prompt_capture.py --write [--changed-in "PR #123"]
"""

from __future__ import annotations

import os

os.environ.setdefault("LEM_TELEMETRY_MUTED", "1")  # before cqc_lem builds its log handlers

import argparse
import contextlib
import copy
import datetime
import hashlib
import importlib
import json
import pathlib
import random
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent
for _path in (REPO / "src", REPO, REPO / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import prompt_registry as registry  # noqa: E402 - sys.path set above

CAPTURED_DIR = registry.PROMPTS_DIR / "captured"

#: Parameters that are part of the prompt's contract. Anything else on the call (temperature,
#: top_p, penalties, user, metadata) is sampling or attribution and never moves a version.
CONTRACT_PARAMS: tuple[str, ...] = ("model", "response_format", "max_tokens", "tools", "tool_choice")

#: The environment a capture runs in, replacing the caller's. Nothing secret survives into it, and
#: flags resolve to their env fallback, so the same canonical input renders the same text anywhere.
CAPTURE_ENV: dict[str, str] = {
    "POSTHOG_FLAGS_ENABLED": "false",
    "LEM_TELEMETRY_MUTED": "1",
    "OPENAI_API_KEY": "capture-not-a-real-key",
    "LITELLM_BASE_URL": "http://litellm.invalid",
    "ENCRYPTION_REQUIRED": "false",
}
CAPTURE_DATE = "2026-01-05T09:00:00"
CAPTURE_SEED = 1729


def _fake_completion(content: str, model: str) -> Any:
    """Return a real ``ChatCompletion`` whose only choice says ``content``."""
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate({
        "id": "capture", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


#: Imported before the clock is frozen: a module that subclasses ``datetime.date`` at import time
#: (pydantic v1, pulled in by ``ai_helper``'s replicate import) fails under freezegun's fake class,
#: and builders import ``ai_helper`` lazily, inside the call.
PRELOAD: tuple[str, ...] = ("cqc_lem.utilities.ai.client", "cqc_lem.utilities.ai.ai_helper")


@contextlib.contextmanager
def pinned_environment(extra_env: dict[str, str] | None = None,
                       preload: tuple[str, ...] = ()) -> Iterator[None]:
    """Run with ``CAPTURE_ENV`` (plus a spec's own ``env``) as the WHOLE environment.

    ``preload`` modules are imported under that environment but before the clock is frozen.
    """
    from freezegun import freeze_time

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ")}
    env.update(CAPTURE_ENV)
    env.update(extra_env or {})
    state = random.getstate()
    try:
        with patch.dict(os.environ, env, clear=True):
            for module in (*PRELOAD, *preload):
                importlib.import_module(module)
            with freeze_time(CAPTURE_DATE):
                try:
                    from cqc_lem.utilities import flags
                    flags.reset_flag_state()
                except ImportError:
                    pass
                random.seed(CAPTURE_SEED)
                yield
    finally:
        random.setstate(state)


def record_calls(target: str, kwargs: dict[str, Any], reply: str = "",
                 env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Call the production builder ``target`` and return every LLM request it made, in order.

    Each request is a deep copy of the keyword arguments handed to ``chat.completions.create``.

    Raises:
        ValueError: ``target`` is not a dotted ``module.function`` path.
    """
    from tests.hermetic import hermetic_io

    module_name, _, func_name = target.rpartition(".")
    if not module_name:
        raise ValueError(f"capture target {target!r} must be a dotted module.function path")
    calls: list[dict[str, Any]] = []

    def _record(*_args: Any, **call_kwargs: Any) -> Any:
        calls.append(copy.deepcopy(call_kwargs))
        return _fake_completion(reply, str(call_kwargs.get("model", "")))

    with pinned_environment(env, preload=(module_name,)), hermetic_io():
        from cqc_lem.utilities.ai.client import client

        func = getattr(importlib.import_module(module_name), func_name)
        with patch.object(client.chat.completions, "create", side_effect=_record):
            func(**kwargs)
    return calls


def content_hash(call: dict[str, Any]) -> str:
    """Return the version hash of one rendered request: messages plus contract parameters."""
    body = {"messages": call.get("messages", []),
            **{k: call[k] for k in CONTRACT_PARAMS if k in call}}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def capture_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Render one registry entry's canonical input and return its captured record.

    Raises:
        RuntimeError: The builder made no LLM request (its canonical input short-circuited, or its
            failure branch swallowed a missing patch) — a capture of nothing is never written.
    """
    spec = entry["capture"]
    calls = record_calls(spec["target"], dict(spec.get("canonical") or {}),
                         reply=str(spec.get("reply", "")), env=spec.get("env"))
    if not calls:
        raise RuntimeError(f"{entry['id']}: {spec['target']} made no LLM request on its canonical input")
    call = calls[int(spec.get("select", -1))]
    return {
        "prompt_id": entry["id"],
        "target": spec["target"],
        "tier": call.get("model"),
        "content_hash": content_hash(call),
        "messages": call.get("messages", []),
        "params": {k: v for k, v in sorted(call.items()) if k != "messages"},
    }


def next_lock_entry(previous: dict[str, Any] | None, source: str, content: str | None,
                    today: str, changed_in: str | None = None) -> dict[str, Any]:
    """Return a prompt's lock entry after a re-render, bumping the version when its hash moved.

    The version follows ``content`` once a prompt is captured and ``source`` before that. Becoming
    captured is not a change: the first content hash is recorded without a bump.
    """
    if not previous:
        return {"version": 1, "source_hash": source, "content_hash": content,
                "changed_in": changed_in, "history": []}
    entry = dict(previous)
    entry["history"] = list(previous.get("history") or [])
    prev_content = previous.get("content_hash")
    if content is not None and prev_content is not None:
        moved, old = content != prev_content, prev_content
    elif content is None:
        moved, old = source != previous.get("source_hash"), previous.get("source_hash")
    else:
        moved, old = False, None
    if moved:
        entry["history"].append({"version": previous["version"], "hash": old, "retired": today})
        entry["version"] = int(previous["version"]) + 1
        entry["changed_in"] = changed_in
    entry["source_hash"] = source
    entry["content_hash"] = content
    return entry


def build_lock(previous: dict[str, Any], today: str, changed_in: str | None = None,
               captures: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return the full lock for the current tree: one entry per tracked prompt, sorted by id."""
    sites = registry.scan()
    reg = registry.load_registry()
    captures = captures if captures is not None else {}
    lock: dict[str, Any] = {}
    for pid, (site, entry) in sorted(registry.tracked_prompts(sites, reg).items()):
        captured = captures.get(pid)
        lock[pid] = next_lock_entry(previous.get(pid), registry.source_hash(site),
                                    captured["content_hash"] if captured else None,
                                    today, changed_in)
    return lock


def capture_all() -> dict[str, dict[str, Any]]:
    """Capture every tracked prompt that declares a ``capture`` block."""
    sites = registry.scan()
    reg = registry.load_registry()
    return {pid: capture_entry(entry)
            for pid, (_site, entry) in sorted(registry.tracked_prompts(sites, reg).items())
            if entry.get("capture")}


def lock_problems(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return why the committed lock does not match the tree (empty when it does)."""
    problems: list[str] = []
    for pid in sorted(set(committed) | set(current)):
        if pid not in current:
            problems.append(f"{pid}: in prompts.lock.json but no longer a tracked prompt")
        elif pid not in committed:
            problems.append(f"{pid}: tracked prompt missing from prompts.lock.json")
        elif committed[pid] != current[pid]:
            problems.append(f"{pid}: prompt changed since it was versioned")
    return problems


def base_version_problems(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """Return prompts whose hash moved against the base branch without a version bump.

    Guards the lock against a hand edit: ``--write`` always bumps, so this only fires when someone
    rewrote a hash by hand.
    """
    problems: list[str] = []
    for pid, entry in head.items():
        old = base.get(pid)
        if not old:
            continue
        key = "content_hash" if entry.get("content_hash") and old.get("content_hash") else "source_hash"
        if entry.get(key) != old.get(key) and int(entry["version"]) <= int(old["version"]):
            problems.append(f"{pid}: {key} changed but version stayed {entry['version']}")
    return problems


def write_captures(captures: dict[str, dict[str, Any]]) -> None:
    """Rewrite ``captured/`` to exactly the given records."""
    CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
    keep = set()
    for pid, record in captures.items():
        path = CAPTURED_DIR / f"{pid}.json"
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        keep.add(path.name)
    for stale in CAPTURED_DIR.glob("*.json"):
        if stale.name not in keep:
            stale.unlink()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 when the lock is stale")
    mode.add_argument("--write", action="store_true", help="re-render, bump versions, write")
    parser.add_argument("--changed-in", default=None, help='recorded on bumped prompts, e.g. "PR #123"')
    args = parser.parse_args(argv)

    committed = registry.load_json(registry.LOCK_PATH)
    captures = capture_all()
    today = datetime.date.today().isoformat()
    current = build_lock(committed, today, args.changed_in, captures)
    if args.check:
        problems = lock_problems(committed, current)
        for line in problems:
            sys.stdout.write(line + "\n")
        if problems:
            sys.stdout.write("Run: python scripts/prompt_capture.py --write\n")
        return 1 if problems else 0
    registry.LOCK_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_captures(captures)
    registry.main(["--inventory"])
    bumped = [pid for pid, e in current.items() if committed.get(pid, {}).get("version") not in
              (None, e["version"])]
    sys.stdout.write(f"{len(current)} prompts versioned, {len(captures)} captured, "
                     f"{len(bumped)} bumped: {', '.join(bumped) or '-'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
