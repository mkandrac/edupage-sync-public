"""Public console boundary: discard worker output, including dependency errors."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def execute(command, *, env, timeout=600):
    try:
        result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        return 0 if result.returncode == 0 else 1
    except Exception:
        return 1


def main():
    parser = argparse.ArgumentParser(description="EduPage private data collector")
    parser.add_argument("mode", choices=("sync", "keepalive", "bootstrap"), nargs="?", default="sync")
    parser.add_argument("--account", help="Private account key, only for local bootstrap")
    args = parser.parse_args()
    if args.mode == "bootstrap" and (os.getenv("GITHUB_ACTIONS") or not args.account):
        print("Bootstrap requires a local terminal and an account key.")
        return 1
    env = dict(os.environ)
    env.update(EDUPAGE_PRIVATE_WORKER="1", EDUPAGE_HTTP_DIAGNOSTICS="0", PYTHONFAULTHANDLER="0")
    command = [sys.executable, str(Path(__file__).with_name("edupage_sync_github.py"))]
    if args.mode == "keepalive":
        command.append("--keepalive")
    elif args.mode == "bootstrap":
        command += ["--bootstrap-session", args.account]
    print("EduPage job started. Personal data and worker output are not logged.", flush=True)
    code = execute(command, env=env)
    print("EduPage job completed." if code == 0 else
          "EduPage job failed. Check private RAW status or renew the session locally.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
