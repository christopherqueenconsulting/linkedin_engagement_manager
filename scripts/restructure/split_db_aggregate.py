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
    bound = set(already_bound)
    for node in ast.walk(tree):
        # Lambda belongs here too: it binds parameters exactly like a def, but has no .name, so
        # a def-only check leaves `lambda t: t["x"]` looking like a read of a global called `t`.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                bound.add(node.name)
            args = node.args
            bound |= {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        if isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
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


def verify(aggregate: str, base: str) -> int:
    """Assert every moved body is byte-identical to the one on `base`, and that none went missing."""
    # An aggregate whose module already exists on `base` was moved by an EARLIER commit, so its
    # functions are legitimately absent from base's db.py and every one would report as INVENTED.
    # That is a meaningless run, not a finding -- say so rather than printing 30 scary names.
    rel = f"src/cqc_lem/platform/db/repositories/{aggregate}.py"
    already = subprocess.run(["git", "cat-file", "-e", f"{base}:{rel}"],
                             capture_output=True, cwd=REPO).returncode == 0
    if already:
        print(f"  {aggregate} already exists on {base} -- verify it against the commit before its"
              f" move, e.g. --base {base}~1. Nothing to check here.")
        return 0

    old = subprocess.run(
        ["git", "show", f"{base}:src/cqc_lem/utilities/db.py"],
        capture_output=True, text=True, cwd=REPO, check=True).stdout
    before = function_bodies(old)
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
    members = {n for n, agg in votes.items() if agg == args.aggregate}
    touching = sorted(n for n, aggs in ambiguous.items() if args.aggregate in aggs)

    calls = collections.defaultdict(set)
    for name, node in nodes.items():
        for sub in ast.walk(node):
            is_call = isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            if is_call and sub.func.id in nodes and sub.func.id != name:
                calls[name].add(sub.func.id)
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
    (REPOSITORIES / f"{args.aggregate}.py").write_text(header + moved_src.strip() + "\n")

    kept = list(lines)
    for start, end in sorted(all_spans, reverse=True):
        del kept[start:end]
    new_db = "".join(kept)
    exported = sorted(movable | set(travelling))
    import_block = (
        f"from cqc_lem.platform.db.repositories.{args.aggregate} import (\n"
        + "".join(f"    {n},\n" for n in exported) + ")\n")
    anchor = "from cqc_lem.utilities.crypto import ("
    new_db = new_db.replace(anchor, import_block + anchor, 1)
    new_db = new_db.replace(
        "__all__ = [\n", "__all__ = [\n" + "".join(f'    "{n}",\n' for n in exported), 1)
    DB.write_text(new_db)

    print(f"  wrote {REPOSITORIES / (args.aggregate + '.py')}")
    print(f"  db.py {len(lines)} -> {len(kept)} lines")
    print("  now run: ruff check --select I,F401 --fix, then --verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
