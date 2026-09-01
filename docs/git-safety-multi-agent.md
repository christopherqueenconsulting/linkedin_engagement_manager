# Git safety & multi-agent concurrency

Full mechanics behind the `CLAUDE.md` "Git Safety & Multi-Agent Concurrency Rules" row. Read this
when you need the exact commands, not just the invariant.

## Every agent gets its own worktree

Agents sharing a single checkout WILL clobber each other — three once did, one switching the
branch under the others inside a minute. `isolation: "worktree"` on the Agent call (and in every
`.claude/agents/*.md` frontmatter) is the fix, and `lib/run_lane.sh` enforces it in code, because
`cd ""` **succeeds** in bash: an empty worktree path silently runs the agent in the shared tree
instead of failing loudly. Never trust a worktree path that could be empty — check it before `cd`.

## Model pins and env traps

`.claude/agents/builder.md` pins the model for the builder agent. Never add `model:` to an agent
definition without checking that pin first — an agent that inherits the parent's Ollama-lane URL
gets a 400 from the wrong endpoint, and it happens invisibly at `rc=0`, so a normal CI run looks
green. To reproduce CI locally, use an empty `.env` and move `src/cqc_lem/ui/dist` aside first —
those two differences are what makes a local pass diverge from a CI failure.

## Fresh state before every edit

Run `git status` and re-read the target file immediately before generating any code edit — never
edit from memory. Another agent may have changed the file under you since you last read it, and a
memory-based edit silently reverts their work.

## Micro-branching and the stash race

Never edit a shared branch asynchronously. Branch per task
(`git checkout -b feature/claude-<task-name>`) and commit each sub-task atomically.

`refs/stash` is **repo-global** — shared across every worktree and every concurrent agent. A
worktree isolates your checkout, not the stash stack, so a bare `git stash` / `git stash pop` is a
race: another agent's push between your push and your pop lands at `stash@{0}`, and your pop takes
*their* work, not yours.

If working-tree changes clash with your targets:

1. **Prefer a temporary WIP commit.** `git commit -m "WIP: <tag>"`, then `git reset` or `amend`
   later. It's addressable by SHA, private to your branch, and nothing else can take it.
2. **Only if stashing is truly unavoidable**, use the safe form:
   - `git stash push -u -m "<unique-tag>"`
   - Immediately capture its SHA: `git stash list --format='%H %gs'`
   - Restore with `git stash apply <sha>` — **never `pop`, never by index**.
   - Drop the entry afterward by re-finding its current index by tag.

## One venv, many worktrees

All worktrees share ONE poetry venv, and its editable-install `.pth` is mutable — the last
`poetry install` run anywhere wins. `poetry run python -c "import cqc_lem…"` may therefore
silently read a **different** worktree's source. Use `PYTHONPATH=src` and print `__file__` to
confirm you're reading your own tree:

```
PYTHONPATH=src poetry run python -c "import cqc_lem.api.main as m; print(m.__file__)"
```

`pytest` is unaffected — its `pythonpath` config is rootdir-relative.

## Branch cleanup

Merged branches auto-delete; orphans are swept weekly. Full posture: `docs/branch-cleanup.md`.

## A label is not an access control

This repo is **public** and the pipeline runs with the owner's credentials, so `agent:ready` /
`release:now` are verified by **provenance, not presence** — the author must have standing AND an
allowlisted actor must have applied the label. An unreadable answer REFUSES rather than assumes.
The pipeline's credential has **no `workflows` permission** — the hard control, since the agent
and the owner otherwise share one identity. Full posture: `docs/contribution-security.md`.
