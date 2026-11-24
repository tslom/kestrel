import { useCallback, useEffect, useState } from "react";
import { useDebounce, useTimeout } from "../lib/timers.js";
import { fetchJSON, postJSON } from "../lib/api";
import { useTopic } from "../lib/refresh";
import { capRows, pinFiles } from "../lib/pin.js";
import Files from "../ui/Files.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// People / CRM-lite slide-over: search, waiting-on threads, birthdays/watch-due, one person's merged
// history, edits via PATCH /api/people/{person_key}. Texts/emails/events only, no vault notes.
// Design: DESIGN.md → `frontend/src/widgets/PeopleExpand.jsx`.

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { year: "2-digit", month: "short", day: "numeric" });
}

// A newer message is a different row and deserves a new draft.
const rowId = (w) => `${w.kind}-${w.key}-${w.last_at}`;

// One waiting row as console context, shared by the pin button and the drag. chat_guid / thread_id
// ride along so the console can db_query what the pinned message was replying to.
const waitingPin = (w) => ({
  label: `waiting · ${w.to || w.key}`,
  data: {
    source: "people", kind: "waiting",
    person_key: w.key, who: w.to, via: w.kind,
    ...(w.chat_guid && { chat_guid: w.chat_guid }),
    ...(w.thread_id && { thread_id: w.thread_id }),
    ...(w.subject && { subject: w.subject }),
    waiting_since: w.last_at, days_waiting: w.days,
    their_latest_message: w.preview,
  },
});

function EditForm({ person, personKey, onSaved }) {
  const [name, setName] = useState(person.name || "");
  const [birthday, setBirthday] = useState(person.birthday || "");
  const [cadence, setCadence] = useState(person.watch_cadence_days ?? "");
  const [notes, setNotes] = useState(person.notes || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const dirty = name !== (person.name || "")
    || birthday !== (person.birthday || "")
    || String(cadence) !== String(person.watch_cadence_days ?? "")
    || notes !== (person.notes || "");

  const save = () => {
    setSaving(true);
    postJSON(`/api/people/${encodeURIComponent(personKey)}`, {
      name: name.trim() || null,
      birthday: birthday.trim() || null,
      watch_cadence_days: cadence === "" ? null : Number(cadence),
      notes: notes.trim() || null,
    }, "PATCH")
      .then(onSaved)
      .catch((e) => setError(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <div className="people-edit">
      {error && <p className="widget-error">{error}</p>}
      <div className="people-edit-row">
        <label>name</label>
        <input value={name} onChange={(e) => setName(e.target.value)}
          placeholder={personKey} />
      </div>
      <div className="people-edit-row">
        <label>birthday</label>
        <input value={birthday} onChange={(e) => setBirthday(e.target.value)}
          placeholder="MM-DD" />
      </div>
      <div className="people-edit-row">
        <label>watch every</label>
        <input type="number" min="0" value={cadence}
          onChange={(e) => setCadence(e.target.value)} placeholder="days" />
      </div>
      <div className="people-edit-row people-edit-notes">
        <label>notes</label>
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
      </div>
      <button className="form-save-btn" disabled={!dirty || saving} onClick={save}>
        {saving ? "saving…" : "save"}
      </button>
    </div>
  );
}

function PersonDetail({ personKey, onPin, onBack }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    fetchJSON(`/api/people/${encodeURIComponent(personKey)}`)
      .then((d) => { setDetail(d); setError(null); })
      .catch((e) => setError(e.message));
  }, [personKey]);

  useEffect(() => { setDetail(null); load(); }, [load]);

  if (error) return <p className="widget-error">{error}</p>;
  if (!detail) return <p className="widget-empty">Loading…</p>;

  const { person, emails, messages, meetings, stats, message_handle } = detail;

  // Backend-side `open`, not an imessage:// link: webviews treat non-http schemes differently.
  const openMessages = () =>
    fetchJSON(`/api/people/${encodeURIComponent(personKey)}/open-messages`,
      { method: "POST" }).catch((e) => setError(e.message));
  const personLabel = `person · ${person.name || personKey}`;
  const personData = {
    source: "people", person, stats,
    recent_emails: emails.slice(0, 5).map(({ subject, received_at, is_sent }) =>
      ({ subject, received_at, is_sent })),
    recent_messages: messages.slice(0, 5).map(({ text, ts, is_from_me, attachments }) =>
      ({ text, ts, is_from_me, files: pinFiles(attachments) })),
    recent_meetings: meetings.slice(0, 5).map(({ summary, start_at }) => ({ summary, start_at })),
  };
  const pin = () => onPin(personLabel, personData);

  return (
    <div className="people-detail">
      {/* Only the head drags: a draggable panel would break text selection in the history. */}
      <div className="people-detail-head" {...dragPin(personLabel, personData)}>
        <button className="widget-footer-btn people-back" onClick={onBack}><Icon name="arrowLeft" size={11} /> all people</button>
        <button className="pin-mini people-pin" onClick={pin}
          title="Attach this person's history to your next console message"><Icon name="pin" /></button>
      </div>
      <h3 className="people-detail-name">{person.name || personKey}</h3>
      {message_handle && (
        <button className="people-open-messages" onClick={openMessages}
          title={`Open the ${message_handle} thread in Messages`}>
          <Icon name="message" /> Open in Messages
        </button>
      )}
      {/* Keyed on what it copies: a save resets the form, a no-change refetch leaves an edit alone. */}
      <EditForm key={[personKey, person.name, person.birthday, person.watch_cadence_days, person.notes].join("\u0000")}
        person={person} personKey={personKey} onSaved={(p) => setDetail((d) => ({ ...d, person: p }))} />
      <dl className="people-stats">
        <div><dt>emails</dt><dd>{stats.email_count}</dd></div>
        <div><dt>messages</dt><dd>{stats.message_count}</dd></div>
        <div><dt>meetings</dt><dd>{stats.meeting_count}</dd></div>
        <div><dt>last contact</dt><dd>{fmtWhen(stats.last_contact_at)}</dd></div>
      </dl>

      {emails.length > 0 && (
        <>
          <h4 className="people-section">emails</h4>
          <ul className="people-history"
            {...dragPin(`${person.name || personKey} · emails`, capRows(emails.slice(0, 20)))}>
            {emails.slice(0, 20).map((e) => (
              <li key={e.msg_id} {...dragPin(`email · ${e.subject || "(no subject)"}`, e)}>
                <span className="people-hist-when">{fmtWhen(e.received_at)}</span>
                <span className="people-hist-dir"><Icon name={e.is_sent ? "arrowRight" : "arrowLeft"} size={11} /></span>
                <span className="people-hist-text">{e.subject || "(no subject)"}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {messages.length > 0 && (
        <>
          <h4 className="people-section">messages</h4>
          <ul className="people-history"
            {...dragPin(`${person.name || personKey} · messages`, capRows(messages.slice(0, 20)))}>
            {messages.slice(0, 20).map((m) => (
              <li key={m.guid} {...dragPin(`message · ${fmtWhen(m.ts)}`, m)}>
                <span className="people-hist-when">{fmtWhen(m.ts)}</span>
                <span className="people-hist-dir"><Icon name={m.is_from_me ? "arrowRight" : "arrowLeft"} size={11} /></span>
                <span className="people-hist-text">
                  {m.is_group && m.conversation && (
                    <span className="people-convo-tag"
                      title={`group chat: ${m.conversation}`}>{m.conversation}</span>
                  )}
                  {m.text || "(attachment)"}
                  <Files items={m.attachments} />
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
      {meetings.length > 0 && (
        <>
          <h4 className="people-section">meetings</h4>
          <ul className="people-history"
            {...dragPin(`${person.name || personKey} · meetings`, meetings.slice(0, 20))}>
            {meetings.slice(0, 20).map((ev) => (
              <li key={ev.event_id} {...dragPin(`meeting · ${ev.summary}`, ev)}>
                <span className="people-hist-when">{fmtWhen(ev.start_at)}</span>
                <span className="people-hist-text">{ev.summary}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {emails.length === 0 && messages.length === 0 && meetings.length === 0 && (
        <p className="widget-empty">No emails, messages, or matching calendar events yet.</p>
      )}
    </div>
  );
}

// One group chat's recent messages, with per-message sender (the whole point
// of a group view — you can see who said what). 1:1 chats route to
// PersonDetail instead, so this only ever renders groups.
function ConversationDetail({ chatGuid, onPin, onBack }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  // Keyed on chatGuid by its caller, so another chat starts empty.
  useEffect(() => {
    fetchJSON(`/api/people/conversation?chat_guid=${encodeURIComponent(chatGuid)}`)
      .then((d) => { setDetail(d); setError(null); })
      .catch((e) => setError(e.message));
  }, [chatGuid]);

  if (error) return <p className="widget-error">{error}</p>;
  if (!detail) return <p className="widget-empty">Loading…</p>;

  const { conversation: convo, messages } = detail;
  const label = convo?.label || "conversation";
  const convoLabel = `conversation · ${label}`;
  const convoData = {
    source: "people", conversation: convo,
    recent_messages: messages.slice(0, 12).map(
      ({ text, ts, is_from_me, contact_name, handle, attachments }) => ({
        text, ts, is_from_me,
        from: is_from_me ? "me" : (contact_name || handle),
        files: pinFiles(attachments),
      })),
  };
  const pin = () => onPin(convoLabel, convoData);

  return (
    <div className="people-detail">
      <div className="people-detail-head" {...dragPin(convoLabel, convoData)}>
        <button className="widget-footer-btn people-back" onClick={onBack}><Icon name="arrowLeft" size={11} /> conversations</button>
        <button className="pin-mini people-pin" onClick={pin}
          title="Attach this conversation to your next console message"><Icon name="pin" /></button>
      </div>
      <h3 className="people-detail-name">{label}</h3>
      {convo && (
        <p className="people-convo-members">
          {convo.participants.map((p) => p.name || p.handle).join(", ")}
        </p>
      )}
      {messages.length === 0
        ? <p className="widget-empty">No messages cached for this chat.</p>
        : (
          <ul className="people-history" {...dragPin(`${label} · messages`, capRows(messages))}>
            {messages.map((m) => (
              <li key={m.guid} {...dragPin(`message · ${fmtWhen(m.ts)}`, m)}>
                <span className="people-hist-when">{fmtWhen(m.ts)}</span>
                <span className="people-hist-dir"><Icon name={m.is_from_me ? "arrowRight" : "arrowLeft"} size={11} /></span>
                <span className="people-hist-text">
                  {!m.is_from_me && (
                    <span className="people-convo-sender">{m.contact_name || m.handle}: </span>
                  )}
                  {m.text || "(attachment)"}
                  <Files items={m.attachments} />
                </span>
              </li>
            ))}
          </ul>
        )}
    </div>
  );
}

function ConversationList({ conversations, onOpenGroup, onOpenPerson }) {
  if (!conversations) return <p className="widget-empty">Loading…</p>;
  if (conversations.length === 0) return <p className="widget-empty">No conversations cached yet.</p>;
  return (
    <ul className="people-history" {...dragPin("conversations", capRows(conversations))}>
      {conversations.map((c) => (
        <li key={c.chat_guid} className="people-row people-convo-row"
          {...dragPin(`conversation · ${c.label}`, c)}
          onClick={() => (c.is_group ? onOpenGroup(c.chat_guid) : onOpenPerson(c.person_key))}>
          <span className="people-convo-main">
            <span className="people-hist-text">
              {c.is_group && <span className="people-convo-badge" title="group chat">group</span>}
              {c.label}
            </span>
            {c.last_text && (
              <span className="people-convo-snippet">
                {c.last_from_me ? "You: " : (c.is_group && c.last_sender ? `${c.last_sender}: ` : "")}
                {c.last_text}
              </span>
            )}
          </span>
          <span className="people-hist-when">{fmtWhen(c.last_ts)}</span>
        </li>
      ))}
    </ul>
  );
}

export default function PeopleExpand({ onPin }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState(null);
  const [waiting, setWaiting] = useState(null);
  const [signals, setSignals] = useState(null);
  const [hidden, setHidden] = useState(null);
  const [showHidden, setShowHidden] = useState(false);
  const [dismissed, setDismissed] = useState(null);
  const [showDismissed, setShowDismissed] = useState(false);
  const [conversations, setConversations] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null); // person_key or null (list view)
  const [openChat, setOpenChat] = useState(null); // group chat_guid, or null
  const [view, setView] = useState("overview");   // overview | conversations
  const [drafts, setDrafts] = useState({});       // rowId -> {loading|drafts|error}
  const [remindState, setRemindState] = useState({}); // rowId -> saving|done|exists
  const [copied, setCopied] = useState(null);     // "rowId-index", briefly

  const load = useCallback(() => {
    Promise.all([
      fetchJSON("/api/people/waiting"),
      fetchJSON("/api/people/signals"),
      fetchJSON("/api/people/hidden"),
      fetchJSON("/api/people/conversations"),
      fetchJSON("/api/people/waiting/dismissed"),
    ])
      .then(([w, s, h, c, d]) => {
        setWaiting(w.data); setSignals(s); setHidden(h.data);
        setConversations(c.data); setDismissed(d.data); setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  const setPersonHidden = (key, val) =>
    postJSON(`/api/people/${encodeURIComponent(key)}`, { hidden: val }, "PATCH")
      .then(load).catch((e) => setError(e.message));

  // Dismiss ≠ mute: mute kills a sender forever; dismiss ends one thread (`through` = the row's last_at).
  const dismiss = (w) =>
    postJSON("/api/people/waiting/dismiss",
      { kind: w.kind, key: w.key, through: w.last_at })
      .then(load).catch((e) => setError(e.message));

  const undismiss = (d) =>
    fetchJSON(`/api/people/waiting/dismiss/${encodeURIComponent(d.kind)}/`
      + `${encodeURIComponent(d.key)}`, { method: "DELETE" })
      .then(load).catch((e) => setError(e.message));

  // "Not now": keeps the row (the reply is still owed) and adds a task to today.
  const remind = (w) => {
    const id = rowId(w);
    setRemindState((s) => ({ ...s, [id]: "saving" }));
    postJSON("/api/people/waiting/remind", {
      kind: w.kind, key: w.key, last_at: w.last_at,
      name: w.to, preview: w.preview, subject: w.subject,
    })
      .then((r) => setRemindState((s) => ({ ...s, [id]: r.created ? "done" : "exists" })))
      .catch((e) => { setRemindState((s) => ({ ...s, [id]: null })); setError(e.message); });
  };

  // One model call per press (seconds): the row shows "drafting…".
  const draft = (w) => {
    const id = rowId(w);
    if (drafts[id]) { setDrafts((d) => ({ ...d, [id]: null })); return; }  // toggle shut
    setDrafts((d) => ({ ...d, [id]: { loading: true } }));
    postJSON("/api/people/waiting/draft", {
      kind: w.kind, name: w.to, chat_guid: w.chat_guid, thread_id: w.thread_id,
    })
      .then((r) => setDrafts((d) => ({ ...d, [id]: { drafts: r.drafts, turns: r.turns } })))
      .catch((e) => setDrafts((d) => ({ ...d, [id]: { error: e.message } })));
  };

  const copiedFade = useTimeout();
  const copyDraft = (id, i, text) => {
    navigator.clipboard.writeText(text)
      .then(() => {
        setCopied(`${id}-${i}`);
        copiedFade.start(() => setCopied(null), 1200);
      })
      .catch((e) => setError(e.message));
  };

  useEffect(() => { load(); }, [load]);
  useTopic("people", load);

  // stale(): a slow "jo" must not land on top of "john"
  useDebounce((stale) => {
    if (!q.trim()) { setResults(null); return; }
    fetchJSON(`/api/people/search?q=${encodeURIComponent(q.trim())}`)
      .then((r) => { if (!stale()) setResults(r.data); })
      .catch((e) => { if (!stale()) setError(e.message); });
  }, q.trim() ? 200 : 0, [q]);

  return (
    <>
    <header className="expand-head">
      <span className="expand-spacer" />
      <div className="widget-actions">
      </div>
    </header>
    {error && <p className="widget-error">{error}</p>}

    {selected ? (
      <PersonDetail personKey={selected} onPin={onPin} onBack={() => setSelected(null)} />
    ) : openChat ? (
      <ConversationDetail key={openChat} chatGuid={openChat} onPin={onPin} onBack={() => setOpenChat(null)} />
    ) : (
      <>
        <div className="chip-row" style={{ marginBottom: 10 }}>
          {["overview", "conversations"].map((v) => (
            <button key={v} className={`chip ${view === v ? "active" : ""}`}
              onClick={() => setView(v)}>{v}</button>
          ))}
        </div>

        {view === "conversations" ? (
          <ConversationList conversations={conversations}
            onOpenGroup={setOpenChat}
            onOpenPerson={(key) => key && setSelected(key)} />
        ) : (
          <>
        <input className="people-search" value={q} autoFocus
          placeholder="search by name…"
          onChange={(e) => setQ(e.target.value)} />

        {results && (
          <ul className="people-history" {...dragPin(`people · “${q.trim()}”`, results)}>
            {results.length === 0 && <p className="widget-empty">No matches.</p>}
            {results.map((p) => (
              <li key={p.person_key} className="people-row"
                {...dragPin(`person · ${p.name}`, p)}
                onClick={() => setSelected(p.person_key)}>
                <span className="people-hist-text">{p.name}</span>
                <span className="people-hist-when">{fmtWhen(p.last_msg_at || p.last_email_at)}</span>
              </li>
            ))}
          </ul>
        )}

        {!q.trim() && signals && signals.birthdays.length > 0 && (
          <>
            <h4 className="people-section">birthdays (next 7d)</h4>
            <ul className="people-history" {...dragPin("birthdays", signals.birthdays)}>
              {signals.birthdays.map((b) => (
                <li key={b.name} {...dragPin(`birthday · ${b.name}`, b)}>
                  <span className="people-hist-text"><Icon name="cake" size={12} /> {b.name}</span>
                  <span className="people-hist-when">
                    {b.days_until === 0 ? "today" : `in ${b.days_until}d`}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}

        {!q.trim() && signals && signals.watch_due.length > 0 && (
          <>
            <h4 className="people-section">watch due</h4>
            <ul className="people-history" {...dragPin("watch due", signals.watch_due)}>
              {signals.watch_due.map((w) => (
                <li key={w.name} {...dragPin(`watch due · ${w.name}`, w)}>
                  <span className="people-hist-text">{w.name}</span>
                  <span className="people-hist-when">{w.days_since}d since contact</span>
                </li>
              ))}
            </ul>
          </>
        )}

        {!q.trim() && (
          <>
            <h4 className="people-section">waiting on reply</h4>
            {!waiting && <p className="widget-empty">Loading…</p>}
            {waiting && waiting.length === 0 && (
              <p className="widget-empty">Nothing stale — everyone's answered.</p>
            )}
            {waiting && waiting.length > 0 && (
              <ul className="people-history" {...dragPin("waiting on reply", waiting)}>
                {waiting.map((w) => {
                  const id = rowId(w);
                  const d = drafts[id];
                  const rem = remindState[id];
                  const pin = waitingPin(w);
                  return (
                  <li key={id}
                    className={w.key ? "people-row" : "people-row-static"}
                    {...dragPin(pin.label, pin.data)}
                    onClick={w.key ? () => setSelected(w.key) : undefined}>
                    <span className="people-hist-text">
                      {w.to || w.subject || "(unknown)"}
                      {/* the message you owe a reply to */}
                      {w.preview && <em className="people-preview">{w.preview}</em>}
                    </span>
                    <span className="people-hist-when">{w.days}d · {w.kind}</span>
                    {w.key && (
                      <>
                        {/* Fourth verb: take it to the console when the answer needs thought. */}
                        <button className="people-mute tip"
                          aria-label="Pin to console"
                          data-tip="Pin — attach this thread to your next console message"
                          onClick={(e) => {
                            e.stopPropagation();
                            onPin(pin.label, pin.data);
                          }}><Icon name="pin" /></button>
                        <button className="people-mute tip"
                          aria-label="Draft a reply"
                          data-tip="Draft a reply — suggestions to copy, nothing is sent"
                          onClick={(e) => { e.stopPropagation(); draft(w); }}>
                          {d?.loading ? "…" : <Icon name="edit" />}
                        </button>
                        <button className="people-mute tip"
                          aria-label={rem === "exists" ? "Already on today's list" : "Remind me today"}
                          data-tip={rem === "exists"
                            ? "Already on today's list"
                            : "Not now — put it on today's task list"}
                          onClick={(e) => { e.stopPropagation(); remind(w); }}>
                          <Icon name={rem === "done" || rem === "exists" ? "listCheck" : "clock"} />
                        </button>
                        <button className="people-mute tip"
                          aria-label="Dismiss this thread"
                          data-tip="Nothing to respond to — dismiss this thread. They come back if they write again"
                          onClick={(e) => { e.stopPropagation(); dismiss(w); }}>
                          <Icon name="check" />
                        </button>
                        <button className="people-mute tip"
                          aria-label="Mute this person"
                          data-tip="Mute — a bot or someone you don't care about. Hides everything they ever send"
                          onClick={(e) => { e.stopPropagation(); setPersonHidden(w.key, true); }}>
                          <Icon name="mute" />
                        </button>
                      </>
                    )}
                    {/* Click a draft to copy; nothing here sends (that stays in Messages/Gmail). */}
                    {d && !d.loading && (
                      <div className="people-drafts" onClick={(e) => e.stopPropagation()}>
                        {d.error && <p className="people-draft-note">{d.error}</p>}
                        {d.drafts?.length === 0 && (
                          <p className="people-draft-note">
                            No thread cached to draft from.
                          </p>
                        )}
                        {d.drafts?.map((text, i) => (
                          <button key={i} className="people-draft"
                            title="Copy to clipboard"
                            onClick={() => copyDraft(id, i, text)}>
                            <span>{text}</span>
                            <em>{copied === `${id}-${i}` ? "copied" : "copy"}</em>
                          </button>
                        ))}
                        {d.turns > 0 && (
                          <p className="people-draft-note">
                            from the last {d.turns} messages · nothing sent
                          </p>
                        )}
                      </div>
                    )}
                  </li>
                  );
                })}
              </ul>
            )}

            {dismissed && dismissed.length > 0 && (
              <>
                <button className="widget-footer-btn"
                  onClick={() => setShowDismissed((v) => !v)}>
                  {showDismissed ? "hide" : "show"} {dismissed.length} dismissed
                </button>
                {showDismissed && (
                  <ul className="people-history">
                    {dismissed.map((d) => (
                      <li key={`${d.kind}-${d.key}`} className="people-row-static">
                        <span className="people-hist-text people-muted"
                          title={d.name === d.key ? undefined : d.key}>
                          {d.name || d.key}
                        </span>
                        <span className="people-hist-when">{d.kind}</span>
                        <button className="people-mute tip" aria-label="Bring this thread back"
                          data-tip="Bring this thread back"
                          onClick={() => undismiss(d)}><Icon name="undo" /></button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}

            {hidden && hidden.length > 0 && (
              <>
                <button className="widget-footer-btn"
                  onClick={() => setShowHidden((v) => !v)}>
                  {showHidden ? "hide" : "show"} {hidden.length} muted
                </button>
                {showHidden && (
                  <ul className="people-history">
                    {hidden.map((h) => (
                      <li key={h.key} className="people-row-static">
                        <span className="people-hist-text people-muted"
                          title={h.name === h.key ? undefined : h.key}>
                          {h.name}
                        </span>
                        <button className="people-mute tip" aria-label="Unmute"
                          data-tip="Unmute — start showing this person again"
                          onClick={() => setPersonHidden(h.key, false)}><Icon name="undo" /></button>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </>
        )}
          </>
        )}
      </>
    )}
    </>
  );
}
