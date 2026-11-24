"""Which Claude account this machine is signed in to, and the way to change it.

One login, the CLI's, shared by the console, `agent.ask` and the terminal. Kestrel never
sees a credential: it runs `claude auth status` / `claude auth login` and reads named
fields. Several accounts are kept by claude_accounts.py; a sign-in here adds one.
Design: DESIGN.md → `app/claude_cli.py`, `app/claude_account.py`, `app/session_digest.py`: console CLI, login, session digests.
"""

import json
import logging
import re
import subprocess
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import claude_accounts
from .config import CONSOLE_CLI_PATH

log = logging.getLogger(__name__)
router = APIRouter()

LOGIN_LIMIT_S = 5 * 60
STATUS_TIMEOUT_S = 20
# Goes on an argv as `--email`, so nothing that could read as a flag.
_EMAIL = re.compile(r"[A-Za-z0-9._%+][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


class _Login:
    lock = threading.Lock()
    proc: subprocess.Popen | None = None
    state = "idle"              # idle | waiting | done | failed
    error: str | None = None


def _status() -> dict:
    """The CLI's login status, by named fields only."""
    try:
        run = subprocess.run([str(CONSOLE_CLI_PATH), "auth", "status"], text=True,
                             capture_output=True, timeout=STATUS_TIMEOUT_S)
        raw = json.loads(run.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"logged_in": False, "error": f"couldn't ask the Claude CLI: {e}"}
    return {"logged_in": bool(raw.get("loggedIn")), "method": raw.get("authMethod"),
            "email": raw.get("email"), "plan": raw.get("subscriptionType"),
            "expires_at": raw.get("loginExpiresAt")}


def _failure(output: str, code: int) -> str:
    """The CLI's own last word, minus the sign-in URL it prints on the way."""
    lines = [ln.strip() for ln in output.splitlines()
             if ln.strip() and "http" not in ln and "Opening browser" not in ln]
    return lines[-1][:300] if lines else f"claude auth login exited {code}"


def _watch(proc: subprocess.Popen) -> None:
    try:
        out, _ = proc.communicate(timeout=LOGIN_LIMIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        with _Login.lock:
            if _Login.proc is proc:
                _Login.proc, _Login.state = None, "failed"
                _Login.error = f"Nothing came back from the browser in {LOGIN_LIMIT_S // 60} minutes."
        return
    with _Login.lock:
        if _Login.proc is not proc:     # cancelled
            return
        _Login.proc = None
        if proc.returncode == 0:
            _Login.state, _Login.error = "done", None
        else:
            _Login.state, _Login.error = "failed", _failure(out or "", proc.returncode)
            return
    try:
        claude_accounts.after_login()
    except Exception:   # the sign-in worked; saving it is the next tick's job too
        log.exception("couldn't save the new login as an account")


def _login_state() -> dict:
    with _Login.lock:
        return {"state": _Login.state, "error": _Login.error}


@router.get("/api/console/account")
def get_account() -> dict:
    return {"account": _status(), "login": _login_state()}


class LoginIn(BaseModel):
    email: str | None = None    # fills in the sign-in page; it's only a hint


@router.post("/api/console/account/login")
def post_login(body: LoginIn) -> dict:
    email = (body.email or "").strip()
    if email and not _EMAIL.fullmatch(email):
        raise HTTPException(400, "That doesn't look like an email address.")
    if not CONSOLE_CLI_PATH.exists():
        raise HTTPException(404, f"no claude CLI at {CONSOLE_CLI_PATH}")
    argv = [str(CONSOLE_CLI_PATH), "auth", "login", *(["--email", email] if email else [])]
    # The sign-in overwrites the slot: stash the current account first so it stays switchable.
    try:
        claude_accounts.before_login()
    except claude_accounts.KeychainError as e:
        raise HTTPException(500, f"Couldn't save the current login before signing in: {e}")
    with _Login.lock:
        if _Login.proc is not None:
            raise HTTPException(409, "A sign-in is already waiting on the browser.")
        try:
            proc = subprocess.Popen(argv, text=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            raise HTTPException(500, f"couldn't run claude auth login: {e}")
        _Login.proc, _Login.state, _Login.error = proc, "waiting", None
    threading.Thread(target=_watch, args=(proc,), daemon=True).start()
    return _login_state()


@router.post("/api/console/account/login/cancel")
def post_login_cancel() -> dict:
    with _Login.lock:
        proc, _Login.proc = _Login.proc, None
        _Login.state, _Login.error = "idle", None
    if proc is not None:
        proc.terminate()
    return _login_state()
