"""Local macOS migration: copy Keychain credentials into a new repository.

No credentials or private configuration are printed or saved to disk.
The original collector is parsed as data, never imported or executed.
"""
import argparse
import ast
from datetime import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys


class MigrationError(Exception):
    pass


def invoke(args, *, value=None, failure="Command failed", timeout=120):
    env = dict(os.environ)
    env.pop("GH_DEBUG", None)
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run(args, input=value, text=True, capture_output=True,
                                env=env, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MigrationError(failure) from None
    if result.returncode:
        raise MigrationError(failure)
    return result.stdout


def parse_source(source):
    wanted = {"ACCOUNTS", "TIMEZONE", "KEYCHAIN_SERVICE",
              "GMAIL_USERNAME_SECRET", "GMAIL_PASSWORD_SECRET"}
    values = {}
    cutoff = None
    try:
        for node in ast.parse(source).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if target.id in wanted:
                values[target.id] = ast.literal_eval(node.value)
            elif target.id == "ACTIONABLE_CATEGORIES_START_AT":
                call = node.value
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "datetime"
                        and call.func.attr == "fromisoformat"
                        and len(call.args) == 1 and not call.keywords):
                    raise ValueError()
                cutoff = ast.literal_eval(call.args[0])
        if set(values) != wanted or not isinstance(cutoff, str):
            raise ValueError()
        if datetime.fromisoformat(cutoff).tzinfo is not None:
            raise ValueError()
        for name in wanted - {"ACCOUNTS"}:
            if not isinstance(values[name], str) or not values[name].strip():
                raise ValueError()
        accounts = values["ACCOUNTS"]
        if not isinstance(accounts, list) or not 1 <= len(accounts) <= 2:
            raise ValueError()
        config_accounts = []
        mapping = [("GMAIL_USERNAME", values["GMAIL_USERNAME_SECRET"]),
                   ("GMAIL_APP_PASSWORD", values["GMAIL_PASSWORD_SECRET"])]
        seen = set()
        for index, account in enumerate(accounts, 1):
            for field in ("key", "name", "subdomain", "username_secret", "password_secret"):
                if not isinstance(account[field], str) or not account[field].strip():
                    raise ValueError()
            if not re.fullmatch(r"[a-z0-9_-]+", account["key"]) or account["key"] in seen:
                raise ValueError()
            if not re.fullmatch(r"[A-Za-z0-9-]+", account["subdomain"]):
                raise ValueError()
            seen.add(account["key"])
            updated = {field: account[field] for field in ("key", "name", "subdomain")}
            for field, suffix in (("username_secret", "USERNAME"), ("password_secret", "PASSWORD")):
                new_name = f"EDUPAGE_ACCOUNT_{index}_{suffix}"
                mapping.append((new_name, account[field]))
                updated[field] = new_name
            config_accounts.append(updated)
        config = {"accounts": config_accounts, "timezone": values["TIMEZONE"],
                  "capture_start_at": cutoff, "require_existing_state": True}
        return config, mapping, values["KEYCHAIN_SERVICE"]
    except Exception:
        raise MigrationError("Unsupported source configuration; nothing was copied.") from None


def migrate(source_repo, target_repo):
    pattern = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    if (not re.fullmatch(pattern, source_repo) or not re.fullmatch(pattern, target_repo)
            or source_repo.lower() == target_repo.lower()
            or source_repo.split('/')[0].lower() != target_repo.split('/')[0].lower()):
        raise MigrationError("Use distinct source and target repositories owned by the same account.")
    for repo, private in ((source_repo, True), (target_repo, False)):
        metadata = json.loads(invoke(["gh", "api", f"repos/{repo}"],
                                    failure="Cannot access repository. Check GitHub CLI login."))
        if metadata.get("private") is not private:
            raise MigrationError("Expected a private source and a public destination; stopped.")
        if not metadata.get("permissions", {}).get("admin"):
            raise MigrationError("Repository administrator access is required.")
    source = invoke(["gh", "api", f"repos/{source_repo}/contents/edupage_sync_github.py",
                     "-H", "Accept: application/vnd.github.raw+json"],
                    failure="Cannot read the original collector configuration.")
    config, mapping, service = parse_source(source)
    secrets = {}
    print("Reading existing Keychain entries. macOS may ask you to Allow access.", flush=True)
    for new_name, old_name in mapping:
        value = invoke(["security", "find-generic-password", "-s", service,
                        "-a", old_name, "-w"],
                       failure=f"Cannot read Keychain entry for {new_name}. Nothing was uploaded.").strip()
        if not value:
            raise MigrationError(f"Empty Keychain entry for {new_name}. Nothing was uploaded.")
        secrets[new_name] = value
    secrets["EDUPAGE_CONFIG_JSON"] = json.dumps(config, ensure_ascii=False)
    # Disable before changing credentials. Never start a production run here.
    for name in ("EDUPAGE_ENABLED", "EDUPAGE_KEEPALIVE_ENABLED"):
        invoke(["gh", "variable", "set", name, "--repo", target_repo, "--body", "false"],
               failure="Cannot disable destination jobs; stopped before uploading secrets.")
    for name, value in secrets.items():
        invoke(["gh", "secret", "set", name, "--repo", target_repo], value=value,
               failure=f"Upload failed for {name}. Destination stays disabled; rerun to finish.")
        print(f"Saved {name}", flush=True)
    print("DONE: 7 secrets copied." if len(secrets) == 7 else f"DONE: {len(secrets)} secrets copied.")
    print("Destination jobs remain disabled. No sync or bootstrap was started.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Existing private OWNER/REPO")
    parser.add_argument("--target", required=True, help="New public OWNER/REPO")
    args = parser.parse_args()
    try:
        if platform.system() != "Darwin" or os.getenv("GITHUB_ACTIONS"):
            raise MigrationError("Run this on your own Mac with the existing Keychain.")
        if not shutil.which("gh"):
            raise MigrationError("GitHub CLI is not installed or is not on PATH.")
        migrate(args.source, args.target)
        return 0
    except MigrationError as error:
        print(f"STOP: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("STOP: Cancelled. Any completed uploads remain; rerun to finish.", file=sys.stderr)
    except Exception:
        print("STOP: Migration could not finish. Private error details were suppressed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
