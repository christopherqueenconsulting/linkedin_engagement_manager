"""DEMO_MODE — the switch that keeps a recording off LinkedIn (#2372).

When ``DEMO_MODE`` is truthy, every LinkedIn client raises `DemoModeError` BEFORE it opens a
connection. There is one choke point per transport, so a new client cannot slip past by forgetting
a call:

* **HTTP** — `install_requests_guard` wraps ``requests.Session.send``, the method every
  ``requests.get/post/put`` call AND the ``linkedin_api`` SDK (``RestliClient``, ``AuthClient``)
  funnel through. Any request whose host is LinkedIn's raises there. The package root installs it,
  so every process that imports ``cqc_lem`` carries it.
* **Selenium** — `selenium_util.get_docker_driver`, the ONE way a browser session is opened, calls
  `guard_linkedin` before it touches the Grid. The single exemption is a ``demo_safe=True`` session
  that films our own SPA and never navigates to LinkedIn.

The publish helpers in ``linkedin/poster.py`` and ``linkedin/reshare.py`` also call
`guard_linkedin` at their entry, because several of them wrap their LinkedIn calls in a broad
``except Exception`` that would otherwise turn the transport's refusal into a quiet ``None``.

This is a safety control, not a feature flag: it is read from the environment at CALL time and never
resolved through PostHog. Off by default.
"""

import functools
import os
from typing import Any
from urllib.parse import urlparse

# A host is LinkedIn's when it IS one of these or a subdomain of one — never a substring match.
LINKEDIN_HOSTS = ("linkedin.com", "licdn.com", "lnkd.in")

_TRUTHY = ("true", "1", "t", "y", "yes")


class DemoModeError(RuntimeError):
    """A LinkedIn call was refused because ``DEMO_MODE`` is on.

    Attributes:
        surface: Which client refused, e.g. ``"poster.share_on_linkedin"`` or ``"http:api.linkedin.com"``.
    """

    def __init__(self, surface: str) -> None:
        """Name the refused surface in the message so a log line says what was stopped."""
        self.surface = surface
        super().__init__(f"DEMO_MODE is on: LinkedIn call refused ({surface})")


def is_demo_mode() -> bool:
    """Whether ``DEMO_MODE`` is on, read from the environment on every call.

    Accepts the same truthy spellings as ``env_constants.isTrue``; unset, empty or anything else is
    off.
    """
    # `os.environ.get`, not `os.getenv`: a test that patches the module-wide `os.getenv` to steer
    # some other flag must not switch demo mode on by accident.
    return os.environ.get("DEMO_MODE", "").strip().lower() in _TRUTHY


def _hosts(url: str) -> set:
    """Every hostname the URL could be read as — the stdlib's reading AND urllib3's.

    urllib3 is what `requests` actually connects with. Checking both readings closes any parser
    differential (e.g. a backslash before ``@``) where one parser sees LinkedIn and the other does
    not: if EITHER sees LinkedIn the request is refused.
    """
    hosts = set()
    try:
        hosts.add(urlparse(url).hostname or "")
    except ValueError:
        # An unparseable URL contributes no host from this parser; the urllib3 read below still runs.
        pass
    try:
        from urllib3.util import parse_url
        hosts.add(parse_url(url).host or "")
    except Exception:  # noqa: BLE001 - an unparseable URL simply contributes no host
        pass
    return {h.lower().strip("[]").rstrip(".") for h in hosts if h}


def is_linkedin_url(url: Any) -> bool:
    """True when ``url``'s parsed hostname is a LinkedIn host or a subdomain of one.

    Matches names only: a bare IP literal is not recognised (see docs/demo-mode.md).
    """
    return any(host == h or host.endswith("." + h)
               for host in _hosts(str(url or "")) for h in LINKEDIN_HOSTS)


def guard_linkedin(surface: str) -> None:
    """Raise `DemoModeError` when demo mode is on; otherwise do nothing.

    Args:
        surface: The client being entered, carried on the exception and the log line.

    Raises:
        DemoModeError: ``DEMO_MODE`` is on.
    """
    if not is_demo_mode():
        return
    # INFO, not WARNING: in demo mode a refusal is the expected outcome, never a defect.
    from cqc_lem.utilities.logger import log_info
    log_info(f"DEMO_MODE: refused LinkedIn call at {surface}", action_type="demo_mode")
    raise DemoModeError(surface)


def install_requests_guard() -> None:
    """Wrap ``requests.Session.send`` so a LinkedIn-bound request raises in demo mode. Idempotent.

    ``Session.send`` sits below ``requests.get/post`` and the ``linkedin_api`` SDK's own sessions,
    and above the adapter that opens the socket, so the refusal happens before any connection.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return
    original = requests.Session.send
    if getattr(original, "_lem_demo_guard", False):
        return

    @functools.wraps(original)
    def send(self: Any, request: Any, **kwargs: Any) -> Any:
        url = getattr(request, "url", "")
        if is_demo_mode() and is_linkedin_url(url):
            guard_linkedin(f"http:{urlparse(str(url)).hostname}")
        return original(self, request, **kwargs)

    send._lem_demo_guard = True  # type: ignore[attr-defined]
    requests.Session.send = send
