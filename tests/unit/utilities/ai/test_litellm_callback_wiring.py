"""Replay LiteLLM's `litellm_settings.callbacks` contract against `.litellm/config.yaml` (#1880).

Why this file exists: from #494 until #1880 the three custom modules were listed under
`custom_callbacks`, a key LiteLLM does not read. It is absent from the `litellm_settings` `elif`
chain in `proxy/proxy_server.py`, so it fell through to a generic `setattr(litellm, key, value)`.
Everything looked wired — the modules were mounted, readable and syntactically fine — and none of
them was ever imported. Nothing in the stack noticed for months.

Rewiring to the real key moves the failure mode from *silent* to *loud*: a bad entry raises inside
`_loaded_callback_or_raise` during config load and the proxy does not come up, taking the whole LLM
path with it. That is worse than three inert modules, so the entries have to be provable in CI.

So this replays the two rules LiteLLM applies, read off the version the proxy container serves
(litellm 1.100.0):

1. **Resolution** — `proxy/types_utils/utils.py::get_instance_fn` splits the entry on its LAST dot,
   resolves the module half as ``<dirname(config.yaml)>/<module>.py`` and `exec_module`s it, then
   `getattr`s the instance half. A file path or a `.py` suffix resolves nothing.
2. **Dispatchability** — `proxy/common_utils/callback_utils.py::_classify_loaded_callback` accepts a
   `CustomLogger` instance or a non-type callable, and raises on anything else (a CLASS instead of
   an instance being the common mistake).

litellm is a container dependency, not one of ours, so it is stubbed the same way
`test_complexity_router.py` stubs it — the rules are reimplemented here, not imported.

The half a unit test cannot see is whether the import actually happened in production. That is
`docker exec litellm ls /app/.litellm/__pycache__/` after a deploy: Python writes bytecode on first
import, so the directory's contents are a durable record independent of any logger.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
LITELLM_DIR = REPO_ROOT / ".litellm"
CONFIG_PATH = LITELLM_DIR / "config.yaml"
SETTINGS = yaml.safe_load(CONFIG_PATH.read_text())["litellm_settings"]

#: What `get_instance_fn` resolves module names against — `os.path.dirname(config_file_path)`, and
#: the config is passed to the container as `/app/.litellm/config.yaml`.
CONTAINER_CONFIG_DIR = "/app/.litellm"


class _StubCustomLogger:
    """Stands in for `litellm.integrations.custom_logger.CustomLogger`."""


@pytest.fixture
def stub_litellm(monkeypatch):
    """Put a minimal `litellm` on `sys.modules`, as the container's own package would be.

    Only `CustomLogger` is provided: the guards reach for `litellm.integrations.posthog` inside
    their `install()`, and its absence exercises the fail-open decline, which is the posture those
    two modules must keep.
    """
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = _StubCustomLogger
    monkeypatch.setitem(sys.modules, "litellm", types.ModuleType("litellm"))
    monkeypatch.setitem(sys.modules, "litellm.integrations", types.ModuleType("litellm.integrations"))
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", custom_logger)
    from cqc_lem.utilities import routing_policy

    monkeypatch.setitem(sys.modules, "routing_policy", routing_policy)
    return custom_logger


def _configured_entries() -> list[str]:
    entries = SETTINGS.get("callbacks")
    assert isinstance(entries, list) and entries, "litellm_settings.callbacks must be a non-empty list"
    return entries


def _resolve(entry: str):
    """Resolve `entry` exactly as `get_instance_fn` does, and return what it lands on."""
    module_name, _, instance_name = entry.rpartition(".")
    module_path = LITELLM_DIR / f"{module_name}.py"
    assert module_path.is_file(), f"{entry} resolves to {CONTAINER_CONFIG_DIR}/{module_name}.py, which does not exist"
    spec = importlib.util.spec_from_file_location(f"lem_wiring_{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, instance_name), f"{module_path.name} has no attribute {instance_name!r}"
    return getattr(module, instance_name)


def _is_dispatchable(loaded: object) -> bool:
    """LiteLLM's `_classify_loaded_callback` rule, verbatim."""
    return isinstance(loaded, _StubCustomLogger) or (callable(loaded) and not isinstance(loaded, type))


class TestTheDeadKeyIsGone:
    def test_custom_callbacks_is_not_used_anywhere_in_the_config(self):
        """The key that never loaded anything must not come back, in any nesting.

        Asserted on the parsed document rather than the text, so the prose above the block — which
        names `custom_callbacks` on purpose, to explain the defect — cannot satisfy it.
        """
        assert "custom_callbacks" not in SETTINGS
        assert "custom_callbacks" not in yaml.safe_load(CONFIG_PATH.read_text())

    def test_the_three_modules_are_all_wired_through_the_real_key(self):
        """A module missing from `callbacks` is a module that does not run — that was the defect."""
        modules = {entry.rpartition(".")[0] for entry in _configured_entries()}
        assert modules == {"complexity_router", "posthog_payload_guard", "posthog_redaction_guard"}


class TestEveryEntryWouldLoad:
    @pytest.mark.parametrize("entry", _configured_entries())
    def test_the_entry_is_a_dotted_module_path_not_a_file_path(self, entry):
        """`get_instance_fn` joins the module half onto the config's directory and appends `.py`.

        `/app/.litellm/complexity_router.py` therefore resolves to
        `/app/.litellm/app/.litellm/complexity_router/py.py`, finds nothing, falls through to
        `importlib.import_module` and raises at startup. Bare module name only.
        """
        assert "/" not in entry, f"{entry} is a path; `callbacks` takes `module.attribute`"
        assert not entry.endswith(".py"), f"{entry} carries a `.py` suffix; `callbacks` takes `module.attribute`"
        assert entry.count(".") == 1, f"{entry} must be exactly `module.attribute`"

    @pytest.mark.parametrize("entry", _configured_entries())
    def test_the_entry_resolves_to_a_dispatchable_object(self, entry, stub_litellm):
        """The check that stands between a typo and a proxy that will not boot.

        Resolution failure and a non-dispatchable object are the two ways config load raises. Both
        are decided here, from the same file that ships.
        """
        loaded = _resolve(entry)

        assert not isinstance(loaded, type), (
            f"{entry} resolves to a class; LiteLLM refuses that at startup — point it at an instance"
        )
        assert _is_dispatchable(loaded)


class TestTheGuardsStayFailOpen:
    """The two PostHog guards must load even where nothing they patch exists.

    `install()` declining is their designed silent mode; the module raising on import is not, and
    under `callbacks` an import that raises is a proxy that does not start.
    """

    @pytest.mark.parametrize(
        "entry",
        ["posthog_payload_guard.proxy_handler_instance", "posthog_redaction_guard.proxy_handler_instance"],
    )
    def test_the_handle_survives_litellm_being_absent_entirely(self, entry, monkeypatch):
        """Imported with no litellm at all, the handle is still something LiteLLM would dispatch.

        This is the degraded case — it should never happen inside the proxy image — but the handle
        is the one part of these modules that CANNOT fail open on its own, because a refused entry
        is a startup failure. So it falls back to a plain callable, which LiteLLM also accepts.
        """
        # A `None` entry in sys.modules makes `import` raise ImportError, which is the shape of a
        # container that has no litellm — and it works whether or not the package is installed here.
        monkeypatch.setitem(sys.modules, "litellm", None)
        monkeypatch.setitem(sys.modules, "litellm.integrations", None)
        monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", None)

        loaded = _resolve(entry)

        assert not isinstance(loaded, type)
        assert callable(loaded)
        assert loaded() is None
