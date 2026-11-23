# Phone door (Settings → Phone, and the phone's own page on :8791)

## Sub-features
- Settings → Phone: your Tailscale login, the On/Off switch (off = the door's port isn't open), four steps that tick as they become true (Tailscale up, Serve pointed at the door, a phone paired, notifications on), Pair a phone (QR + an 8-digit code, 10 minutes, once), the paired devices with Remove, a test notification, the last few events.
- The phone's page (served by the door, not by the desk): pair by code or by the QR's link, the cards that are waiting with Allow / Deny, "Turn on notifications", "Unpair this phone", and the unreachable state (last cards shown with buttons off, "Mac unreachable since …").
- A card answered on the phone settles at the desk too (a `permission_answered` frame on the tab's socket).
- The push: a card that has waited 20s with nobody at the desk sends one notification per card, naming the tab only.

## How to get to it (user POV)
- `⌘K Settings`, then the Phone section.
- The phone: `https://<machine>.<tailnet>.ts.net:8443`, which Tailscale Serve proxies to `127.0.0.1:8791`. Nothing else reaches the door in real use.

## Driving it with Playwright
- Settings: `.phone-access`, `input.phone-login`, the switch is `.set-seg` with aria-label "Phone access", `.phone-steps li` (`.done`), `.phone-pair`, `img.phone-qr`, `.phone-code`, `.phone-device`. `GET/PUT /api/phone`, `POST /api/phone/pair`, `DELETE /api/phone/devices/{id}`, `POST /api/phone/test-push`.
- The door's page is plain HTML in `backend/app/phone_page/` (not in frontend/src, so its ids aren't checked here): #pair, #pair-code, #card-list, .card, .card-btns, #unreachable, #notify, #state.
- Use a scratch backend and scratch data, with the door on a spare port: `DASHBOARD_DATA_DIR=/tmp/x BACKEND_PORT=8799 PHONE_DOOR_PORT=8795 … uvicorn app.main:app --port 8799 --lifespan off`, then `PUT /api/phone {"owner_login": "owner@example.com", "enabled": true}`.
- Every request to the door needs `Tailscale-User-Login`. Playwright's `extraHTTPHeaders` covers page requests but **not** the service worker's own fetches, so for anything touching the worker (offline shell, push) put the stand-in for Serve in front: `python backend/scripts/phone_serve_sim.py http://127.0.0.1:8795 owner@example.com 8796` and browse `http://127.0.0.1:8796`.
- A real card: open `ws://127.0.0.1:8799/ws/console`, send `{"type":"start","model":"haiku","permission_mode":"default","profile":"general","tab":"t1","attach":false}` then a `user` message that needs a write tool (task_create), and wait for `permission_request`. Proof is the three together: the card in `GET /api/status` on the door, `permission_answered` + `tool_result` on the socket after Allow, and the row in the scratch app.db.
- A push arriving: Chrome over CDP, `ServiceWorker.deliverPushMessage`, then `registration.getNotifications()`.
- pytest holds the rest: `tests/test_phone.py` (allowlist, guard, pairing, stale ids, push timing), `tests/test_webpush.py` (RFC 8291's example).

## Gotchas
- Never point the rig's door at the real app.db with the switch on: `enabled` is a row in app.db, and the real backend would open the real door on its next start.
- The real-card journey runs a real Haiku turn and leaves a session in the vault's session list.
- Headless browsers can't subscribe to push ("permission denied"), and no local rig reaches Apple's push service. Real delivery is only provable on the phone: Settings → Phone → Test notification.
- Switching off in Settings closes the port; a page already open on the phone then reads "Unreachable", which is also how a sleeping Mac looks.
- `tailscale serve` is the user's command to run. Don't run it, or `serve reset`, from a session.
