"""The one JSON-over-HTTP call most providers make.

Calendar, Gmail, Health, Weather, Spotify and Last.fm each carried their own
copy of the same dozen lines: build a Request, urlopen it, parse the JSON,
and turn an HTTPError into a RuntimeError that carries the response body —
which is where Google and friends put the reason. Only the label and the
timeout differed, so those are parameters and the rest lives here.

Each provider keeps its own `_request`/`_api` as a thin wrapper over this:
tests monkeypatch those names, and they are where a provider's quirks sit
(Last.fm's errors-in-a-200-body, Reddit's retry, Plaid's PlaidError and
News's redirect-following stay in their own modules because they don't fit
this shape).
"""

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable

# One retry for a transient failure on an idempotent call. Measured on the
# health poller (2026-10): ~8% of cycles died on a single read timeout,
# connection reset or 503 out of ~11 calls — each a blip the next attempt
# clears. One retry turns that into a rare double blip; more would only
# stretch a real outage (each attempt can sit out the full timeout).
RETRIES = 1
RETRY_BACKOFF = 0.5          # seconds before the retry
# Server-side hiccups worth asking again. 4xx (incl. 429) is an answer about
# the request or the quota, and repeating it straight away changes nothing.
RETRY_STATUS = frozenset({500, 502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def _transient(exc: BaseException) -> bool:
    """A failure a second attempt can plausibly clear: the socket stalled or
    was dropped mid-exchange. Not a resolver failure or an unreachable
    network — those are "offline", the next attempt half a second later meets
    the same dead wifi, and offline.py already serves the cache for them."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(
            exc, urllib.error.HTTPError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    # A stalled TLS handshake arrives as TimeoutError('_ssl.c:…: The handshake
    # operation timed out'); a certificate failure is an SSLError and is not
    # retried.
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
    """One API call -> parsed JSON, `{}` for an empty body (a DELETE answers
    204). `token` becomes a Bearer header; `body` is sent as JSON.

    An HTTPError is re-raised as `RuntimeError("{label} {code}: {body}")`,
    the response body truncated to `detail_limit` chars when given. Network
    failures (URLError and friends) pass through untouched — offline.py
    tells those apart from an answer.

    `idempotent` calls get RETRIES more attempts after a transient failure
    (read timeout, reset/dropped connection, 5xx); the last failure is what
    raises. Defaults to True for GET/HEAD only — a POST is retried only when
    its caller says it is a read in POST's clothing (Google Health's rollUp),
    because re-sending a POST that did land would do it twice.

    `reauth`, given with a `token`, is called once on a 401 for a fresh token
    and the call is re-sent — whatever the method, since a 401 means it never
    landed. Google rejects an access token its own expiry still vouches for
    now and then (24 single-cycle 401s across calendar, Gmail and Health in
    three weeks, each gone the next cycle); a forced refresh clears it."""
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
