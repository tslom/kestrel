import { useState } from "react";
import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh.js";

// Settings → Phone: the phone door (backend app/phone.py). Answer a waiting
// card from the phone, over your own tailnet. Off until switched on here;
// on, the backend opens a second listener on 127.0.0.1:8791 that Tailscale
// Serve is pointed at. The main app is never exposed.
//
// Three things have to be true, and each is a row that says so: Tailscale is
// up, Serve points at the door, and a phone is paired. The Serve command is
// yours to run: this app doesn't change Tailscale's configuration.

const when = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "never");
const EVENT = { paired: "paired", allowed: "allowed", denied: "denied", pushed: "notified", revoked: "removed",
  subscribed: "notifications on", "push-failed": "notification failed", "pair-voided": "pairing code voided",
  "switched-on": "switched on", "switched-off": "switched off" };

function Step({ done, children }) {
  return (
    <li className={done ? "done" : ""}>
      <span className="prov-step-n" aria-hidden="true">{done ? "✓" : "·"}</span><span>{children}</span>
    </li>
  );
}

export default function PhoneAccess() {
  const { data, setData, error, setError } = useWidgetData("/api/phone");
  const [login, setLogin] = useState(null);       // null: showing what's stored or suggested
  const [pairing, setPairing] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pushNote, setPushNote] = useState(null);
  if (!data) return <p className="set-note">{error || "Loading…"}</p>;

  const run = (p) => { setBusy(true); return p.then((d) => { setError(null); return d; })
    .catch((e) => { setError(e.message); return null; }).finally(() => setBusy(false)); };
  const save = (body) => run(postJSON("/api/phone", body, "PUT")).then((d) => d && setData(d));
  const recheck = () => run(fetchJSON("/api/phone")).then((d) => d && setData(d));
  const pair = () => run(postJSON("/api/phone/pair", {})).then((d) => d && setPairing(d));
  const remove = (id) => run(fetchJSON(`/api/phone/devices/${id}`, { method: "DELETE" })).then(recheck);
  const testPush = () => run(postJSON("/api/phone/test-push", {})).then((r) => r && setPushNote(
    r.sent ? `Sent to ${r.sent} phone${r.sent === 1 ? "" : "s"}.` : `Not sent: ${r.errors?.[0] || "no phone has notifications on"}`));

  const ts = data.tailscale;
  const typed = login ?? data.owner_login ?? data.suggested_login ?? "";
  const loginSaved = !!data.owner_login && typed.trim().toLowerCase() === data.owner_login;
  const anyPush = data.devices.some((d) => d.push);
  return (
    <div className="phone-access">
      <p className="set-note">
        Allow or deny a waiting card from your phone, with a notification when one waits while you're away.
        It runs over your own Tailscale network to a separate small door that can see open cards and answer
        them, and nothing else: no files, no database, no console. With the Mac asleep the phone says it
        can't reach it, and nothing is queued.
      </p>
      {error && <p className="set-note bad" role="alert">{error}</p>}

      <div className="set-row">
        <div className="set-what"><span>Your Tailscale login</span>
          <small>The door only answers requests Tailscale stamps with this login</small></div>
        <div className="set-how">
          <input className="set-input phone-login" value={typed} placeholder="you@example.com" aria-label="Tailscale login"
            onChange={(e) => setLogin(e.target.value)} />
          <button className="pbtn" disabled={busy || !typed.trim() || loginSaved}
            onClick={() => save({ owner_login: typed }).then(() => setLogin(null))}>{loginSaved ? "Saved" : "Save"}</button>
        </div>
      </div>
      <div className="set-row">
        <div className="set-what"><span>Phone access</span>
          <small>{data.enabled
            ? (data.listening ? `Door open on 127.0.0.1:${data.port}` : `Door not open: ${data.bind_error || "starting"}`)
            : "Off: the door's port isn't open"}</small></div>
        <div className="set-how">
          <div className="set-seg" role="radiogroup" aria-label="Phone access">
            {[[true, "On"], [false, "Off"]].map(([value, label]) => (
              <button key={label} role="radio" aria-checked={data.enabled === value}
                disabled={busy || (value && !data.owner_login)} className={data.enabled === value ? "on" : ""}
                onClick={() => { if (data.enabled !== value) save({ enabled: value }); }}>{label}</button>
            ))}
          </div>
        </div>
      </div>

      {data.enabled && (
        <>
          <ol className="prov-steps phone-steps">
            <Step done={ts.online}>
              {!ts.installed ? "Install Tailscale on this Mac and on the phone, signed in to the same account."
                : ts.online ? `Tailscale is up${ts.name ? ` (${ts.name})` : ""}.`
                  : "Tailscale is installed but not connected. Open it and connect, on this Mac and on the phone."}
            </Step>
            <Step done={ts.served}>
              {ts.served ? "Tailscale Serve points at the door." : <>Point Tailscale Serve at the door (once, in a terminal;
                it asks you to enable HTTPS for the tailnet the first time): <code>{data.serve_command}</code></>}
            </Step>
            <Step done={data.devices.length > 0}>
              {data.devices.length ? "A phone is paired." : "Pair the phone: open the address below on it, or scan the code."}
            </Step>
            <Step done={anyPush}>
              {anyPush ? "Notifications are on." : "On the phone: Share → Add to Home Screen, open Kestrel from there, pair, then Turn on notifications."}
            </Step>
          </ol>
          <div className="set-row">
            <div className="set-what"><span>Pair a phone</span>
              <small>{data.url || "The address appears once Tailscale is connected"}</small></div>
            <div className="set-how">
              <button className="pbtn" disabled={busy} onClick={recheck}>Check again</button>
              <button className="pbtn go" disabled={busy} onClick={pair}>{pairing ? "New code" : "Pair a phone"}</button>
            </div>
          </div>
          {pairing && (
            <div className="phone-pair">
              {pairing.qr && <img className="phone-qr" alt="QR code for pairing"
                src={`data:image/svg+xml;utf8,${encodeURIComponent(pairing.qr)}`} />}
              <div>
                <div className="phone-code">{pairing.code}</div>
                <p className="set-note">Good for {Math.round(pairing.expires_in / 60)} minutes, once.
                  {pairing.qr ? " Scan it with the phone's camera, or type the code on the phone's Kestrel page." : " Type it on the phone's Kestrel page."}
                  {" "}The Home Screen app keeps its own sign-in, so pair again there with a new code.</p>
                <button className="pbtn" onClick={() => { setPairing(null); recheck(); }}>Done</button>
              </div>
            </div>
          )}
          {data.devices.map((d) => (
            <div className="set-row phone-device" key={d.id}>
              <div className="set-what"><span>{d.name}</span>
                <small>Paired {when(d.created_at)} · last seen {when(d.last_seen_at)} · notifications {d.push ? "on" : "off"}</small></div>
              <div className="set-how"><button className="pbtn" disabled={busy} onClick={() => remove(d.id)}>Remove</button></div>
            </div>
          ))}
          {anyPush && (
            <div className="set-row">
              <div className="set-what"><span>Test notification</span>{pushNote && <small>{pushNote}</small>}</div>
              <div className="set-how"><button className="pbtn" disabled={busy} onClick={testPush}>Send</button></div>
            </div>
          )}
          {data.events.length > 0 && (
            <p className="set-note phone-events">
              Lately: {data.events.slice(0, 5).map((e) => `${EVENT[e.kind] || e.kind} ${when(e.at)}`).join(" · ")}
            </p>
          )}
        </>
      )}
    </div>
  );
}
