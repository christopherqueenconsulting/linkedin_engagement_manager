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
import collections
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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
    return used - bound - set(dir(__builtins__)) - {"__name__"}


def function_bodies(src: str) -> dict[str, str]:
    lines = src.splitlines()
    return {
        n.name: "\n".join(lines[n.lineno - 1:n.end_lineno])
        for n in ast.parse(src).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def verify(aggregate: str, base: str) -> int:
    """Assert every moved body is byte-identical to the one on `base`, and that none went missing."""
    old = subprocess.run(
        ["git", "show", f"{base}:src/cqc_lem/utilities/db.py"],
        capture_output=True, text=True, cwd=REPO, check=True).stdout
    before = function_bodies(old)
    moved = function_bodies((REPOSITORIES / f"{aggregate}.py").read_text())
    remaining = function_bodies(DB.read_text())

    altered = sorted(k for k in moved if k in before and before[k] != moved[k])
    invented = sorted(k for k in moved if k not in before)
    lost = sorted(k for k in before if k not in remaining and k not in moved)
    for label, names in (("ALTERED", altered), ("INVENTED", invented), ("LOST", lost)):
        if names:
            print(f"  {label}: {names}")
    if altered or invented or lost:
        return 1
    print(f"  {len(moved)} function(s) moved, all byte-identical to {base}; none lost")
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

    votes = {}
    for name, node in nodes.items():
        owners = collections.Counter(
            TABLE_OWNER[t] for t in tables_touched(node, known) if t in TABLE_OWNER)
        if owners:
            votes[name] = owners.most_common(1)[0][0]
    members = {n for n, agg in votes.items() if agg == args.aggregate}

    calls = collections.defaultdict(set)
    for name, node in nodes.items():
        for sub in ast.walk(node):
            is_call = isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
            if is_call and sub.func.id in nodes and sub.func.id != name:
                calls[name].add(sub.func.id)
    movable = {f for f in members if calls[f] <= members}
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
    if deferred:
        print(f"  deferred (call a function that stays behind): {deferred}")
    print(f"  constants travelling with them: {travelling}")
    if unresolved:
        print(f"  UNRESOLVED names, refusing to write: {unresolved}")
        return 1
    if args.dry_run:
        return 0

    grouped: dict[tuple, list[str]] = {}
    for name in needed:
        entry = imported[name]
        if isinstance(entry, tuple):
            stmt, alias = entry
            grouped.setdefault(("from", stmt.module), []).append(alias.asname or alias.name)
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
