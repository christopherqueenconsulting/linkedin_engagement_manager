"""No test may patch a moved symbol on `run_automation` and believe it did something (#1154).

The trap, in one sentence: **a re-export is a second binding.** Reading through a facade resolves the
same object; REBINDING one does not. So when the shared Selenium core moves down to
`utilities/linkedin/*` and takes its imports with it, a test still doing
`patch("cqc_lem.app.run_automation._comment_container")` rebinds a name that code no longer looks at.
The mock is never consulted, the real DOM helper runs against a `MagicMock` driver, and the
assertion passes for an unrelated reason.

Usually a move fails LOUDLY — `patch` raises `AttributeError` once the name is gone from
`run_automation`, which is exactly why the private helpers are not re-exported. The dangerous case is
a symbol that STAYS bound in `run_automation` for code that did not move while also being read by
code that did: `_reply_composer_for_comment` is read by `_reply_to_comment_inline` (stayed) and by
`_reply_under_comment_inline` (moved to `composer.py`). Patching it on `run_automation` still
resolves, nothing raises, and the reply that "went to the right comment" went wherever the unpatched
resolver actually sent it.

The check is therefore structural: it does not ask whether a test passes, it asks whether the patch
could bind anything the file exercises. Three conditions, and all three have to fire:

1. the file patches `run_automation.<sym>`, in ANY spelling it binds for that module path;
2. it names at least one function a moved module OWNS — i.e. it exercises moved code;
3. and it names NO `run_automation`-defined function that reads `<sym>` from `run_automation`'s own
   globals — i.e. there is nothing in that module for the patch to have been for.

**The scope is the FILE, not the `with` block, and that was learned by re-breaking this guard.** An
earlier version matched a 12-line window around the patch line, which is fine for
`test_reply_inline.py` — and completely blind to `test_comment_followups.py`, where the patches live
in a module-level `_worker_env()` helper sixty lines above the test that calls it. Both of those were
real breakages in this PR; a guard that catches one and not the other is worse than none, because it
reads as coverage.

Patching the same symbol on BOTH modules with one mock is the deliberate exemption:
`test_both_reply_paths_use_the_same_composer_resolver` spans the two modules on purpose, and that is
the shape a correct dual-module test has.
"""

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

_REPO = pathlib.Path(__file__).resolve().parents[3]
_RA_PATH = "cqc_lem.app.run_automation"
_RA_FILE = "src/cqc_lem/app/run_automation.py"

# Where the shared core landed. Everything under `utilities/linkedin/` is derived, so the next slice
# to push a helper down there inherits this guard instead of depending on someone extending a list.
# The three record-writers went to their EXISTING owners instead of a new module, so those are named.
#
# `app/engagement/` is the OTHER destination, and it carries the same hazard for a different reason:
# `run_automation` re-exports the TASKS a cluster took with it, so `run_automation.log_error` still
# resolves for a test driving `newsletter.auto_publish_edition` while the task reads its own
# module's binding. Listing the directory rather than its files means every later cluster (feed,
# posting, outreach) inherits this guard on arrival instead of needing someone to remember.
_MOVED_DIRS = ("src/cqc_lem/utilities/linkedin", "src/cqc_lem/app/engagement")
_MOVED_FILES = (
    "src/cqc_lem/utilities/golden_hour.py",
    "src/cqc_lem/utilities/lead_scoring.py",
    "src/cqc_lem/utilities/dm_templates.py",
)

# Deleted outright in #1154 rather than moved, because an alias left behind is a SILENT patch seam:
# `_strip_non_bmp` was literally `= strip_non_bmp`, and `_profile_slug` was byte-identical to
# `lead_scoring.profile_slug`. Their absence is what makes a stale patch of them raise.
_DELETED_FROM_RUN_AUTOMATION = ("_strip_non_bmp", "_profile_slug")

# How far either side of a `patch(...)` line still counts as the same `with` block, for the ONE
# question that is block-scoped: was the same symbol also patched where the moved code reads it?
_DUAL_LINES = 6


def _moved_modules(repo: pathlib.Path) -> list[pathlib.Path]:
    """Every module the shared core moved into."""
    out = [repo / f for f in _MOVED_FILES]
    for d in _MOVED_DIRS:
        out += [p for p in sorted((repo / d).glob("*.py")) if p.name != "__init__.py"]
    return [p for p in out if p.exists()]


def _module_facts(path: pathlib.Path) -> tuple[dict[str, set[str]], set[str]]:
    """`(symbol -> functions here that READ it, functions this module DEFINES)`.

    A name bound at module level is a hazard however it got there — defined here or imported here.
    `composer.py` does `from cqc_lem.utilities.logger import log_warning`; a test patching
    `run_automation.log_warning` for a composer function is asserting against a mock that function
    never consults. That import half is the one this shape of guard has missed before.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined: set[str] = set()
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}

    readers: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                    and sub.id in bound and sub.id != node.name):
                readers.setdefault(sub.id, set()).add(node.name)
    return readers, defined


def _spellings(text: str, module_path: str) -> list[str]:
    """Every way this file writes a patch target for `module_path`, as a literal prefix to match.

    Files pick their own alias — `_RA`, `RA`, `_MOD`, `COMPOSER` — and a check that knew only one
    spelling is how this shape of guard has shipped blind before. Resolved from the file's OWN
    assignments rather than assumed.
    """
    out = [f'"{module_path}.', f"'{module_path}."]
    for alias in re.findall(rf'^(\w+)\s*=\s*["\']{re.escape(module_path)}["\']', text, re.MULTILINE):
        out.append("{" + alias + "}.")
    return out


def _patch_sites(text: str, spellings: list[str]) -> list[tuple[str, str]]:
    """`(symbol, surrounding lines)` for every patch of `<module>.<symbol>` in `text`."""
    if not spellings:
        return []
    pattern = re.compile("(?:" + "|".join(re.escape(sp) for sp in spellings) + r")(\w+)")
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        for match in pattern.finditer(line):
            out.append((match.group(1),
                        "\n".join(lines[max(0, i - _DUAL_LINES):i + _DUAL_LINES])))
    return out


def _patched_symbols(text: str, spellings: list[str]) -> set[str]:
    """Every `<symbol>` patched on a module anywhere in `text`."""
    return {sym for sym, _block in _patch_sites(text, spellings)}


def _names_called(text: str) -> set[str]:
    """Every identifier the file CALLS.

    Deliberately narrower than "mentions", and that asymmetry is the whole design. A patch TARGET is
    a mention, not a call — `patch(f"{RA}.login_to_linkedin")` writes the name followed by a quote,
    never by `(` — so requiring a call is what separates "this file exercises the moved module" from
    "this file happens to name one of its functions while stubbing it out". Mentions were tried first
    and produced six false positives on this tree.
    """
    return set(re.findall(r"\b(\w+)\s*\(", text))


def _names_used(text: str) -> set[str]:
    """Every identifier the file mentions — used ONLY for the exemption, never for the trigger.

    A loose exemption can only cost a miss; a loose trigger costs noise, and a noisy guard gets
    ignored. `sweep_reply_comments.run(...)` never spells the task with a `(` after it, so a
    call-based exemption would flag a perfectly correct patch.
    """
    return set(re.findall(r"\b\w+\b", text))


def _ra_entry_points(readers: dict[str, set[str]], defined: set[str]) -> dict[str, set[str]]:
    """`symbol -> every run_automation function from which a run_automation READER is reachable`.

    The exemption has to be transitive or it fires on a one-hop indirection. `_visible_composers` is
    read by `_composer_in_post_scope`, which `test_linkedin_live_validation.py` never names — it
    names `_post_composer_for_card`, one call above. Patching `_visible_composers` on
    `run_automation` is CORRECT there, and a guard that says otherwise is the noise that gets guards
    deleted.
    """
    callers: dict[str, set[str]] = {}
    for symbol, reading_fns in readers.items():
        if symbol in defined:
            for fn in reading_fns:
                callers.setdefault(symbol, set()).add(fn)

    out: dict[str, set[str]] = {}
    for symbol, reading_fns in readers.items():
        seen, stack = set(reading_fns), list(reading_fns)
        while stack:
            fn = stack.pop()
            for up in callers.get(fn, ()):
                if up not in seen:
                    seen.add(up)
                    stack.append(up)
        out[symbol] = seen
    return out


def offenders(test_files: list[pathlib.Path], modules: list[pathlib.Path],
              repo: pathlib.Path, ra_file: pathlib.Path) -> list[str]:
    """The check itself, factored out so the anti-vacuity tests can drive the SAME code."""
    ra_readers_direct, ra_defined = _module_facts(ra_file)
    ra_readers = _ra_entry_points(ra_readers_direct, ra_defined)
    facts = []
    for module in modules:
        dotted = str(module.relative_to(repo)).removesuffix(".py").replace("/", ".")
        facts.append((dotted.removeprefix("src."), *_module_facts(module)))

    found: list[str] = []
    for path in test_files:
        if path.name == pathlib.Path(__file__).name:
            continue  # this file quotes the offending patch strings in its own prose
        text = path.read_text(errors="ignore")
        if _RA_PATH not in text:
            continue
        ra_sites = _patch_sites(text, _spellings(text, _RA_PATH))
        if not ra_sites:
            continue
        called, used = _names_called(text), _names_used(text)
        for dotted, readers, defined in facts:
            exercised = sorted(called & defined)
            if not exercised:
                continue  # the file never calls into this module
            module_spellings = _spellings(text, dotted)
            for symbol, block in sorted(set(ra_sites)):
                if symbol not in readers:
                    continue
                # The dual-module shape is judged on the BLOCK, never the file: a file that patches
                # the symbol correctly in four tests and staler-ly in a fifth is exactly the case
                # this exists for, and a file-wide exemption reads that as compliant. Found by
                # re-breaking one patch in `test_reply_inline.py` and watching the guard stay green.
                if symbol in _patched_symbols(block, module_spellings):
                    continue
                if used & ra_readers.get(symbol, set()):
                    continue  # run_automation code that reaches a reader of it IS named here
                found.append(
                    f"{path.relative_to(repo)} patches {symbol} on run_automation, but the code it "
                    f"calls that reads {symbol} is {dotted} "
                    f"({', '.join(sorted(readers[symbol])[:3])}), which reads it from its OWN "
                    f"globals; target {dotted}.{symbol}")
    return sorted(set(found))


def _run(repo: pathlib.Path, test_files: list[pathlib.Path], modules: list[pathlib.Path],
         ra_file: pathlib.Path) -> list[str]:
    return offenders(test_files, modules, repo, ra_file)


class TestNoTestPatchesAMovedSymbolOnRunAutomation:
    def test_there_are_moved_modules_to_check(self):
        """Anti-vacuity: with no destination modules found, everything below reads nothing."""
        names = {m.name for m in _moved_modules(_REPO)}
        assert {"cards.py", "composer.py", "session.py", "dm_templates.py",
                "invites.py", "newsletter.py"} <= names, names

    def test_the_derivation_finds_reads_of_every_binding_kind(self, tmp_path):
        """A defined callee, a module constant and an IMPORTED name are all the same hazard."""
        (tmp_path / "widgets.py").write_text(
            "from cqc_lem.utilities.logger import log_warning\n"
            "\n"
            "WIDGET_LIMIT = 5\n"
            "UNREAD_LIMIT = 9\n"
            "\n"
            "def read_widget(x):\n"
            "    return x\n"
            "\n"
            "def list_widgets():\n"
            "    log_warning('x')\n"
            "    return [read_widget(1)][:WIDGET_LIMIT]\n"
            "\n"
            "def untouched():\n"
            "    return None\n")
        readers, defined = _module_facts(tmp_path / "widgets.py")
        assert readers == {"read_widget": {"list_widgets"},
                           "WIDGET_LIMIT": {"list_widgets"},
                           "log_warning": {"list_widgets"}}
        assert defined == {"read_widget", "list_widgets", "untouched"}

    @staticmethod
    def _synthetic(tmp_path):
        """A miniature repo: one moved module, one `run_automation` that imports from it."""
        module = tmp_path / "src" / "cqc_lem" / "utilities" / "linkedin"
        module.mkdir(parents=True)
        (module / "widgets.py").write_text(
            "from cqc_lem.utilities.logger import log_warning\n"
            "\n"
            "def open_widget(driver):\n"
            "    log_warning('nope')\n"
            "    return None\n")
        ra_dir = tmp_path / "src" / "cqc_lem" / "app"
        ra_dir.mkdir(parents=True)
        ra = ra_dir / "run_automation.py"
        ra.write_text(
            "from cqc_lem.utilities.linkedin.widgets import open_widget\n"
            "from cqc_lem.utilities.logger import log_warning\n"
            "\n"
            "def sweep(driver):\n"
            "    return open_widget(driver)\n"
            "\n"
            "def unrelated():\n"
            "    log_warning('this one really is run_automation\\'s')\n")
        (tmp_path / "tests").mkdir()
        return module / "widgets.py", ra

    def test_a_stale_patch_is_flagged_even_when_it_is_nowhere_near_the_call(self, tmp_path):
        """The assertion this class exists for, driven against synthetic offenders.

        This shape of guard has shipped blind three times in this repo — a bare recursive call
        instead of `yield from`, matching only the test body and not the enclosing class, and knowing
        only one alias spelling. A fourth was found while writing THIS one: a window around the patch
        line missed `test_comment_followups.py` entirely, whose patches sit in a module-level helper
        far above the test that calls it. So the offender below is written in exactly that shape.
        """
        widgets, ra = self._synthetic(tmp_path)
        tests = tmp_path / "tests"
        bad = tests / "test_bad.py"
        bad.write_text(
            'from contextlib import ExitStack\n'
            'from unittest.mock import patch\n'
            '\n'
            'RA = "cqc_lem.app.run_automation"\n'
            '\n'
            '\n'
            'def _env(es):\n'
            '    """The patch lives HERE, sixty lines from the test that uses it."""\n'
            '    es.enter_context(patch(f"{RA}.log_warning"))\n'
            '\n'
            '\n'
            'class TestWidget:\n'
            '    def test_it_warns(self):\n'
            '        from cqc_lem.utilities.linkedin.widgets import open_widget\n'
            '        with ExitStack() as es:\n'
            '            _env(es)\n'
            '            open_widget(None)\n')
        assert _run(tmp_path, [bad], [widgets], ra), "the stale patch was NOT flagged"

    def test_the_two_correct_shapes_are_not_flagged(self, tmp_path):
        """A guard that flags a correct patch gets ignored, so pin both exemptions too."""
        widgets, ra = self._synthetic(tmp_path)
        tests = tmp_path / "tests"

        dual = tests / "test_dual.py"
        dual.write_text(
            'from unittest.mock import patch\n'
            '\n'
            'RA = "cqc_lem.app.run_automation"\n'
            'W = "cqc_lem.utilities.linkedin.widgets"\n'
            '\n'
            '\n'
            'class TestWidget:\n'
            '    def test_it_warns(self):\n'
            '        from cqc_lem.utilities.linkedin.widgets import open_widget\n'
            '        with patch(f"{RA}.log_warning"), patch(f"{W}.log_warning") as w:\n'
            '            open_widget(None)\n'
            '        w.assert_called_once()\n')

        stayed = tests / "test_stayed.py"
        stayed.write_text(
            'from unittest.mock import patch\n'
            '\n'
            'RA = "cqc_lem.app.run_automation"\n'
            '\n'
            '\n'
            'class TestUnrelated:\n'
            '    def test_it_warns(self):\n'
            '        from cqc_lem.app.run_automation import unrelated, open_widget\n'
            '        with patch(f"{RA}.log_warning") as w:\n'
            '            unrelated()\n'
            '        w.assert_called_once()\n')

        assert _run(tmp_path, [dual], [widgets], ra) == [], "the dual-module patch is correct"
        assert _run(tmp_path, [stayed], [widgets], ra) == [], (
            "patching a symbol a run_automation function still reads is correct")

    def test_run_automation_never_re_aliases_a_deleted_duplicate(self):
        """`_strip_non_bmp` and `_profile_slug` are gone; re-adding either is a SILENT seam."""
        import cqc_lem.app.run_automation as ra
        present = [n for n in _DELETED_FROM_RUN_AUTOMATION if hasattr(ra, n)]
        assert present == [], (
            f"{present} was deleted in #1154 as a duplicate of an upstream original — re-aliasing it "
            "makes every stale patch of it resolve while binding nothing the code reads")

    def test_no_test_patches_a_moved_symbol_on_run_automation(self):
        found = offenders(sorted((_REPO / "tests").rglob("test_*.py")),
                          _moved_modules(_REPO), _REPO, _REPO / _RA_FILE)
        assert found == [], "\n  ".join([""] + found)
