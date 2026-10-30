"""
Run:
    pip install pywebview
    python gui_launcher.py
"""

import string
from collections import Counter
from pathlib import Path

import webview

from entropy_engine import EntropyCollector, EntropyDaemon, EntropyPool

DAEMON_INTERVAL = 10
HTML_FILE = Path(__file__).parent / "entropy_gui.html"


# ---------------------------------------------------------------------------
# Password engine - plain functions, no GUI dependency (unchanged from
# the customtkinter version, just relocated)
# ---------------------------------------------------------------------------

CHARSETS = {
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "digits": string.digits,
    "symbols": "!@#$%^&*()-_=+[]{}",
}


def build_charset(lower=True, upper=True, digits=True, symbols=True) -> str:
    charset = ""
    if lower:
        charset += CHARSETS["lower"]
    if upper:
        charset += CHARSETS["upper"]
    if digits:
        charset += CHARSETS["digits"]
    if symbols:
        charset += CHARSETS["symbols"]
    if not charset:
        raise ValueError("at least one character class must be enabled")
    return charset


def _reject_sample(raw: bytes, charset: str, limit: int):
    for b in raw:
        if b < limit:
            yield charset[b % len(charset)]


def generate_password(pool_get_bytes, length: int = 20, **charset_kwargs) -> str:
    charset = build_charset(**charset_kwargs)
    limit = 256 - (256 % len(charset))
    chars = []
    while len(chars) < length:
        raw = pool_get_bytes(max(length * 2, 16))
        chars.extend(_reject_sample(raw, charset, limit))
    return "".join(chars[:length])


def entropy_bits(length: int, charset_size: int) -> float:
    import math
    return length * math.log2(charset_size)


# ---------------------------------------------------------------------------
# js_api bridge - every method here is callable from JS as
# window.pywebview.api.<name>(...), returning a JSON-serializable dict.
# ---------------------------------------------------------------------------

class EntropyAPI:
    def __init__(self):
        self.daemon = None

    def toggle_daemon(self, length=20, lower=True, upper=True, digits=True, symbols=True):
        if self.daemon is None:
            self.daemon = EntropyDaemon(interval=DAEMON_INTERVAL)
            self.daemon.force_stir()  # the ONE stir for start - reused below, not repeated
            self.daemon.start()
            breakdown = self.daemon.get_last_breakdown()
            pool_preview = self.daemon.peek(8).hex()
            try:
                length = max(4, min(128, int(length)))
            except (TypeError, ValueError):
                length = 20
            charset_kwargs = dict(lower=bool(lower), upper=bool(upper), digits=bool(digits), symbols=bool(symbols))
            try:
                password = generate_password(self.daemon.get_bytes, length=length, **charset_kwargs)
                extra = {"password": password, "bits": round(entropy_bits(length, len(build_charset(**charset_kwargs))))}
            except ValueError:
                extra = {"error": "no_charset"}
            return {
                "running": True,
                "interval": DAEMON_INTERVAL,
                "pool_preview": pool_preview,
                "breakdown": breakdown,
                **extra,
            }
        self.daemon.stop()
        self.daemon = None
        return {"running": False}

    def poll(self):
        """Called on an interval by JS (plays the role verify_gui.py's
        _poll_daemon did) - non-mutating, safe to call as often as
        wanted. Never returns password material."""
        if self.daemon is None:
            return {"running": False}
        return {"running": True, "pool_preview": self.daemon.peek(8).hex()}

    def regenerate(self, length, lower, upper, digits, symbols):
        """Force a fresh stir and draw a new password from it. Called
        on Regenerate clicks and on any length/charset control change -
        JS decides when, same "only on explicit action" rule as the
        customtkinter version."""
        if self.daemon is None:
            return {"error": "not_running"}
        self.daemon.force_stir()
        breakdown = self.daemon.get_last_breakdown()
        pool_preview = self.daemon.peek(8).hex()
        try:
            length = max(4, min(128, int(length)))
        except (TypeError, ValueError):
            length = 20
        charset_kwargs = dict(lower=bool(lower), upper=bool(upper), digits=bool(digits), symbols=bool(symbols))
        try:
            password = generate_password(self.daemon.get_bytes, length=length, **charset_kwargs)
        except ValueError:
            return {"error": "no_charset", "breakdown": breakdown, "pool_preview": pool_preview}
        bits = entropy_bits(length, len(build_charset(**charset_kwargs)))
        return {
            "password": password,
            "bits": round(bits),
            "breakdown": breakdown,
            "pool_preview": pool_preview,
        }

    def run_checks(self):
        results = []

        def check(kind, ok, **data):
            results.append({"kind": kind, "ok": bool(ok), **data})

        c = EntropyCollector()
        for src in c.sources:
            data = src()
            check("source", isinstance(data, bytes) and len(data) > 0, name=src.__name__, len=len(data))

        a, b = c.collect(), c.collect()
        check("collector_nonempty", isinstance(a, bytes) and len(a) > 0, len=len(a))
        check("collector_differs", a != b)

        p = EntropyPool()
        check("pool_length", len(p.get_bytes(32)) == 32)
        check("pool_differs", p.get_bytes(32) != p.get_bytes(32))
        sample = p.get_bytes(4096)
        counts = Counter(sample)
        check("pool_distribution", max(counts.values()) / len(sample) < 0.05)
        return results


def main():
    api = EntropyAPI()
    webview.create_window(
        title="E.P.G. / Entropy Password Generator",
        url=HTML_FILE.as_uri(),
        js_api=api,
        width=640,
        height=760,
        min_size=(480, 560),
        background_color="#0a0c10",
    )
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        print("[entropy_gui] Edge WebView2 unavailable, trying default engine...")
        webview.start(debug=False)


if __name__ == "__main__":
    main()