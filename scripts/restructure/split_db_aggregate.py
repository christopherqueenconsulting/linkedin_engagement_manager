"""Move one aggregate's functions out of `utilities/db.py` into `platform/db/repositories/`.

Phase 2 slice 2a of the layered restructure (issue #1154). Temporary tooling: delete it once the
last aggregate has moved.

The point of doing this with a script rather than an edit pass is that the moved function bodies
come across byte-for-byte. A reviewer can then check the claim mechanically -- `--verify` re-parses
both files and asserts every moved body is identical to the one on the merge base -- instead of
reading 500 lines of diff hoping an LLM did not quietly reword a SQL string.

Usage:
    python scripts/restructure/split_db_aggregate.py <aggregate> [--dry-run]
    python scripts/restructure/split_db_aggregate.py <aggregate> --verify [--base origin/main]

A module that already exists is APPENDED to, not overwritten: after the ten first-pass slices the
remaining members are residue, and rewriting `avatar.py` from the leftovers would delete the 18
functions that landed in it earlier. `--verify` reads the aggregate's pre-move bodies from both
sides for the same reason.

Three things this script refuses to do, each because it silently corrupted an earlier attempt:

1. Move a function that calls a `db.py` function staying behind. The facade would have to import
   the repository and the repository the facade -- a circular import. Such functions are reported
   and left in place for a later slice.
2. Leave an aggregate-private module constant behind. A constant whose last reader moves becomes
   dead code that ruff and CodeQL then flag; one that moves while a stayer still reads it is a
   NameError. The script computes which side each constant belongs on.
3. Guess at names used only inside string annotations. `-> "datetime | None"` is invisible to
   `ast.Name`, so the naive free-variable pass dropped the import and produced an F821 that no test
   caught, because a string annotation is never evaluated.
"""

import argparse
import ast
import builtins
import collections
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
DB = REPO / "src/cqc_lem/utilities/db.py"
REPOSITORIES = REPO / "src/cqc_lem/platform/db/repositories"
MIGRATIONS = REPO / "compose/local/database/migrations"

# Which aggregate owns each table. Grounded against the CREATE TABLE / ALTER TABLE names in the
# migrations, so a typo here becomes an unassigned function rather than a wrong module.
TABLE_OWNER = {
    "users": "users", "profiles": "users", "engagement_preferences": "users", "cookies": "users",
    "onboarding_state": "users", "onboarding_nudges": "users", "user_email_history": "users",
    "posts": "posts", "post_stats": "posts", "post_engagers": "posts", "post_variants": "posts",
    "content_quality_scores": "posts", "follower_stats": "posts",
    "sessions": "auth", "user_auth_factors": "auth", "auth_challenges": "auth",
    "email_pin_auth": "auth", "user_recovery_codes": "auth", "auth_audit_log": "auth",
    "app_credentials": "auth",
    "scheduled_dms": "outreach", "connection_requests": "outreach",
    "outreach_funnel_targets": "outreach", "engagement_targets": "outreach", "leads": "outreach",
    "lead_signals": "outreach", "dm_followups": "outreach", "dm_templates": "outreach",
    "appreciation_touches": "outreach", "lead_magnet_settings": "outreach",
    "lead_magnet_sent": "outreach", "catchup_touches": "outreach",
    "catchup_send_attempts": "outreach",
    "logs": "engagement", "commented_posts": "engagement", "comment_followups": "engagement",
    "comment_outcomes": "engagement",
    "newsletter_editions": "newsletter", "newsletter_settings": "newsletter",
    "newsletter_subscriber_stats": "newsletter", "shipped_notices": "newsletter",
    "shipped_notice_recipients": "newsletter",
    "feedback": "feedback", "faq_entries": "feedback", "faq_entry_versions": "feedback",
    "survey_prompts": "feedback", "story_bank": "feedback",
    "avatar_trainings": "avatar", "avatar_credit_ledger": "avatar",
    "video_credit_ledger": "avatar",
    "user_groups": "groups", "group_post_drafts": "groups",
    "cost_ledger": "billing", "affiliate_enrollments": "billing", "affiliate_rewards": "billing",
    "affiliate_referrals": "billing", "early_adopter_grants": "billing",
    "early_adopter_slots": "billing",
}

_TABLE_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+`?([a-z_][a-z0-9_]*)`?", re.I)


def real_tables() -> set[str]:
    """Table names the migrations actually create, used to reject prose that parses like SQL."""
    sql = " ".join(p.read_text() for p in MIGRATIONS.rglob("*.sql"))
    pattern = r"(?:CREATE TABLE(?:\s+IF NOT EXISTS)?|ALTER TABLE)\s+`?([a-z_][a-z0-9_]*)`?"
    return {t.lower() for t in re.findall(pattern, sql, re.I)}


def tables_touched(node: ast.AST, known: set[str]) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found |= {m.lower() for m in _TABLE_RE.findall(sub.value)}
    return found & known


def free_names(text: str, already_bound: set[str]) -> set[str]:
    """Names `text` reads but does not define, including those hidden in string annotations."""
    tree = ast.parse(text)

    # TWO scopes, kept apart. Python has exactly this rule and both halves bit once when it was
    # flattened into a single set:
    #
    #   * a function-LOCAL `from ... import log_error` binds inside that function only. Treated as a
    #     module binding, it made the import block skip `log_error` entirely while other moved
    #     functions still relied on db.py's module-level one -- NameError, on an error path only.
    #   * a function PARAMETER named `timezone` binds inside that function only. Treated as a module
    #     binding, it suppressed `from datetime import timezone` while six other functions called
    #     `datetime.now(timezone.utc)` -- NameError again, and this one on the HAPPY path.
    #
    # So: `bound` is module-level names, and each function's own locals are subtracted only from
    # what THAT function reads.
    bound = set(already_bound)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)

    def _locals_of(scope: ast.AST) -> set[str]:
        """Everything `scope` binds for itself: params, assignments, excepts, local imports."""
        local: set[str] = set()
        for sub in ast.walk(scope):
            # Lambda binds parameters exactly like a def but has no .name, so a def-only check
            # leaves `lambda t: t["x"]` looking like a read of a global called `t`.
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                args = sub.args
                local |= {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
                if args.vararg:
                    local.add(args.vararg.arg)
                if args.kwarg:
                    local.add(args.kwarg.arg)
                if not isinstance(sub, ast.Lambda) and sub is not scope:
                    local.add(sub.name)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                local.add(sub.id)
            if isinstance(sub, ast.ExceptHandler) and sub.name:
                local.add(sub.name)
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                local |= {(a.asname or a.name).split(".")[0] for a in sub.names}
        return local

    def _comprehension_targets(scope: ast.AST) -> set[str]:
        """Names a comprehension binds. These have their own scope at ANY nesting level.

        `ONBOARDING_STEPS = tuple(step for step in OnboardingStep)` sits at module level, so it is
        not a function whose locals get subtracted, and `step` is not an assignment target -- it
        read as a free global the tool then could not resolve.
        """
        names: set[str] = set()
        for sub in ast.walk(scope):
            if isinstance(sub, ast.comprehension):
                names |= {n.id for n in ast.walk(sub.target)
                          if isinstance(n, ast.Name)}
        return names

    used = set()
    for node in tree.body:
        scope_bound = _locals_of(node) if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else set()
        scope_bound |= _comprehension_targets(node)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in scope_bound:
                    used.add(sub.id)
    for node in ast.walk(tree):
        annotation = getattr(node, "returns", None) or getattr(node, "annotation", None)
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                parsed = ast.parse(annotation.value, mode="eval")
            except SyntaxError:
                continue
            used |= {n.id for n in ast.walk(parsed) if isinstance(n, ast.Name)}
    # `dir(__builtins__)` is not this: __builtins__ is the module when a script runs
    # directly but a DICT when imported, so dir() would hand back dict methods and every
    # real builtin would read as a free name needing an import.
    return used - bound - set(dir(builtins)) - {"__name__"}


def adopt_private_helpers(votes: dict[str, str], ambiguous: dict[str, list],
                          callers: dict[str, set]) -> dict[str, str]:
    """Assign a table-less private helper to the aggregate every one of its readers belongs to.

    Assignment is by the tables a function's SQL names, so a helper that runs no SQL of its own --
    `_avatar_row_to_dict`, `_group_post_draft_row` -- belongs to no aggregate and therefore stays in
    db.py. Movability is transitive, so it then strands every function that calls it: after the ten
    first-pass slices, 8 helpers were holding 21 members behind them, and re-running any aggregate
    reported 0 movable rather than saying why.

    Two deliberate limits, because this rule infers ownership rather than reading it off the SQL:

    * **Private names only.** Adoption is transitive too, and applied to public names it walks the
      facade's own API into a repository on the strength of one caller: `is_premium_subscriber`
      (billing) gets dragged into `users` because `max_catchup_touches_allowed` reads it and that
      was itself dragged there by `update_engagement_preferences`. A leading underscore is the
      module's own statement that the name is internal.
    * **Unanimous readers.** A helper two aggregates read is shared vocabulary and belongs in
      `platform/db/shared.py`, which is a human's call -- the same rule the constants already follow.

    A helper nothing in db.py calls is left alone: its readers are outside this module, so it has no
    aggregate to infer and no caller it can strand.
    """
    adopted = dict(votes)
    while True:
        gained = False
        for name, readers in callers.items():
            if name in adopted or name in ambiguous or not name.startswith("_") or not readers:
                continue
            owners = {adopted.get(r) for r in readers}
            if len(owners) == 1 and None not in owners:
                adopted[name] = owners.pop()
                gained = True
        if not gained:
            return adopted


def open_alerts_in(spans: list[tuple[int, int]]) -> list[str]:
    """Open CodeQL alerts sitting inside the line ranges about to move.

    CodeQL tracks an alert by (path, content). A whole-file `git mv` carries the identity across,
    but this is a partial extraction into a NEW file -- so an alert that rides along is reported at
    a path that never had it, which the PR gate counts as newly introduced and refuses. Cheaper to
    learn that here than 3 minutes into CI.
    """
    query = (
        '[.[] | select(.most_recent_instance.location.path == "src/cqc_lem/utilities/db.py")'
        ' | "\\(.rule.id)@\\(.most_recent_instance.location.start_line)"]'
    )
    probe = subprocess.run(
        ["gh", "api", "repos/:owner/:repo/code-scanning/alerts?state=open&per_page=100",
         "--jq", query],
        capture_output=True, text=True, cwd=REPO)
    if probe.returncode != 0:
        return []  # no gh, no token, no network -- advisory check, never a hard stop
    riding = []
    for entry in json.loads(probe.stdout or "[]"):
        rule, _, line = entry.rpartition("@")
        if any(lo <= int(line) <= hi for lo, hi in spans):
            riding.append(f"{rule} at db.py:{line}")
    return riding


def function_bodies(src: str) -> dict[str, str]:
    lines = src.splitlines()
    return {
        n.name: "\n".join(lines[n.lineno - 1:n.end_lineno])
        for n in ast.parse(src).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def module_bindings(src: str) -> dict[str, str]:
    """Module-level constants and classes, by source text.

    Verifying only function bodies left the most dangerous thing in the split unchecked. The `users`
    aggregate carries `SECRET_FIELD_COOKIE_VALUE = "cookies.value"` and its three siblings, and those
    STRING VALUES are the AAD every encrypted column was sealed under. A byte that changes while
    moving does not fail a test -- it silently orphans every row already written, and
    `ENCRYPTION_REQUIRED=true` in production means those reads then return None rather than erroring.
    """
    lines = src.splitlines()
    out: dict[str, str] = {}
    for n in ast.parse(src).body:
        name = None
        if isinstance(n, ast.Assign):
            name = next((t.id for t in n.targets if isinstance(t, ast.Name)), None)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            name = n.target.id
        elif isinstance(n, ast.ClassDef):
            name = n.name
        if name:
            out[name] = "\n".join(lines[n.lineno - 1:n.end_lineno])
    return out


def append_to_module(existing: str, blocks: list[str], moved_src: str) -> str:
    """Add `moved_src` to a repository module that already exists, with only the imports it lacks.

    Emitting the whole computed import header again would redeclare names the module already binds;
    emitting none would leave the appended functions reading free names. Only what is missing goes
    in, placed after the last import so `ruff check --select I --fix` can fold it into the block.
    """
    tree = ast.parse(existing)
    bound = set()
    last_import = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
            last_import = max(last_import, node.end_lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)

    missing = []
    for block in blocks:
        # A block is `import x`, `from m import a` or a parenthesised `from m import (\n a,\n b,\n)`.
        # Keep only the names this module does not already bind, so the merge is by NAME rather
        # than by whole statement -- `from typing import Optional` is already here in every module,
        # while `Union` alongside it may not be.
        names = re.findall(r"^\s*([\w.]+(?: as \w+)?),?\s*$", block, re.M)
        head = block.split(" import ")[0]
        if block.startswith("import "):
            if block[len("import "):].split(" as ")[0].split(".")[0] not in bound:
                missing.append(block)
            continue
        wanted = [n for n in names
                  if (n.split(" as ")[-1] if " as " in n else n).split(".")[0] not in bound]
        if not wanted:
            continue
        if len(wanted) == 1 and " import (" not in block:
            missing.append(f"{head} import {wanted[0]}")
        else:
            missing.append(f"{head} import (\n" + "".join(f"    {n},\n" for n in wanted) + ")")

    lines = existing.splitlines(keepends=True)
    if missing:
        lines[last_import:last_import] = [b + "\n" for b in missing]
    return "".join(lines).rstrip("\n") + "\n\n\n" + moved_src.strip() + "\n"


def merge_facade_import(src: str, aggregate: str, exported: list[str]) -> str:
    """Re-export the moved names from db.py, extending the aggregate's block if it has one.

    A second `from cqc_lem.platform.db.repositories.avatar import (...)` statement is legal Python
    and ruff would fold it, but the file a human reviews is the one written here.
    """
    module = f"cqc_lem.platform.db.repositories.{aggregate}"
    lines = src.splitlines(keepends=True)
    for node in ast.parse(src).body:
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names = sorted({a.name for a in node.names} | set(exported))
            block = (f"from {module} import (\n"
                     + "".join(f"    {n},\n" for n in names) + ")\n")
            lines[node.lineno - 1:node.end_lineno] = [block]
            return "".join(lines)
    block = (f"from {module} import (\n"
             + "".join(f"    {n},\n" for n in exported) + ")\n")
    anchor = "from cqc_lem.utilities.crypto import ("
    return src.replace(anchor, block + anchor, 1)


def merge_dunder_all(src: str, exported: list[str]) -> str:
    """Add the moved names to `__all__` as one sorted run at the end.

    `__all__` is what keeps ruff (F401) and CodeQL (py/unused-import) from deleting the re-exports,
    so a name that misses it is not a style problem -- it is a re-export with a countdown on it.

    The list is already ten sorted runs, one per slice, not one sorted list. Two alternatives were
    worse: re-sorting it globally is a ~900-line reordering that buries the ~80 lines of SQL a slice
    actually moves (and shifts every db.py line a CodeQL alert is keyed to), while inserting each
    name before the first entry that sorts after it scatters this slice's names through the FIRST
    run rather than keeping them together. A run per slice is what the file already is.
    """
    lines = src.splitlines(keepends=True)
    for node in ast.parse(src).body:
        is_all = isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        if not is_all:
            continue
        current = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
        current += sorted(n for n in set(exported) if n not in current)
        body = "".join(f'    "{n}",\n' for n in current)
        lines[node.lineno - 1:node.end_lineno] = [f"__all__ = [\n{body}]\n"]
        return "".join(lines)
    raise SystemExit("db.py has no __all__ to extend -- refusing to write a re-export ruff will cut")


def verify(aggregate: str, base: str) -> int:
    """Assert every moved body is byte-identical to the one on `base`, and that none went missing."""
    # An aggregate whose module already exists on `base` was moved by an EARLIER commit, so its
    # functions are legitimately absent from base's db.py. Reading `before` from db.py alone would
    # report every one of them as INVENTED -- 30 scary names describing nothing. They are not
    # unverifiable, though: they are on `base` at the OTHER path, so the pre-move state of this
    # aggregate is the union of the two files. That keeps a residue slice checked as strictly as a
    # first-pass one, which the earlier "nothing to check here" short-circuit did not.
    rel = f"src/cqc_lem/platform/db/repositories/{aggregate}.py"
    already = subprocess.run(["git", "cat-file", "-e", f"{base}:{rel}"],
                             capture_output=True, cwd=REPO).returncode == 0

    old = subprocess.run(
        ["git", "show", f"{base}:src/cqc_lem/utilities/db.py"],
        capture_output=True, text=True, cwd=REPO, check=True).stdout
    old_module = subprocess.run(
        ["git", "show", f"{base}:{rel}"],
        capture_output=True, text=True, cwd=REPO, check=True).stdout if already else ""
    before = function_bodies(old)
    before.update(function_bodies(old_module))
    moved = function_bodies((REPOSITORIES / f"{aggregate}.py").read_text())
    # "Lost" means gone from the CODEBASE, so every repository counts as a destination, not just
    # this one. Scoping it to db.py + the aggregate under test reported each function moved by a
    # SIBLING aggregate in the same branch as lost -- a false alarm that would train a reader to
    # ignore the one check that would catch a real deletion.
    remaining = function_bodies(DB.read_text())
    for sibling in sorted(REPOSITORIES.glob("*.py")):
        if sibling.name != "__init__.py":
            remaining.update(function_bodies(sibling.read_text()))

    altered = sorted(k for k in moved if k in before and before[k] != moved[k])
    invented = sorted(k for k in moved if k not in before)
    lost = sorted(k for k in before if k not in remaining and k not in moved)

    # Constants and classes travel too, and one of them is the encryption AAD.
    binds_before = module_bindings(old)
    binds_before.update(module_bindings(old_module))
    binds_moved = module_bindings((REPOSITORIES / f"{aggregate}.py").read_text())
    const_altered = sorted(
        k for k in binds_moved if k in binds_before and binds_before[k] != binds_moved[k])

    for label, names in (("ALTERED", altered), ("INVENTED", invented), ("LOST", lost),
                         ("ALTERED CONSTANT/CLASS", const_altered)):
        if names:
            print(f"  {label}: {names}")
    if altered or invented or lost or const_altered:
        return 1
    print(f"  {len(moved)} function(s) and {len(binds_moved)} constant(s)/class(es) moved, "
          f"all byte-identical to {base}; none lost")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aggregate", choices=sorted(set(TABLE_OWNER.values())))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()

    if args.verify:
        return verify(args.aggregate, args.base)

    src = DB.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    nodes = {
        n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    known = real_tables()

    # SORTED, and ties are refused rather than broken. tables_touched returns a set, and Python
    # randomises string hashing per process -- so with an unsorted iteration a function touching
    # two tables owned by different aggregates got a tied Counter that most_common() resolved by
    # insertion order. Three identical runs assigned `posts` 79, 79 and 77 members. A function that
    # lands in a different module depending on the run is not a refactor, it is a coin flip, and
    # the `users` slice carries the SECRET_FIELD_* AAD constants.
    votes, ambiguous = {}, {}
    for name, node in nodes.items():
        owners = collections.Counter(
            TABLE_OWNER[t] for t in sorted(tables_touched(node, known)) if t in TABLE_OWNER)
        if not owners:
            continue
        top = max(owners.values())
        winners = sorted(a for a, c in owners.items() if c == top)
        if len(winners) > 1:
            ambiguous[name] = winners        # genuinely spans aggregates -- a human decides
            continue
        votes[name] = winners[0]
    touching = sorted(n for n, aggs in ambiguous.items() if args.aggregate in aggs)

    calls = collections.defaultdict(set)
    callers = collections.defaultdict(set)
    for name, node in nodes.items():
        for sub in ast.walk(node):
            is_call = isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            if is_call and sub.func.id in nodes and sub.func.id != name:
                calls[name].add(sub.func.id)
                callers[sub.func.id].add(name)

    votes = adopt_private_helpers(votes, ambiguous, callers)
    members = {n for n, agg in votes.items() if agg == args.aggregate}
    # Fixpoint, not a single pass. Movability is TRANSITIVE: if A calls B and B has to stay
    # behind, A cannot leave either, or it would reference a name that is no longer in scope. A
    # one-pass `calls[f] <= members` check misses that, marks A movable, and the breakage only
    # surfaces later as an unresolved name.
    movable = set(members)
    while True:
        stranded = {f for f in movable if not calls[f] <= movable}
        if not stranded:
            break
        movable -= stranded
    deferred = sorted(members - movable)

    constants: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            constants[node.target.id] = node

    imported: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported[alias.asname or alias.name] = (node, alias)

    def span(node: ast.AST) -> tuple[int, int]:
        decorators = getattr(node, "decorator_list", [])
        start = min([node.lineno] + [d.lineno for d in decorators]) - 1
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        return start, node.end_lineno

    fn_text = "".join("".join(lines[a:b]) for a, b in sorted(span(nodes[f]) for f in movable))
    stayers = set(nodes) - movable
    stay_text = "".join("".join(lines[span(nodes[f])[0]:span(nodes[f])[1]]) for f in stayers)
    stay_free = free_names(stay_text, stayers)
    travelling = sorted(
        c for c in free_names(fn_text, movable) & set(constants) if c not in stay_free)

    all_spans = sorted(
        [span(nodes[f]) for f in movable] + [span(constants[c]) for c in travelling])
    moved_src = "".join("".join(lines[a:b]) for a, b in all_spans)
    needed = sorted(free_names(moved_src, movable | set(travelling)))
    unresolved = [n for n in needed if n not in imported]

    print(f"{args.aggregate}: {len(members)} member(s), {len(movable)} movable")
    if touching:
        print(f"  AMBIGUOUS, left in db.py for a human to place ({len(touching)}):")
        for name in touching:
            print(f"    {name} -> {'/'.join(ambiguous[name])}")
    if deferred:
        print(f"  deferred (call a function that stays behind): {deferred}")
    print(f"  constants travelling with them: {travelling}")
    riding = open_alerts_in([(nodes[f].lineno, nodes[f].end_lineno) for f in movable])
    if riding:
        print("  OPEN CodeQL ALERTS inside the moved code -- the PR gate will read these as newly")
        print("  introduced once they land at a new path. Clear them on main first:")
        for alert in riding:
            print(f"    {alert}")
        return 1
    if unresolved:
        # Split the two very different reasons a name will not resolve. A name db.py DEFINES but
        # cannot send along -- because something staying behind still reads it -- is shared
        # vocabulary, and the fix is to lift it into a module both sides can import, never to
        # duplicate it. Anything else is a genuine gap in this script.
        shared = [n for n in unresolved if n in constants or n in {c.name for c in tree.body
                  if isinstance(c, ast.ClassDef)}]
        rest = [n for n in unresolved if n not in shared]
        print("  REFUSING to write.")
        if shared:
            print(f"    shared with code that stays behind, lift these to platform/db/ first: {shared}")
        if rest:
            print(f"    unaccounted for -- this script has a gap: {rest}")
        return 1
    if args.dry_run:
        return 0

    grouped: dict[tuple, list[str]] = {}
    for name in needed:
        entry = imported[name]
        if isinstance(entry, tuple):
            stmt, alias = entry
            # Carry the `as` through. db.py binds the connection module as `from cqc_lem.platform.db
            # import connection as _connection`, and emitting just the bound name produced
            # `from cqc_lem.platform.db import _connection` -- an ImportError, since the module is
            # called `connection`. Only functions using `_connection.` directly hit this, so the
            # first aggregate moved (all db_cursor) never exercised it.
            spec = alias.name if not alias.asname else f"{alias.name} as {alias.asname}"
            grouped.setdefault(("from", stmt.module), []).append(spec)
        else:
            grouped.setdefault(("import", entry.names[0].asname or entry.names[0].name), [])
    blocks = []
    for key in sorted(grouped, key=lambda k: (k[0] != "import", k[1])):
        if key[0] == "import":
            blocks.append(f"import {key[1]}")
            continue
        names = sorted(set(grouped[key]))
        if len(names) == 1:
            blocks.append(f"from {key[1]} import {names[0]}")
        else:
            body = "".join(f"    {n},\n" for n in names)
            blocks.append(f"from {key[1]} import (\n{body})")

    header = (
        f'"""Every SQL statement LEM runs against the {args.aggregate} tables.\n\n'
        f"Split out of `cqc_lem.utilities.db` (issue #1154). The fail-soft reader contract and the\n"
        f"secret-sealing rules described there apply here unchanged; `cqc_lem.utilities.db`\n"
        f're-exports every name below, so existing importers and patch targets keep resolving.\n"""\n\n'
        + "\n".join(blocks) + "\n\n\n")

    REPOSITORIES.mkdir(parents=True, exist_ok=True)
    init = REPOSITORIES / "__init__.py"
    if not init.exists():
        init.write_text('"""Per-aggregate SQL modules split out of `cqc_lem.utilities.db`."""\n')
    target = REPOSITORIES / f"{args.aggregate}.py"
    if target.exists():
        # A residue slice. Overwriting is what a first-pass slice does and it would silently drop
        # everything the earlier pass put here -- 18 avatar functions for the sake of 4.
        target.write_text(append_to_module(target.read_text(), blocks, moved_src))
    else:
        target.write_text(header + moved_src.strip() + "\n")

    kept = list(lines)
    for start, end in sorted(all_spans, reverse=True):
        del kept[start:end]
    new_db = "".join(kept)
    exported = sorted(movable | set(travelling))
    new_db = merge_facade_import(new_db, args.aggregate, exported)
    new_db = merge_dunder_all(new_db, exported)
    DB.write_text(new_db)

    print(f"  wrote {REPOSITORIES / (args.aggregate + '.py')}")
    print(f"  db.py {len(lines)} -> {len(kept)} lines")
    print("  now run: ruff check --select I,F401 --fix, then --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
