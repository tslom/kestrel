"""Give Antigravity (`agy`) a home of its own, so Kestrel can run it sandboxed.

Usage (from backend/):
    .venv/bin/python scripts/antigravity_home.py          # set up, then sign in
    .venv/bin/python scripts/antigravity_home.py --check  # say what's there

Kestrel runs agy with HOME set to `registry.AGY_HOME`, whose own keychain holds
agy's token only. Copies an existing sign-in from the login keychain (secret via
`security -i` stdin, never argv), else starts agy there for a browser sign-in.
Design: DESIGN.md → `backend/scripts/antigravity_home.py`.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.delegates.registry import AGY_HOME, AGY_KEYCHAIN_ITEM, AGY_TOKEN_FILE

KEYCHAIN = "login.keychain-db"


def _security(*args: str, home: Path | None = None, stdin: str | None = None,
              check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **({"HOME": str(home)} if home else {})}
    return subprocess.run(["/usr/bin/security", *args], env=env, input=stdin,
                          capture_output=True, text=True, check=check)


def has_keychain() -> bool:
    return (AGY_HOME / "Library" / "Keychains" / KEYCHAIN).exists()


def signed_in() -> bool:
    """The keychain item, or the file agy falls back to (registry._agy_signed_in)."""
    if AGY_TOKEN_FILE.is_file() and AGY_TOKEN_FILE.stat().st_size:
        return True
    service, account = AGY_KEYCHAIN_ITEM
    return _security("find-generic-password", "-s", service, "-a", account,
                     home=AGY_HOME, check=False).returncode == 0


def make_keychain() -> None:
    (AGY_HOME / "Library" / "Keychains").mkdir(parents=True, exist_ok=True)
    AGY_HOME.chmod(0o700)
    if has_keychain():
        return
    # Empty password, no lock timeout: a locked keychain stalls a headless run on a dialog.
    _security("create-keychain", "-p", "", KEYCHAIN, home=AGY_HOME)
    _security("set-keychain-settings", KEYCHAIN, home=AGY_HOME)
    _security("list-keychains", "-d", "user", "-s", KEYCHAIN, home=AGY_HOME)
    _security("default-keychain", "-d", "user", "-s", KEYCHAIN, home=AGY_HOME)


def copy_sign_in() -> bool:
    service, account = AGY_KEYCHAIN_ITEM
    found = _security("find-generic-password", "-s", service, "-a", account, "-w", check=False)
    token = found.stdout.strip()
    if found.returncode or not token:
        return False
    if any(c in token for c in '"\\\n '):
        raise SystemExit("the stored sign-in isn't in the shape expected; sign in instead")
    _security("-i", home=AGY_HOME,
              stdin=f'add-generic-password -U -s {service} -a {account} -w "{token}"\n')
    return True


def main() -> None:
    if "--check" in sys.argv[1:]:
        print(f"home: {AGY_HOME} ({'has' if has_keychain() else 'no'} keychain)")
        print("signed in" if has_keychain() and signed_in() else "not signed in")
        return
    make_keychain()
    if signed_in():
        print(f"agy is already signed in under {AGY_HOME}")
    elif copy_sign_in():
        print(f"copied your agy sign-in into {AGY_HOME}'s own keychain")
    else:
        print("no agy sign-in to copy: starting agy so you can sign in (quit it when done)")
        subprocess.run(["agy"], env={**os.environ, "HOME": str(AGY_HOME)}, cwd=AGY_HOME)
        print("signed in" if signed_in() else "still not signed in")
    print("In Kestrel: Settings → Providers → Check again.")


if __name__ == "__main__":
    main()
