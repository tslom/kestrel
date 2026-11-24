"""The one JSON-over-HTTP call most providers make: parse JSON, turn an HTTPError into a
RuntimeError carrying the response body. Providers keep a thin `_request`/`_api` wrapper
(tests patch those names; Last.fm, Reddit, Plaid and News quirks stay in their modules).
Design: DESIGN.md → `app/providers/http.py`.
"""

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

# One retry: ~8% of health-poller cycles (~11 calls) hit a single blip; more only stretches a real outage.
RETRIES = 1
RETRY_BACKOFF = 0.5          # seconds before the retry
# 4xx (incl. 429) is an answer; repeating it changes nothing.
RETRY_STATUS = frozenset({500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def _transient(exc: BaseException) -> bool:
    """A stalled or dropped socket a retry can clear; not resolver/unreachable (offline.py's case)."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(
            exc, urllib.error.HTTPError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    # A stalled TLS handshake is a TimeoutError; a certificate failure is an SSLError, not retried.
    return isinstance(exc, (TimeoutError, ConnectionResetError,
                            ConnectionAbortedError, BrokenPipeError,
                            http.client.RemoteDisconnected,
                            http.client.IncompleteRead))


def json_request(url: str, *, label: str, token: str | None = None,
                 method: str = "GET", body: dict | None = None,
                 headers: dict | None = None, timeout: float = 30,
                 detail_limit: int | None = None,
                 idempotent: bool | None = None,
                 reauth: Callable[[], str] | None = None) -> dict:
    """One API call -> parsed JSON, `{}` for an empty body. `token` becomes a Bearer header; `body` is JSON.

    HTTPError -> `RuntimeError("{label} {code}: {body}")` (body cut to `detail_limit`); network
    failures pass through untouched for offline.py.

    `idempotent` calls retry RETRIES times on a transient failure or 5xx. Default True for
    GET/HEAD only: a POST retries only if the caller says it is a read (Health rollUp).

    `reauth` (with `token`) is called once on a 401 and the call re-sent, any method (a 401
    never landed). Google sometimes rejects a token its expiry still vouches for."""
    if idempotent is None:
        idempotent = method.upper() in IDEMPOTENT_METHODS
    attempts = 1 + (RETRIES if idempotent else 0)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    attempt = 0
    while attempt < attempts:
        last = attempt == attempts - 1
        attempt += 1
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and reauth is not None and token is not None:
                e.close()
                hdrs["Authorization"] = f"Bearer {reauth()}"
                reauth = None       # once: a second 401 is a real answer
                attempt -= 1        # the re-sent call is not a retry
                continue
            if not last and e.code in RETRY_STATUS:
                e.close()
                time.sleep(RETRY_BACKOFF)
                continue
            detail = e.read().decode(errors="replace")[:detail_limit]
            raise RuntimeError(f"{label} {e.code}: {detail}") from None
        except Exception as e:  # re-raised unless transient
            if last or not _transient(e):
                raise
            time.sleep(RETRY_BACKOFF)
    raise AssertionError("unreachable")  # pragma: no cover
