"""No test may patch a `cqc_lem.utilities.linkedin.*` symbol on its defining module while
exercising an `app.engagement.*` module that imported that symbol directly.

Issue #1209. `app/run_automation.py` was deleted in #1206/#1207, removing the stale
`cqc_lem.app.run_automation` patch seam. The same trap now lives one layer down:
`app.engagement.feed` imports `_redis_client` from `utilities.linkedin.rate_limit` and reads
it from its OWN globals, so a test patching `cqc_lem.utilities.linkedin.rate_limit._redis_client`
rebinds a name nothing in the engagement module consults. The mock is never called, the real Redis
client runs, and the assertion happens to hold for an unrelated reason.

This guard derives the hazard set from the engagement modules' AST rather than listing it, so
adding or renaming a moved symbol does not silently retire the check. It mirrors
`tests/unit/platform/db/test_connection_seam.py` and `tests/unit/api/test_router_patch_seam.py`.
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


def _engagement_modules() -> list[pathlib.Path]:
    return sorted(p for p in _ENGAGEMENT_DIR.glob("*.py") if p.name != "__init__.py")


def _module_globals(path: pathlib.Path) -> dict[str, tuple[str, str]]:
    """Map local name -> (source module, imported name) for direct `utilities.linkedin` imports.

    `from cqc_lem.utilities.linkedin.cards import _card_for_textbox as card` binds `card` locally;
    a patch on `cards._card_for_textbox` would miss `M.card`. Module-as imports (`import X as _X`)
    bind the MODULE, so attributes reached through them are shared objects and not this hazard.
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
    """Map function name -> set of local imported names the function reads."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    readers: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        reads = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id in local_names:
                reads.add(sub.id)
            # Attribute load through a module alias (`_zw.report_zero_walk`) is NOT a direct read
            # of the imported symbol; it reaches the same module object and sees patches there.
        readers[node.name] = reads
    return readers


def _hazard_set() -> dict[str, dict[str, set[str]]]:
    """symbol -> {engagement_module: {function_names that read the local binding}}.

    The outer key is the defining-module symbol (`cqc_lem.utilities.linkedin.rate_limit._redis_client`)
    so test scanning can look it up directly.
    """
    hazards: dict[str, dict[str, set[str]]] = {}
    for path in _engagement_modules():
        local_map = _module_globals(path)
        local_names = set(local_map)
        readers = _reader_functions(path, local_names)
        for local, (source, imported) in local_map.items():
            symbol = f"{source}.{imported}"
            funcs = {fn for fn, reads in readers.items() if local in reads}
            if funcs:
                hazards.setdefault(symbol, {}).setdefault(path.stem, funcs)
    return hazards


def _aliases_for(path: pathlib.Path, module: str) -> list[str]:
    """`{_M}`-style f-string spellings of `module` assigned as constants in this file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and node.value.value == module):
            out += ["{" + t.id + "}" for t in node.targets if isinstance(t, ast.Name)]
    return out


def _facade_patch_blocks(text: str, sym: str) -> list[tuple[int, str]]:
    """Every patch context around a patch of `sym` on a `cqc_lem.utilities.linkedin.*` module.

    Returns (line_number, block_text) so the caller can report where the offence is. `block_text`
    is the body of the innermost function/method that contains the matched line, parsed from the
    original file's AST — so backslash-continued `with` statements and nested classes are handled
    correctly.
    """
    all_lines = text.splitlines()
    tree = ast.parse(text)
    aliases = _aliases_for_from_text(text, _LINKEDIN_PREFIX.rstrip("."))
    patterns = [rf'["\']{re.escape(sym)}["\']']
    # Match literal module aliases too: patch("{_RL}._redis_client")
    module, _, name = sym.rpartition(".")
    for alias in aliases.get(module, []):
        patterns.append(rf'\{{{re.escape(alias.lstrip("{").rstrip("}"))}\}}\.{re.escape(name)}\b')
    out = []
    for i, line in enumerate(all_lines, start=1):
        if not any(re.search(pat, line) for pat in patterns):
            continue
        # Find the innermost function/class that contains this line.
        best: ast.AST | None = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.lineno <= i <= node.end_lineno:
                    if best is None or node.lineno > best.lineno:
                        best = node
        if best is None:
            continue
        body = "\n".join(all_lines[best.lineno - 1:best.end_lineno])
        out.append((i, body))
    return out


def _aliases_for_from_text(text: str, module: str) -> dict[str, list[str]]:
    """Map module path -> braced alias names found in `text`."""
    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        return {}
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(node.value.value, []).append("{" + t.id + "}")
    return out


def _parse_test_files() -> list[tuple[pathlib.Path, str, list[ast.AST | None], dict[str, list[str]]]]:
    """Pre-parse every unit test file once and build a line-to-container index.

    This is the hot path: the naive version re-parsed every file for each of ~70 hazard symbols,
    which cost ~120s. Doing it once and reusing the index brings the guard down to sub-second.
    """
    out = []
    for path in sorted((_REPO / "tests" / "unit").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(text)
        containers = sorted(
            (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))),
            key=lambda n: (n.lineno, -n.end_lineno)
        )
        line_to_best: list[ast.AST | None] = [None] * (len(text.splitlines()) + 1)
        for n in containers:
            for i in range(n.lineno, n.end_lineno + 1):
                line_to_best[i] = n
        aliases: dict[str, list[str]] = {}
        for node in tree.body:
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        aliases.setdefault(node.value.value, []).append("{" + t.id + "}")
        out.append((path, text, line_to_best, aliases))
    return out


def _all_patch_blocks_for_symbols(
        text: str,
        line_to_best: list[ast.AST | None],
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
    symbol_for_name: dict[str, str] = {}
    literal_pats: list[str] = []
    for sym in symbols:
        literal_pats.append(re.escape(sym))
        module, _, name = sym.rpartition(".")
        by_module.setdefault(module, []).append(name)
        symbol_for_name[name] = sym
    alias_pats: list[str] = []
    for module, names in by_module.items():
        for alias in aliases.get(module, []):
            alias_pats.append(
                re.escape(alias) + r"\.(" + "|".join(re.escape(n) for n in names) + r")\b")
    parts = [rf'["\'](' + "|".join(literal_pats) + r')["\']']
    if alias_pats:
        parts.append(r"(" + "|".join(alias_pats) + r")")
    pat = re.compile("|".join(parts))
    # Reverse map so an alias token like `{_CMP}` resolves back to its module path.
    alias_to_module: dict[str, str] = {}
    for module, alias_list in aliases.items():
        for alias in alias_list:
            alias_to_module[alias] = module
    all_lines = text.splitlines()
    out: list[tuple[int, str, set[str]]] = []
    seen: dict[tuple[int, int], tuple[int, str, set[str]]] = {}
    for m in pat.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        n = line_to_best[line_no]
        if n is None:
            continue
        key = (n.lineno, n.end_lineno)
        block = "\n".join(all_lines[n.lineno - 1:n.end_lineno])
        matched = _symbols_patched_by_match(m, symbols, alias_to_module)
        if key not in seen:
            seen[key] = (line_no, block, matched)
        else:
            seen[key][2].update(matched)
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


def _engagement_functions_in_block(block: str, hazards: dict[str, dict[str, set[str]]]) -> list[str]:
    """Which hazard symbols have a reader function called inside `block`.

    `block` is extracted from an existing Python file, so it may start with arbitrary indentation.
    We de-indent it before parsing so AST walks work regardless of how deeply nested the patch was.

    A block may call engagement functions in three ways:

    1. `cqc_lem.app.engagement.<mod>.<fn>()`
    2. `<alias>.<fn>()` where `<alias>` is bound to an engagement module
    3. `from cqc_lem.app.engagement.<mod> import <fn>` followed by `<fn>()` inside the block

    We only flag when the called function is a KNOWN reader of the patched symbol in that module.
    """
    tree = ast.parse(textwrap.dedent(block))
    aliases = _aliases_for_from_text(block, "cqc_lem.app.engagement.")
    # Build map: alias -> module stem
    alias_to_stem: dict[str, str] = {}
    for full, braced in aliases.items():
        stem = full.rpartition(".")[2]
        for b in braced:
            alias_to_stem[b.strip("{}")] = stem

    # Resolve direct imports of engagement functions/modules inside this block (case 3).
    direct_imports: dict[str, tuple[str, str]] = {}
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module.startswith("cqc_lem.app.engagement."):
            stem = node.module.rpartition(".")[2]
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == stem and alias.asname:
                    # `from cqc_lem.app.engagement import posting as p` — module alias
                    module_aliases[local] = stem
                else:
                    direct_imports[local] = (stem, alias.name)
        elif node.module == "cqc_lem.app.engagement":
            # `from cqc_lem.app.engagement import posting` (no `.posting` suffix)
            for alias in node.names:
                local = alias.asname or alias.name
                module_aliases[local] = alias.name

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        mod_stem: str | None = None
        fn_name: str | None = None
        if isinstance(node.func, ast.Attribute):
            fn_name = node.func.attr
            if isinstance(node.func.value, ast.Name):
                val = node.func.value.id
                if val in alias_to_stem:
                    mod_stem = alias_to_stem[val]
                elif val in module_aliases:
                    mod_stem = module_aliases[val]
                elif re.fullmatch(r"cqc_lem\.app\.engagement\.\w+", val):
                    # bare module name used as identifier (rare, but possible)
                    mod_stem = val.rpartition(".")[2]
            elif isinstance(node.func.value, ast.Attribute):
                # e.g. cqc_lem.app.engagement.feed.automate_commenting()
                dotted = _dotted_name(node.func.value)
                if dotted and dotted.startswith("cqc_lem.app.engagement."):
                    mod_stem = dotted.split(".")[3]
        elif isinstance(node.func, ast.Name):
            # Direct call like `use_redis()` after `from ... import use_redis`.
            if node.func.id in direct_imports:
                mod_stem, fn_name = direct_imports[node.func.id]
        if mod_stem is None or fn_name is None:
            continue
        for symbol, mods in hazards.items():
            if mod_stem in mods and fn_name in mods[mod_stem]:
                found.append(symbol)
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


class TestLinkedInEngagementPatchSeam:
    def test_there_are_hazards_to_check(self):
        """Anti-vacuity: if nothing is imported directly, the check below passes having read nothing."""
        assert _hazard_set(), (
            "no direct `cqc_lem.utilities.linkedin.*` imports found in app/engagement/*.py — "
            "the seam check would be vacuous")

    def test_no_test_patches_a_direct_import_on_the_defining_module(self):
        """The core guard: a patch on the source module plus a reader in the importer is an error."""
        hazards = _hazard_set()
        test_files = _parse_test_files()
        offenders = []
        for path, text, line_to_best, aliases in test_files:
            if path.name == "test_linkedin_engagement_patch_seam.py":
                continue
            for line, block, matched_symbols in _all_patch_blocks_for_symbols(text, line_to_best, aliases, sorted(hazards)):
                called = _engagement_functions_in_block(block, hazards)
                for symbol in set(called) & matched_symbols:
                    rel = path.relative_to(_REPO)
                    offenders.append(
                        f"{rel}:{line} patches {symbol} while calling a reader in "
                        f"cqc_lem.app.engagement.{list(hazards[symbol].keys())[0]}; "
                        f"patch cqc_lem.app.engagement.<module>.{symbol.rpartition('.')[2]} instead")
        assert offenders == [], "\n  ".join([""] + offenders)

    def test_hazard_derivation_fires_on_synthetic_code(self, tmp_path):
        """Anti-vacuity: the guard must detect a deliberately wrong patch target."""
        eng = tmp_path / "engagement.py"
        eng.write_text(
            "from cqc_lem.utilities.linkedin.rate_limit import _redis_client\n"
            "\n"
            "def use_redis():\n"
            "    return _redis_client()\n"
            "\n"
            "def no_redis():\n"
            "    return 1\n")
        # Put it under a fake app/engagement directory so _engagement_modules finds it.
        fake_eng = tmp_path / "app" / "engagement"
        fake_eng.mkdir(parents=True)
        (fake_eng / "__init__.py").write_text("")
        (fake_eng / "feed.py").write_text(eng.read_text())

        # Derive hazards from the fake tree.
        hazards = _hazard_set_for_dir(fake_eng)
        assert "cqc_lem.utilities.linkedin.rate_limit._redis_client" in hazards
        assert hazards["cqc_lem.utilities.linkedin.rate_limit._redis_client"]["feed"] == {"use_redis"}

        # A block that patches the defining module while calling use_redis must be flagged.
        block = (
            'with patch("cqc_lem.utilities.linkedin.rate_limit._redis_client"):\n'
            '    from cqc_lem.app.engagement.feed import use_redis\n'
            '    use_redis()\n')
        called = _engagement_functions_in_block(block, hazards)
        assert "cqc_lem.utilities.linkedin.rate_limit._redis_client" in called

        # Patching the engagement module directly is correct and must NOT be flagged.
        block_ok = (
            'with patch("cqc_lem.app.engagement.feed._redis_client"):\n'
            '    use_redis()\n')
        called_ok = _engagement_functions_in_block(block_ok, hazards)
        assert "cqc_lem.utilities.linkedin.rate_limit._redis_client" not in called_ok


def _hazard_set_for_dir(directory: pathlib.Path) -> dict[str, dict[str, set[str]]]:
    """Variant of _hazard_set that reads a custom engagement directory (used by synthetic test)."""
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
