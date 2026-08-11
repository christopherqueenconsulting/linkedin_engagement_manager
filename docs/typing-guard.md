# The Optional-typing guard — advisory mypy, 13 modules

**Config:** `pyproject.toml` `[tool.mypy]` · **Runner:** `scripts/mypy_check.sh` · **Gate:** none, by
design · **Issue:** #1221 (phase 3 of #1154)

## Why this exists, and what it is NOT

The #1154 audit looked for the `Optional[bool]` truthiness bug — a function that returns
`True` / `False` / `None`, where `None` means *unknown*, read as `if value:` so unknown quietly
answers "no". It found **zero** live instances.

So this is **prevention, and it is deliberately undersold**. It is not a fix for a defect, it is not
a required check, and it is not a step toward typing the tree. It is worth having only while it stays
cheap; the day it costs more than it prevents, delete it.

Concretely:

- **Advisory by construction.** `scripts/mypy_check.sh` always exits 0. There is no workflow that
  runs mypy, and none of the six required contexts on `main` involve it.
- **Scoped, not tree-wide.** `[tool.mypy] files` names 13 modules. Everything they import is read
  for signatures only (`follow_imports = "silent"`), which is what keeps the run to a few seconds.
- **It did not move `.ruff-baseline`.** The ratchet is a separate gate (`docs/docstring-standard.md`)
  and this work must never raise it.

## What is checked

Three settings carry the whole point:

| Setting | What it catches |
|---|---|
| `strict_optional` | `None` reaching a position typed non-Optional — the actual shape of the bug |
| `no_implicit_optional` | `def f(name: str = None)`, which declares a non-Optional parameter that is None half the time |
| `warn_no_return` | a branch that falls off the end, returning an implicit `None` nobody declared |

Everything else is off. Untyped defs, `Any`, missing stubs and unreachable code are all fine here —
grading them would flood the output and nobody would read it again.

## The scope, and how it grows

A module belongs in `files` when **its public returns are genuinely `Optional` and `None` carries a
distinct third meaning** — unknown, unreadable, not-yet-measured. Those are the returns where
truthiness silently converts "I could not tell" into "no". Every entry in `pyproject.toml` states the
contract it protects; keep that up to date, it is the reason the list is legible.

The other rule: **add a module only once it already checks clean.** A scope with known findings in it
is a scope nobody reads, and then the guard is just noise with a config file.

To see whether a module is ready:

```bash
poetry install --with lint
scripts/mypy_check.sh src/cqc_lem/utilities/whatever.py
```

## What the first run actually found

Nothing that misbehaves in production, which is the honest headline. Every finding was a *declaration*
that lied about a value already flowing through it, and every fix was annotation-only:

- `insert_new_log(post_url: str = None)` and three siblings — implicit-Optional parameters whose
  callers pass `None` on purpose, into `logs` columns that are `NULL`-able.
- `update_user_access_token(expires_in: int)` — LinkedIn's refresh response does not always carry
  `expires_in`, the column is `INT NULL`, and the renewal beat already passed `None` through.
- `golden_hour_report(comments_found: int, replies_sent: int)` — the body reads
  `int(comments_found or 0)`, so it always took `None`; only the signature said otherwise.
- `open_message_thread` / `open_addressed_composer` / `_try_messaging_search` — `person_name: str = None`,
  where the body's first line is `(person_name or "").strip()`.
- `_reach_signal` in `suppression.py` — the `Optional[float]` guard was an `any(... is None ...)`
  early return, which is correct but invisible to a reader (and to a checker) three lines later; it
  now filters into a narrowed list instead.

One finding was not about `Optional` at all: `message_thread.py`'s locator tables infer
`list[tuple[Literal[...], str]]` from Selenium's `By` constants, and `list` is invariant, so passing
one to a `list[tuple[str, str]]` parameter is an error. They carry an explicit
`list[tuple[str, str]]` annotation now. That is the tax this check charges, and it is worth knowing
before adding a Selenium-heavy module to the scope.

That is the whole value proposition: the declarations now match what the code does, so the next
`Optional[bool]` return that gets read for truth has something to be checked against.

## Running it

```bash
poetry install --with lint   # the lint group is optional and NOT in the Docker image
scripts/mypy_check.sh
```

The result is 13 files in a couple of seconds. Anything slower means the scope has grown past what
this was meant to be.
