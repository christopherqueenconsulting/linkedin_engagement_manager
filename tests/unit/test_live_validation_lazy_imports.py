"""Issue #1215: every `cqc_lem` import buried inside the live-validation probe still resolves.

`scripts/linkedin_live_validation.py` reaches into production modules from INSIDE its probe
functions — roughly sixty `from cqc_lem... import ...` statements that only execute when that one
probe runs against a live browser. Nothing in CI drives a browser, so those statements are never
executed by any lane, and a rename on the production side of them is invisible until someone runs
the probe by hand against LinkedIn. Three separate restructure slices shipped exactly that break.

The probe's own unit tests (`test_linkedin_live_validation.py`) call the probe functions they cover
with fakes, so they resolve the imports on the paths they reach — but the file is 4.5k lines and the
uncovered paths are precisely the ones a restructure silently rots.

This test closes the gap statically, and deliberately so: it reads the imports out of the AST rather
than executing the probe, so it needs no browser, no session and no LinkedIn, and it fails with the
line number of the stale import rather than with a NameError two hours into a manual probe run. What
it proves is narrow and exact — the module still imports and still exports that name. Whether the
probe then uses it correctly is what the rest of that file's tests are for.
"""

import ast
import importlib
from pathlib import Path

import pytest

_PROBE = Path(__file__).resolve().parents[2] / "scripts" / "linkedin_live_validation.py"
_PACKAGE = "cqc_lem"


def _deferred_cqc_lem_imports() -> list[tuple[int, str, tuple[str, ...]]]:
    """Every non-top-level `cqc_lem` import in the probe, as (line, module, imported names).

    Top-level imports are excluded because importing the script — which
    `test_linkedin_live_validation.py` does on collection — already proves those. The ones that rot
    unseen are the function-local ones.
    """
    tree = ast.parse(_PROBE.read_text(encoding="utf-8"), str(_PROBE))
    top_level = {id(node) for node in tree.body}

    found: list[tuple[int, str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if id(node) in top_level:
            continue
        if isinstance(node, ast.ImportFrom):
            # `from . import x` has no module; the probe is a standalone script, so it has none.
            if node.module and node.module.split(".")[0] == _PACKAGE:
                found.append((node.lineno, node.module, tuple(alias.name for alias in node.names)))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == _PACKAGE:
                    found.append((node.lineno, alias.name, ()))
    return sorted(found)


_DEFERRED = _deferred_cqc_lem_imports()


def _identifier(entry: tuple[int, str, tuple[str, ...]]) -> str:
    line, module, names = entry
    return f"L{line}:{module}:{','.join(names) or '<module>'}"


@pytest.mark.unit
def test_the_probe_defers_cqc_lem_imports_at_all():
    """A guard on the guard: if this drops to zero the sweep below is silently vacuous.

    That would mean the probe stopped importing production code lazily — worth noticing, because
    then this whole file is dead weight rather than quietly protecting nothing.
    """
    assert len(_DEFERRED) > 20, (
        f"expected the probe to carry dozens of deferred cqc_lem imports, found {len(_DEFERRED)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("entry", _DEFERRED, ids=_identifier)
def test_deferred_probe_import_still_resolves(entry: tuple[int, str, tuple[str, ...]]):
    """The named module imports, and every name the probe pulls out of it is still there."""
    line, module_name, names = entry
    try:
        module = importlib.import_module(module_name)
    except Exception as e:  # noqa: BLE001 - an import that raises at all is the defect being hunted
        pytest.fail(
            f"{_PROBE.name}:{line} imports `{module_name}`, which no longer imports: "
            f"{type(e).__name__}: {e}. A restructure moved or renamed it — update the probe."
        )

    for name in names:
        if hasattr(module, name):
            continue
        # `from cqc_lem.utilities.linkedin import rate_limit` is a SUBMODULE import: the attribute
        # only appears on the package once the submodule has been imported, so a plain hasattr()
        # would report a healthy import as broken.
        try:
            importlib.import_module(f"{module_name}.{name}")
        except Exception:  # noqa: BLE001 - not a submodule either, so the name is simply gone
            pytest.fail(
                f"{_PROBE.name}:{line} imports `{name}` from `{module_name}`, which no longer "
                f"exports it. A restructure renamed or moved it — update the probe."
            )
