"""
Pytest setup for the auto-publish repo.

`autopublish_app.py` imports `modal` and creates `modal.Dict` handles at module
import time. CI runners do not have a Modal token, so we stub the parts of the
`modal` API that get touched during import. This lets us test the pure helper
functions (`_parse_caption`, `_infer_retry_keys_natural`, `_schedule_iso_central`,
etc.) without standing up a real Modal app.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BLOTATO_API_KEY", "test-key-not-real")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("GROQ_API_KEY", "test-key-not-real")
os.environ.setdefault("TAVILY_API_KEY", "test-key-not-real")
os.environ.setdefault("API_AUTH_TOKEN", "test-token-not-real")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_IDS", "0")
os.environ.setdefault(
    "ACCOUNTS_JSON",
    '{"te":{"ig":{"account_id":"1"},"yt":{"account_id":"2"},'
    '"fb":{"account_id":"3","page_id":"p3"}},'
    '"en":{"ig":{"account_id":"4"},"yt":{"account_id":"5"},'
    '"fb":{"account_id":"6","page_id":"p6"},"x":{"account_id":"7"}}}',
)


def _install_modal_stub() -> None:
    if "modal" in sys.modules:
        return

    modal = types.ModuleType("modal")

    class _Dict(dict):
        @classmethod
        def from_name(cls, _name: str, create_if_missing: bool = False) -> "_Dict":
            return cls()

    class _Image:
        """Chainable builder: every method returns self so .pip_install().apt_install()... all work."""

        @staticmethod
        def debian_slim(*_a, **_kw) -> "_Image":
            return _Image()

        @staticmethod
        def from_registry(*_a, **_kw) -> "_Image":
            return _Image()

        def __getattr__(self, _name):
            def chainable(*_a, **_kw):
                return self

            return chainable

    class _Secret:
        @classmethod
        def from_name(cls, _name: str) -> "_Secret":
            return cls()

    class _App:
        def __init__(self, _name: str = "test") -> None:
            self.name = _name

        def function(self, *_a, **_kw):
            def deco(fn):
                return fn

            return deco

        def web_endpoint(self, *_a, **_kw):
            def deco(fn):
                return fn

            return deco

        def cls(self, *_a, **_kw):
            def deco(klass):
                return klass

            return deco

    modal.App = _App
    modal.Dict = _Dict
    modal.Image = _Image
    modal.Secret = _Secret
    modal.web_endpoint = lambda *_a, **_kw: (lambda fn: fn)
    modal.fastapi_endpoint = lambda *_a, **_kw: (lambda fn: fn)
    modal.asgi_app = lambda *_a, **_kw: (lambda fn: fn)
    modal.method = lambda *_a, **_kw: (lambda fn: fn)
    modal.enter = lambda *_a, **_kw: (lambda fn: fn)
    modal.exit = lambda *_a, **_kw: (lambda fn: fn)

    sys.modules["modal"] = modal


_install_modal_stub()
