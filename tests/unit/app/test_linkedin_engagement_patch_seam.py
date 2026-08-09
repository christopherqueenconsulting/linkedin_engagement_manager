"""Guard the `utilities.linkedin` -> `app.engagement` patch seam.

No test may patch a `cqc_lem.utilities.linkedin.*` symbol on its defining module while
exercising an `app.engagement.*` module that imported that symbol directly — unless it ALSO
patches the importer's own binding, which is what makes the mock reachable.

Issue #1209. `app/run_automation.py` was deleted in #1206/#1207, removing the stale
`cqc_lem.app.run_automation` patch seam. The same trap now lives one layer down:
`app.engagement.feed` imports `_redis_client` from `utilities.linkedin.rate_limit` and reads
it from its OWN globals, so a test patching `cqc_lem.utilities.linkedin.rate_limit._redis_client`
rebinds a name nothing in the engagement module consults. The mock is never called, the real Redis
client runs, and the assertion happens to hold for an unrelated reason.

This guard derives the hazard set from the engagement modules' AST rather than listing it, so
adding or renaming a moved symbol does not silently retire the check. It mirrors
`tests/unit/platform/db/test_connection_seam.py` and `tests/unit/api/test_router_patch_seam.py`.

`test_guard_names_the_exact_offender` runs the WHOLE scan over a synthetic test tree — the
`with`-block form, the decorator form, and the correctly-patched form — because every guard in
this program has been vacuous on its first attempt.

What it does NOT see, so that nobody reads a pass as proof of absence: a call reached through
dynamic dispatch (`getattr(importlib.import_module(POST), name)`), a call made outside the patch's
own function body, and modules outside `app/engagement/` — `app/run_scheduler.py` imports from
`utilities/linkedin/*` the same way and is not covered here.
"""

import ast
import pathlib
import re
import textwrap

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[3]
_ENGAGEMENT_DIR = _REPO / "src" / "cqc_lem" / "app" / "engagement"
_LINKEDIN_PREFIX = "cqc_lem.utilities.linkedin."
_ENGAGEMENT_PREFIX = "cqc_lem.app.engagement."
_SELF = "test_linkedin_engagement_patch_seam.py"


def _module_globals(path: pathlib.Path) -> dict[str, tuple[str, str]]:
    """Map local name -> (source module, imported name) for direct `utilities.linkedin` imports.

    `from cqc_lem.utilities.linkedin.cards import _card_for_textbox as card` binds `card` locally;
    a patch on `cards._card_for_textbox` would miss `M.card`. Module-as imports (`import X as _X`)
    bind the MODULE, so attributes reached through them are shared objects and not this hazard.
    Function-local imports are likewise safe: they re-read the defining module at CALL time, so a
    patch there does reach them — only module-level `ImportFrom` is a second binding.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module and node.module.startswith(_LINKEDIN_PREFIX)):
            continue
        source = node.module
        for alias in node.names:
            local = alias.asname or alias.name
            # Submodule import: `from cqc_lem.utilities.linkedin import cards as _cards`
            # binds the module object, not a symbol. Patches on _cards.something are shared.
            if alias.name == source.rpartition(".")[2] and alias.asname:
                continue
            out[local] = (source, alias.name)
    return out


def _reader_functions(path: pathlib.Path, local_names: set[str]) -> dict[str, set[str]]:
    """Map function name -> the local imported names that calling it can reach.

    Reads are propagated along the module's OWN call graph: a task that delegates to a private
    helper still runs the helper's stale binding, so patching the defining module misses it just
    the same. Without the closure the guard would only see symbols a task reads in its own body,
    which is the minority of them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    names = {n.name for n in functions}
    readers: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for node in functions:
        reads = set()
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id in local_names:
                reads.add(sub.id)
            # Attribute load through a module alias (`_zw.report_zero_walk`) is NOT a direct read
            # of the imported symbol; it reaches the same module object and sees patches there.
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in names:
                called.add(sub.func.id)
        readers[node.name] = reads
        calls[node.name] = called
    changed = True
    while changed:  # fixpoint, so recursion and mutual calls terminate
        changed = False
        for fn, callees in calls.items():
            for callee in callees:
                new = readers[callee] - readers[fn]
                if new:
                    readers[fn] |= new
                    changed = True
    return readers


def _hazard_set(directory: pathlib.Path | None = None) -> dict[str, dict[str, set[str]]]:
    """Map each moved symbol to the engagement functions that read its LOCAL binding.

    Shape: `symbol -> {engagement_module: {function_names that read the local binding}}`.
    The outer key is the defining-module symbol (`cqc_lem.utilities.linkedin.rate_limit._redis_client`)
    so test scanning can look it up directly. `directory` defaults to the real `app/engagement/`;
    the anti-vacuity test points it at a synthetic tree.
    """
    directory = directory or _ENGAGEMENT_DIR
    hazards: dict[str, dict[str, set[str]]] = {}
    for path in sorted(p for p in directory.glob("*.py") if p.name != "__init__.py"):
        local_map = _module_globals(path)
        readers = _reader_functions(path, set(local_map))
        for local, (source, imported) in local_map.items():
            symbol = f"{source}.{imported}"
            funcs = {fn for fn, reads in readers.items() if local in reads}
            if funcs:
                hazards.setdefault(symbol, {}).setdefault(path.stem, funcs)
    return hazards


def _string_constant_aliases(tree: ast.Module) -> dict[str, list[str]]:
    """Map module path -> braced alias names (`{_POST}`) assigned to it at a file's top level."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(node.value.value, []).append("{" + t.id + "}")
    return out


def _engagement_imports(nodes) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Split `app.engagement` imports into `local -> (stem, function)` and `local -> stem`.

    The second map is the module form (`from cqc_lem.app.engagement import posting`), whose
    attribute calls name the function; the first is the symbol form, whose bare call does.
    """
    direct: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module.startswith(_ENGAGEMENT_PREFIX):
            stem = node.module.rpartition(".")[2]
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == stem and alias.asname:
                    # `from cqc_lem.app.engagement import posting as p` — module alias
                    modules[local] = stem
                else:
                    direct[local] = (stem, alias.name)
        elif node.module == _ENGAGEMENT_PREFIX.rstrip("."):
            # `from cqc_lem.app.engagement import posting` (no `.posting` suffix)
            for alias in node.names:
                modules[alias.asname or alias.name] = alias.name
    return direct, modules


def _parse_test_files(root: pathlib.Path | None = None) -> list[
        tuple[pathlib.Path, str, list[tuple[int, int] | None], dict[str, list[str]],
              tuple[dict[str, tuple[str, str]], dict[str, str]]]]:
    """Pre-parse every test file under `root` once and build a line-to-container index.

    This is the hot path: the naive version re-parsed every file for each of ~70 hazard symbols,
    which cost ~120s. Doing it once and reusing the index brings the guard down to sub-second.

    The index maps a line number to the (start, end) span of the innermost function/class holding
    it. A container's span STARTS at its first decorator, not at `def` — `@patch("...")` sits above
    the `def` line, and a span that began at `def` made every decorator-form patch invisible to the
    scan (measured: the offender below was reported for the `with` form and missed for the
    decorator form).

    The engagement imports come back too, because a test file that imports the module at the TOP
    and calls it inside the test would otherwise read as calling nothing.
    """
    root = root or (_REPO / "tests" / "unit")
    out = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(text)
        spans = []
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            spans.append((start, n.end_lineno))
        spans.sort(key=lambda s: (s[0], -s[1]))
        line_to_span: list[tuple[int, int] | None] = [None] * (len(text.splitlines()) + 2)
        for start, end in spans:
            for i in range(start, end + 1):
                line_to_span[i] = (start, end)
        out.append((path, text, line_to_span, _string_constant_aliases(tree),
                    _engagement_imports(tree.body)))
    return out


def _all_patch_blocks_for_symbols(
        text: str,
        line_to_span: list[tuple[int, int] | None],
        aliases: dict[str, list[str]],
        symbols: list[str],
) -> list[tuple[int, str, set[str]]]:
    """Every patch block touching any of `symbols` in a single file.

    A combined regex matches every symbol literal plus module-alias spellings, so a file is scanned
    once regardless of how many hazard symbols exist. Returns the matched symbols alongside the
    block so the caller only reports symbols that were BOTH patched AND read by an engagement
    function in the same block.
    """
    by_module: dict[str, list[str]] = {}
    literal_pats: list[str] = []
    for sym in symbols:
        literal_pats.append(re.escape(sym))
        module, _, name = sym.rpartition(".")
        by_module.setdefault(module, []).append(name)
    alias_pats: list[str] = []
    for module, names in by_module.items():
        for alias in aliases.get(module, []):
            alias_pats.append(
                re.escape(alias) + r"\.(" + "|".join(re.escape(n) for n in names) + r")\b")
    parts = [r'["\'](' + "|".join(literal_pats) + r')["\']']
    if alias_pats:
        parts.append(r"(" + "|".join(alias_pats) + r")")
    pat = re.compile("|".join(parts))
    # Reverse map so an alias token like `{_CMP}` resolves back to its module path.
    alias_to_module: dict[str, str] = {}
    for module, alias_list in aliases.items():
        for alias in alias_list:
            alias_to_module[alias] = module
    all_lines = text.splitlines()
    seen: dict[tuple[int, int], tuple[int, str, set[str]]] = {}
    for m in pat.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        span = line_to_span[line_no]
        if span is None:
            continue
        start, end = span
        block = "\n".join(all_lines[start - 1:end])
        matched = _symbols_patched_by_match(m, symbols, alias_to_module)
        if span not in seen:
            seen[span] = (line_no, block, matched)
        else:
            seen[span][2].update(matched)
    return list(seen.values())


def _symbols_patched_by_match(
        m: re.Match,
        symbols: list[str],
        alias_to_module: dict[str, str],
) -> set[str]:
    """Return the hazard symbol(s) this regex match corresponds to.

    A match is either a literal patch string (`"cqc_lem.utilities.linkedin.X"`) or an alias spelling
    (`f"{_CMP}.X"`). For aliases we map `{_CMP}` back to its module path via `alias_to_module`.
    """
    matched = set()
    matched_text = m.group(0)
    if matched_text.startswith(("'", '"')) and matched_text.endswith(("'", '"')):
        unquoted = matched_text[1:-1]
        for sym in symbols:
            if sym == unquoted:
                matched.add(sym)
        return matched
    # Alias spelling: e.g. `{_CMP}._reply_composer_for_comment`
    if "." not in matched_text:
        return matched
    alias_token, short = matched_text.rsplit(".", 1)
    module = alias_to_module.get(alias_token)
    if module is None:
        return matched
    candidate = f"{module}.{short}"
    if candidate in symbols:
        matched.add(candidate)
    return matched


def _engagement_patch_targets(block: str, aliases: dict[str, list[str]]) -> set[tuple[str, str]]:
    """`(module stem, attribute)` pairs this block patches ON the engagement module itself.

    Patching both bindings is the CORRECT way to cover a symbol two modules read, so a block that
    does it is not an offender — without this the guard flags working tests and its only remedy is
    to contort them into shapes that hide from it.
    """
    out: set[tuple[str, str]] = set()
    for m in re.finditer(r'["\']cqc_lem\.app\.engagement\.(\w+)\.(\w+)["\']', block):
        out.add((m.group(1), m.group(2)))
    for module, braced in aliases.items():
        if not module.startswith(_ENGAGEMENT_PREFIX):
            continue
        stem = module.rpartition(".")[2]
        for token in braced:
            for m in re.finditer(re.escape(token) + r"\.(\w+)", block):
                out.add((stem, m.group(1)))
    return out


def _engagement_functions_in_block(
        block: str,
        hazards: dict[str, dict[str, set[str]]],
        base_imports: tuple[dict[str, tuple[str, str]], dict[str, str]] = ({}, {}),
) -> set[tuple[str, str]]:
    """`(hazard symbol, engagement module stem)` pairs whose reader is called inside `block`.

    `block` is extracted from an existing Python file, so it may start with arbitrary indentation.
    We de-indent it before parsing so AST walks work regardless of how deeply nested the patch was.

    A block may reach an engagement function three ways, and `base_imports` carries the file's
    top-level imports so the second and third also work when the import sits outside the block:

    1. `cqc_lem.app.engagement.<mod>.<fn>()`
    2. `<alias>.<fn>()` where `<alias>` is bound to an engagement module
    3. `from cqc_lem.app.engagement.<mod> import <fn>` followed by `<fn>()`

    We only flag when the called function is a KNOWN reader of the patched symbol in that module.
    """
    tree = ast.parse(textwrap.dedent(block))
    block_direct, block_modules = _engagement_imports(ast.walk(tree))
    direct_imports = {**base_imports[0], **block_direct}
    module_aliases = {**base_imports[1], **block_modules}

    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        mod_stem: str | None = None
        fn_name: str | None = None
        if isinstance(node.func, ast.Attribute):
            fn_name = node.func.attr
            if isinstance(node.func.value, ast.Name):
                mod_stem = module_aliases.get(node.func.value.id)
            elif isinstance(node.func.value, ast.Attribute):
                # e.g. cqc_lem.app.engagement.feed.automate_commenting()
                dotted = _dotted_name(node.func.value)
                if dotted and dotted.startswith(_ENGAGEMENT_PREFIX):
                    mod_stem = dotted.split(".")[3]
        elif isinstance(node.func, ast.Name):
            # Direct call like `use_redis()` after `from ... import use_redis`.
            if node.func.id in direct_imports:
                mod_stem, fn_name = direct_imports[node.func.id]
        if mod_stem is None or fn_name is None:
            continue
        for symbol, mods in hazards.items():
            if mod_stem in mods and fn_name in mods[mod_stem]:
                found.add((symbol, mod_stem))
    return found


def _dotted_name(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _scan_for_offenders(
        hazards: dict[str, dict[str, set[str]]],
        test_files: list,
        root: pathlib.Path,
) -> list[str]:
    """Report every `file:line` that patches a hazard symbol on its DEFINING module unsafely."""
    offenders = []
    symbols = sorted(hazards)
    for path, text, line_to_span, aliases, base_imports in test_files:
        if path.name == _SELF:
            continue
        for line, block, matched_symbols in _all_patch_blocks_for_symbols(
                text, line_to_span, aliases, symbols):
            covered = _engagement_patch_targets(block, aliases)
            for symbol, stem in _engagement_functions_in_block(block, hazards, base_imports):
                short = symbol.rpartition(".")[2]
                if symbol not in matched_symbols or (stem, short) in covered:
                    continue
                offenders.append(
                    f"{path.relative_to(root)}:{line} patches {symbol} while calling a reader in "
                    f"{_ENGAGEMENT_PREFIX}{stem}; also patch "
                    f"{_ENGAGEMENT_PREFIX}{stem}.{short} (that is the binding the code reads)")
    return sorted(offenders)


class TestLinkedInEngagementPatchSeam:
    def test_there_are_hazards_to_check(self):
        """Anti-vacuity: if nothing is imported directly, the check below passes having read nothing."""
        assert _hazard_set(), (
            "no direct `cqc_lem.utilities.linkedin.*` imports found in app/engagement/*.py — "
            "the seam check would be vacuous")

    def test_no_test_patches_a_direct_import_on_the_defining_module(self):
        """The core guard: a patch on the source module plus a reader in the importer is an error."""
        offenders = _scan_for_offenders(_hazard_set(), _parse_test_files(), _REPO)
        assert offenders == [], "\n  ".join([""] + offenders)

    def test_guard_names_the_exact_offender(self, tmp_path):
        """Anti-vacuity, end to end: run the WHOLE scan over a synthetic tree and read the report.

        Three shapes, because the guard was wrong on two of them before this test existed: the
        `with`-block form, the decorator form (whose patch line sits ABOVE `def`), and the
        correctly-patched form that must NOT be reported.
        """
        eng = tmp_path / "app" / "engagement"
        eng.mkdir(parents=True)
        (eng / "__init__.py").write_text("")
        (eng / "feed.py").write_text(
            "from cqc_lem.utilities.linkedin.rate_limit import _redis_client\n"
            "\n"
            "def use_redis():\n"
            "    return _redis_client()\n"
            "\n"
            "def no_redis():\n"
            "    return 1\n")

        hazards = _hazard_set(eng)
        assert hazards["cqc_lem.utilities.linkedin.rate_limit._redis_client"]["feed"] == {"use_redis"}

        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_synthetic.py").write_text(
            'from unittest.mock import patch\n'
            'from cqc_lem.app.engagement import feed\n'
            '\n'
            '_FEED = "cqc_lem.app.engagement.feed"\n'
            '\n'
            '\n'
            'def test_with_block_offender():\n'
            '    with patch("cqc_lem.utilities.linkedin.rate_limit._redis_client"):\n'
            '        feed.use_redis()\n'
            '\n'
            '\n'
            '@patch("cqc_lem.utilities.linkedin.rate_limit._redis_client")\n'
            'def test_decorator_offender(_rc):\n'
            '    feed.use_redis()\n'
            '\n'
            '\n'
            'def test_both_bindings_patched_is_fine():\n'
            '    with patch("cqc_lem.utilities.linkedin.rate_limit._redis_client"), \\\n'
            '         patch(f"{_FEED}._redis_client"):\n'
            '        feed.use_redis()\n'
            '\n'
            '\n'
            'def test_calls_a_non_reader():\n'
            '    with patch("cqc_lem.utilities.linkedin.rate_limit._redis_client"):\n'
            '        feed.no_redis()\n')

        offenders = _scan_for_offenders(hazards, _parse_test_files(tests), tmp_path)
        lines = {o.split(" patches ")[0] for o in offenders}
        assert lines == {"tests/test_synthetic.py:8", "tests/test_synthetic.py:12"}, offenders
        assert all("rate_limit._redis_client" in o for o in offenders)
        assert all("cqc_lem.app.engagement.feed._redis_client" in o for o in offenders)
