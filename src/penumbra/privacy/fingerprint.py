"""Browser fingerprint randomization — never look the same twice.

This module produces a `Fingerprint` (user-agent, viewport, locale, timezone,
platform) and exposes helpers to apply it to a Playwright `BrowserContext`.

The user-agent pool is hand-curated and intentionally narrow: only currently-
released Chrome versions on common platforms. A stale or weird UA is worse than
a slightly less unique one because anti-bot systems treat oddities as signals.

Penumbra does NOT try to fight every fingerprinting vector (canvas, WebGL,
audio context). Doing that well requires a stealth-browser project, and
trying it halfway is worse than not trying. We do the high-leverage stuff
(UA, viewport, locale, timezone, WebRTC IP leak, navigator.webdriver) and
document the rest as out-of-scope.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext


_CHROME_VERSIONS: Final[tuple[str, ...]] = (
    "131.0.6778.205",
    "132.0.6834.83",
    "133.0.6943.16",
)

_PLATFORMS: Final[tuple[tuple[str, str], ...]] = (
    ("Windows NT 10.0; Win64; x64", "Win32"),
    ("Macintosh; Intel Mac OS X 10_15_7", "MacIntel"),
    ("X11; Linux x86_64", "Linux x86_64"),
)

_VIEWPORTS: Final[tuple[tuple[int, int], ...]] = (
    (1920, 1080),
    (1536, 864),
    (1440, 900),
    (1366, 768),
    (2560, 1440),
    (1680, 1050),
)

_LOCALES: Final[tuple[str, ...]] = (
    "en-US",
    "en-GB",
    "en-CA",
    "de-DE",
    "fr-FR",
    "it-IT",
    "es-ES",
    "nl-NL",
)

_TIMEZONES: Final[tuple[str, ...]] = (
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Rome",
    "Europe/Amsterdam",
)


@dataclass(slots=True, frozen=True)
class Fingerprint:
    """A single browser identity. Re-use within a session, rotate between sessions."""

    user_agent: str
    platform: str
    viewport: tuple[int, int]
    locale: str
    timezone: str
    color_scheme: str = "light"

    @property
    def viewport_dict(self) -> dict[str, int]:
        return {"width": self.viewport[0], "height": self.viewport[1]}


_STEALTH_JS: Final[str] = """
// Hide automation signal that Playwright sets by default.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Mock plausible plugins/languages so detection scripts see something.
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Generic Plugin' })),
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['__LOCALE__', 'en'],
});

// chrome runtime stub — present on real Chrome, absent on headless.
window.chrome = window.chrome || { runtime: {} };

// Prevent WebRTC from leaking the real local IP through ICE candidates.
const _rtc = window.RTCPeerConnection;
if (_rtc) {
    window.RTCPeerConnection = function(...args) {
        const pc = new _rtc(...args);
        const _create = pc.createOffer.bind(pc);
        pc.createOffer = (opts) => _create({ ...(opts || {}), offerToReceiveAudio: false, offerToReceiveVideo: false });
        return pc;
    };
}
"""


class FingerprintEngine:
    """Generates fingerprints and applies them to Playwright contexts."""

    def __init__(self, *, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def generate(self) -> Fingerprint:
        version = self._rng.choice(_CHROME_VERSIONS)
        ua_platform, navigator_platform = self._rng.choice(_PLATFORMS)
        viewport = self._rng.choice(_VIEWPORTS)
        locale = self._rng.choice(_LOCALES)
        timezone = self._rng.choice(_TIMEZONES)
        user_agent = (
            f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
        )
        return Fingerprint(
            user_agent=user_agent,
            platform=navigator_platform,
            viewport=viewport,
            locale=locale,
            timezone=timezone,
        )

    async def apply(self, context: BrowserContext, fingerprint: Fingerprint) -> None:
        """Inject the fingerprint into a Playwright BrowserContext."""
        script = _STEALTH_JS.replace("__LOCALE__", fingerprint.locale)
        await context.add_init_script(script)

    @staticmethod
    def context_options(fingerprint: Fingerprint) -> dict[str, object]:
        """Return kwargs you can pass directly to browser.new_context(...)."""
        return {
            "user_agent": fingerprint.user_agent,
            "viewport": fingerprint.viewport_dict,
            "locale": fingerprint.locale,
            "timezone_id": fingerprint.timezone,
            "color_scheme": fingerprint.color_scheme,
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
            "bypass_csp": False,
        }
