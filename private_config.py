"""Private deployment configuration: never commit a real configuration file."""
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def load_config():
    raw = os.environ.get("EDUPAGE_CONFIG_JSON")
    if not raw:
        raise ValueError("Missing private configuration")
    try:
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError()
        accounts = config["accounts"]
        if not isinstance(accounts, list) or not 1 <= len(accounts) <= 2:
            raise ValueError()
        keys = set()
        for account in accounts:
            for field in ("key", "name", "subdomain", "username_secret", "password_secret"):
                if not isinstance(account[field], str) or not account[field].strip():
                    raise ValueError()
            if not re.fullmatch(r"[a-z0-9_-]+", account["key"]):
                raise ValueError()
            if account["key"] in keys:
                raise ValueError()
            keys.add(account["key"])
            if not re.fullmatch(r"[a-zA-Z0-9-]+", account["subdomain"]):
                raise ValueError()
            for field, suffix in (("username_secret", "USERNAME"), ("password_secret", "PASSWORD")):
                if not re.fullmatch(r"EDUPAGE_ACCOUNT_[12]_" + suffix, account[field]):
                    raise ValueError()
        config.setdefault("timezone", "Europe/Bratislava")
        ZoneInfo(config["timezone"])
        start = datetime.fromisoformat(config["capture_start_at"])
        if start.tzinfo is not None:
            raise ValueError()
        if not isinstance(config.get("require_existing_state", True), bool):
            raise ValueError()
        return config
    except Exception:
        raise ValueError("Invalid private configuration") from None
