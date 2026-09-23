"""
Central configuration.

Import this module FIRST in every entry point. Loading .env happens here, at
import time, before any other module can read os.environ — that ordering is the
whole point of this file.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent

load_dotenv(BASE_DIR / ".env")

# State files live next to the code by default. Point STATE_DIR at a mounted
# disk (or any persistent path) to survive redeploys on an ephemeral host.
STATE_DIR = Path(os.environ.get("STATE_DIR", str(BASE_DIR)))
STATE_DIR.mkdir(parents=True, exist_ok=True)

_config_cache = None


def load_config():
    """Read config.yaml once and keep it in memory."""
    global _config_cache
    if _config_cache is None:
        with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
            _config_cache = yaml.safe_load(f) or {}
    return _config_cache


def mailboxes():
    return load_config().get("mailboxes", [])


def settings():
    return load_config().get("settings", {})


def setting(key, default=None):
    return settings().get(key, default)


def state_path(filename):
    return STATE_DIR / filename


def env(name, default=""):
    return os.environ.get(name, default)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in .env locally, or in the host's environment settings when deployed."
        )
    return value


def mailbox_password(mailbox):
    """The password for one mailbox, or '' if it isn't configured."""
    return os.environ.get(mailbox.get("password_env", ""), "")


def configured_mailboxes():
    """(usable, skipped) — mailboxes that have a password vs. those that don't."""
    usable, skipped = [], []
    for mb in mailboxes():
        (usable if mailbox_password(mb) else skipped).append(mb)
    return usable, skipped
