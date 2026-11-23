import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";
import EmailReader from "../ui/EmailReader.jsx";
import { fetchJSON } from "../lib/api";
import RangePicker from "../ui/RangePicker.jsx";
import SheetHead from "../ui/SheetHead.jsx";
import Band from "../ui/Band.jsx";
import Icon from "../ui/icons.jsx";
import { Bars, RankBars, Stat, rankNote } from "../ui/charts.jsx";
import { useSheetData } from "../lib/sheet.js";
import { capRows } from "../lib/pin.js";
import { GMAIL_URL, pinEmail, dragEmail, shortLabel } from "../lib/emailPin.js";
import { useGoogleAddresses } from "../lib/googleLinks";
import { fmtAgo, fmtDay, fmtTime, shortDay } from "../lib/time";
import { dragPin } from "../lib/drag.js";

// Email sheet — what arrived, not what to do about it (IDEAS §1, §5).
//
// The review queue is the sheet that *acts*: it archives, bins and
// unsubscribes, one card at a time, and it only ever holds the inbox. This
// one is the other question — how much mail a week was, who sent it, and
// where a particular message went — over any range, with no buttons that
// change anything. Keeping them apart is why neither has to compromise: a
// list you're triaging wants one message at a time, and a list you're
// searching wants all of them at once.
const VIEWS = [null];

const TOP_SENDERS = 10;

// One message as a row: time (or date, in search results, which span months),
// subject that opens the reader, sender, where it's filed, Gmail, pin.
function MailRow({ m, onPin, when }) {
  const [reading, setReading] = useState(false);
  return (
    <Fragment>
      <li {...dragEmail(m)}>
        <span className="event-time">{when}</span>
        <button className="event-summary email-open"
          title={m.summary || "Read the message"}
          aria-haspopup="dialog" onClick={() => setReading(true)}>
          {m.unread ? <strong>{m.subject}</strong> : m.subject}
        </button>
        <span className="event-cal">{m.from}</span>
        {m.label && (
          <span className="mail-label" title={m.label}>{shortLabel(m.label)}</span>
        )}
        <a className="email-gmail" href={GMAIL_URL(m.id)} target="_blank"
          rel="noopener noreferrer" title="Open in Gmail">
          <Icon name="external" size={11} />
        </a>
        <button className="pin-mini" onClick={() => pinEmail(onPin, m)}
          title="Attach this email to your next console message"><Icon name="pin" /></button>
      </li>
      {reading && <EmailReader id={m.id} row={m} onClose={() => setReading(false)} />}
    </Fragment>
  );
}

// Finding a message again — the reason review files anything. Ranked over
// every cached message (subject, sender, summary, label, and the terms pulled
// out of each body), not the range above; Gmail's own search is one click
// further, and runs by itself when nothing local matches.
function MailSearch({ onPin }) {
  useGoogleAddresses();
  const [q, setQ] = useState("");
  const [label, setLabel] = useState("");
  const [labels, setLabels] = useState([]);
  const [res, setRes] = useState(null);
  const [error, setError] = useState(null);
  const seq = useRef(0);

  useEffect(() => {
    fetchJSON("/api/email/labels").then(setLabels).catch(() => setLabels([]));
  }, []);

  const run = useCallback((text, lab, gmail) => {
    const mine = ++seq.current;          // a slow answer to an old query loses
    const params = new URLSearchParams({ q: text });
    if (lab) params.set("label", lab);
    if (gmail) params.set("gmail", "true");
    fetchJSON(`/api/email/search?${params}`)
      .then((r) => { if (mine === seq.current) { setRes(r); setError(null); } })
      .catch((e) => { if (mine === seq.current) setError(e.message); });
  }, []);

  const asking = Boolean(q.trim() || label);
  useDebounce(() => {
    if (!asking) { seq.current++; setRes(null); setError(null); return; }
    run(q, label, false);
  }, asking ? 250 : 0, [q, label, run]);

  const active = q.trim() || label;
  return (
    <>
      <label className="mail-search">
        <Icon name="search" size={13} />
        <input value={q} placeholder="Find mail — a subject, sender, order #, course…"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") { setQ(""); setLabel(""); } }} />
        <select value={label} onChange={(e) => setLabel(e.target.value)}
          aria-label="Only this label">
          <option value="">all labels</option>
          {labels.map((l) => (
            <option key={l.name} value={l.name}>{shortLabel(l.name)}</option>
          ))}
        </select>
      </label>
      {active && error && <p className="widget-error">{error}</p>}
      {active && res && (
        <Band title="Found" note={`${res.count}${res.gmail ? " · incl. Gmail" : ""}`}
          pin={[`email search · ${q || shortLabel(label)}`, capRows(res.data)]}>
          {res.data.length === 0 && <p className="widget-empty">Nothing matches.</p>}
          <ul className="event-list">
            {res.data.map((m) => (
              <MailRow key={m.id} m={m} onPin={onPin} when={fmtDay(m.day)} />
            ))}
          </ul>
          {!res.gmail && q.trim() && !label && (
            <p className="mail-search-note">
              Not it? <button onClick={() => run(q, label, true)}>Search Gmail too</button>
              {" "}— reads full bodies, not just what's cached.
            </p>
          )}
        </Band>
      )}
    </>
  );
}

function MailDays({ messages, onPin }) {
  // Every Gmail href here carries an authuser=, which arrives from the backend
  // a beat after the first render — without this the whole list keeps the
  // account-less links it was built with.
  useGoogleAddresses();
  if (messages.length === 0) return <p className="widget-empty">No mail in this range.</p>;
  const byDay = new Map();
  for (const m of messages) {
    if (!byDay.has(m.day)) byDay.set(m.day, []);
    byDay.get(m.day).push(m);
  }
  return [...byDay.entries()].map(([day, msgs]) => (
    <section key={day} className="slice-day"
      {...dragPin(`email · ${fmtDay(day)}`, capRows(msgs))}>
      <h3>{fmtDay(day)}
        <span className="day-note">{msgs.length}
          {msgs.some((m) => m.unread) ? ` · ${msgs.filter((m) => m.unread).length} unread` : ""}
        </span>
      </h3>
      <ul className="event-list">
        {msgs.map((m) => (
          <MailRow key={m.id} m={m} onPin={onPin} when={fmtTime(m.received)} />
        ))}
      </ul>
    </section>
  ));
}

export default function EmailSheet({ onPin }) {
  const { ranges, range, pickRange, rows, refreshedAt, error, loading, payloads, reload }
    = useSheetData("email", VIEWS, "this-week");

  const mail = rows();
  const unread = mail.filter((m) => m.unread);
  const inbox = mail.filter((m) => m.in_inbox);

  // Mail arrives in one stream and is read in another, so a day's bar is its
  // messages split by whether they've been opened — the unread part is the
  // part that's still yours to deal with, and it belongs inside the day's
  // height rather than beside it.
  const byDay = new Map();
  for (const m of mail) {
    const cur = byDay.get(m.day) || { read: 0, unread: 0 };
    cur[m.unread ? "unread" : "read"] += 1;
    byDay.set(m.day, cur);
  }
  const dayBars = [...byDay.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, v]) => ({ key: day, label: shortDay(day), v }));

  const bySender = new Map();
  for (const m of mail) {
    const cur = bySender.get(m.from) || { from: m.from, count: 0, unread: 0 };
    cur.count += 1;
    if (m.unread) cur.unread += 1;
    bySender.set(m.from, cur);
  }
  const senders = [...bySender.values()].sort((a, b) => b.count - a.count);

  const days = byDay.size;

  const pinLabel = `email · ${range}`;
  const pinData = payloads && {
    range,
    messages: mail.length,
    unread: unread.length,
    in_inbox: inbox.length,
    senders: senders.slice(0, TOP_SENDERS).map((s) => ({ from: s.from, count: s.count })),
  };

  return (
    <>
      <SheetHead refreshedAt={fmtAgo(refreshedAt)} label={pinLabel}
        data={error ? null : pinData} onPin={onPin} onRefresh={reload} />

      <MailSearch onPin={onPin} />

      <RangePicker ranges={ranges}
        range={range} onPick={pickRange} />

      {error && <p className="widget-error">{error}</p>}
      {loading && !payloads && <p className="widget-empty">Loading…</p>}

      {payloads && !error && (
        <>
          <Band title="Mail" note={`${mail.length}`}
            pin={[`email · ${range}`, capRows(mail)]}>
            <div className="hero-row">
              <Stat value={mail.length.toLocaleString()} label="Messages"
                sub={days > 1 ? `${Math.round(mail.length / days)}/day` : ""} />
              <Stat value={unread.length.toLocaleString()} label="Unread"
                sub={mail.length ? `${Math.round((unread.length / mail.length) * 100)}%` : ""} />
              <Stat value={inbox.length.toLocaleString()} label="Still in inbox"
                sub={mail.length - inbox.length ? `${mail.length - inbox.length} filed` : ""} />
              <Stat value={senders.length.toLocaleString()} label="Senders" />
            </div>
            <Bars items={dayBars}
              series={[
                { key: "read", label: "read", cls: "seg-read" },
                { key: "unread", label: "unread", cls: "seg-unread" },
              ]}
              pin={(it) => [`email · ${it.label}`,
                capRows(mail.filter((m) => m.day === it.key))]}
              empty="No mail in this range." />
          </Band>

          <Band title="Senders" note={rankNote(TOP_SENDERS, senders.length)}>
            <RankBars rows={senders.slice(0, TOP_SENDERS).map((s) => ({
              key: s.from, label: s.from, value: s.count,
              title: s.unread ? `${s.from} · ${s.unread} unread` : s.from,
              data: { from: s.from, count: s.count, unread: s.unread },
            }))}
              pinLabel={(r) => `sender · ${r.label}`}
              empty="No mail in this range." />
          </Band>

          <Band title="Messages" note={`${mail.length}`}
            pin={[`messages · ${range}`, capRows(mail)]}>
            <MailDays messages={mail} onPin={onPin} />
          </Band>
        </>
      )}
    </>
  );
}
