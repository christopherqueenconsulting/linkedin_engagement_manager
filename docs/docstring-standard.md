# Docstrings & lint — the house standard

**Gate:** `Docstring & Lint Gate` (`.github/workflows/docstring-lint.yml`) — a **ratchet against
`.ruff-baseline`, not yet a required check** · **Rules:** `pyproject.toml` `[tool.ruff.lint]` ·
**Auto-fix lane:** `agent:docfix` → RUNBOOK `MODE=docfix`

## Why this exists

Two problems, one fix.

**The lint gate could not fail.** `.github/workflows/test.yml` ran `ruff check` under
`continue-on-error: true`, so "Run Linting" reported green no matter what. It had to: `pyproject.toml`
carried no `[tool.ruff] line-length`, so ruff measured against its **88-column default** while this
code has always been written to **120** — **24,584** phantom `line-too-long` errors, with every real
violation buried inside them. Setting `line-length = 120` drops the same measurement to 330.

**Docstrings were unowned.** `CLAUDE.md` said "no docstring blocks", which was read as "docstrings are
optional" — but the modules that carry this system's hardest invariants (`stale_invites.py`,
`log_escalation.py`, `human_pacing.py`) are already carrying long, careful ones, because that is where
the reasoning lives. The rule was never "don't explain"; it was "don't pad". This standard says that
out loud and makes a machine check the shape.

## The rule

Ruff enforces `E` (pycodestyle), `F` (pyflakes), `I` (import order), `T201` (no `print`) and **`D`
(pydocstyle) under the Google convention** across `src/` and `tests/`.

**Tests are exempt from the *missing*-docstring rules** (`D100`–`D107`) and only bound by the format
ones. A test's name is its documentation, and this suite already writes a docstring wherever the
reason for a test is not obvious — demanding one on all ~10,700 would manufacture the exact noise
this standard is trying to prevent. That exemption is in `[tool.ruff.lint.per-file-ignores]`, with
the reason next to it.

## What a good docstring looks like here

A docstring answers **why**, and what a caller can rely on. The shape is Google's; the content is
this repo's.

```python
def control_names_row(label_name: str, row_text: str) -> bool:
    """Does the Withdraw control's own label name the person this row is about?

    The live control's label is "Withdraw invitation sent to <Name>" — so unlike almost every other
    surface, this one states WHO the click acts on, and that reading is checked rather than assumed.
    A withdrawal is one-way (LinkedIn blocks re-inviting for weeks), and #1012 cost ~20 invites by
    clicking a control whose label named someone other than the target.

    An empty `label_name` is the unlabelled fallback shape, which carries no name to contradict —
    that reads True, exactly as before this check existed.
    """
```

Note what it does **not** do: no `Args: label_name (str): The label name.` A parameter whose name and
type already say everything gets no line. `Args:` / `Returns:` / `Raises:` earn their place when the
value is non-obvious — a unit, a sentinel, a failure mode, an ownership rule:

```python
    Returns:
        Age in days, or None when the "Sent ... ago" stamp could not be read. None is
        load-bearing: the caller must treat an unreadable stamp as NOT stale.
```

Three rules that reviewers will hold you to:

1. **Never invent behaviour to satisfy the linter.** If you cannot tell what a function guarantees,
   read its callers and its tests. A confident wrong sentence is worse than no sentence — it will be
   believed.
2. **Don't restate the signature.** `Returns: The user id.` under `-> int` is noise; delete it.
3. **Preserve prose when reformatting.** `D205` is fixed by splitting the first sentence onto its own
   line and adding a blank line — never by rewriting the paragraph underneath it.

## The ratchet

The tree does not meet this standard yet — `.ruff-baseline` holds what remains. The gate fails a PR
that **raises** that number, never one that merely inherits it.

That is not a softening; it is what makes the gate safe to arm at all. A zero-tolerance gate turned
on today would fail every PR on debt it did not create, and because the router labels a failing PR
`agent:docfix` — which `tick.sh` services in a priority lane **ahead of all roadmap work** — it would
point every open PR at the same ~3,400-item backlog, burn three Claude attempts each, and stall the
pipeline behind a standard nobody had finished adopting. The ratchet lets the gate protect the tree
from day one while the sweep walks the baseline down.

**Lowering the baseline is part of the work.** A PR that removes violations updates `.ruff-baseline`
to the new count; the gate's job summary tells you the number. When it reaches 0 this becomes an
ordinary zero-tolerance gate, the ratchet step can be deleted, and it goes into branch protection as
a required check.

## When the gate fails

Nothing is stranded on a human. `docstring-lint-router.yml` labels the PR **`agent:docfix`**, the
pipeline picks it up in a priority lane (right behind Dependabot's `agent:depfix`), fixes it on the
branch and clears the label. Three failed attempts on one branch escalates to the owner instead of
looping.

Because the gate is a ratchet, that label means "**this PR added violations**" — a small, diff-shaped
job — not "go fix the whole repo".

Locally:

```sh
poetry run ruff check src/ tests/ --statistics   # what and how much
poetry run ruff check src/ tests/ --fix          # the mechanical majority
```

**Two fix hazards, both measured rather than assumed.**

`--fix` will remove `ai_helper`'s deliberate `content_alignment` re-export aliases as unused imports
(`F401`), which turns **4 tests red** — the module aliases them to keep its long-standing internal
API stable, and `test_content_alignment.py` asserts the identity. If you are fixing broadly, run
`--fix --select D,I,E,T201,F541` and handle `F401` by hand.

**Never `--unsafe-fixes`.** Measured on this tree it produces **18 failures**: it deletes the two
aliases carrying the `lgtm` suppression, and — the larger share — strips `print()` calls under `T201`
from the `selenium_load_test`, `margin`, `cost_alerts` and `maintenance` CLIs, where the printed
output IS the product.
