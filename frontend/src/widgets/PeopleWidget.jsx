import { fetchJSON, postJSON } from "../lib/api";
import { useWidgetData } from "../lib/refresh";
import { NEEDS, QUIET } from "../lib/salience";
import { OWED, age } from "../lib/asks.js";
import WidgetError from "../ui/WidgetError.jsx";
import RefreshButton from "../ui/RefreshButton.jsx";
import Icon from "../ui/icons.jsx";
import { dragPin } from "../lib/drag.js";

// Compact People/CRM-lite card (ROUND34_PLAN.md §17): waiting-on threads +
// upcoming birthdays/watch-due at a glance. The search + per-person history
// view lives in PeopleExpand.jsx, opened via the expand icon.
export function usePeopleCard() {
  const { data, setData, error, setError, reload, busy: refreshing } = useWidgetData(() =>
    Promise.all([fetchJSON("/api/people/waiting"), fetchJSON("/api/people/signals")])
      .then(([w, s]) => ({ waiting: w.data, signals: s })), "people");
  const waiting = data?.waiting ?? null;
  const signals = data?.signals ?? null;

  // Optimistic (low-stakes, high-churn); undo lives in the expansion's "show N dismissed" list.
  const dismiss = (w) => {
    setData((d) => ({ ...d, waiting: d.waiting.filter((x) => x !== w) }));
    postJSON("/api/people/waiting/dismiss",
      { kind: w.kind, key: w.key, through: w.last_at })
      .catch((e) => { setError(e.message); reload(); });
  };

  const loaded = waiting !== null && signals !== null;
  const attention = loaded && (waiting.length > 0 || signals.birthdays.length > 0);

  // The same rule the dot already used: a reply owed or a birthday coming is
  // the only reason this card is on the desk.
  const level = !loaded ? null : attention ? NEEDS : QUIET;
  const summary = !attention ? "everyone's answered"
    : [waiting?.length && `${waiting.length} waiting on a reply`,
       signals?.birthdays.length && `${signals.birthdays.length} birthday${signals.birthdays.length > 1 ? "s" : ""}`]
      .filter(Boolean).join(" · ");

  // One row for replies owed (freshest named), one per birthday.
  const freshest = loaded && waiting.length
    ? waiting.reduce((a, b) => (b.days < a.days ? b : a)) : null;
  const asks = !loaded ? [] : [
    ...(freshest ? [{
      id: "people", card: "people", tier: OWED, kind: "",
      title: `${waiting.length} waiting on a reply`,
      why: `${freshest.to}${waiting.length > 1 ? ` and ${waiting.length - 1} more` : ""}`
        + ` · waiting ${age(freshest.last_at)}`,
      acts: [{ label: "Open", sheet: "people" }],
    }] : []),
    ...signals.birthdays.map((b) => ({
      id: `birthday:${b.name}`, card: "people", tier: OWED, kind: "",
      title: `${b.name}'s birthday ${b.days_until === 0 ? "is today" : `in ${b.days_until} d`}`,
      why: "", acts: [{ label: "Open", sheet: "people" }],
    })),
  ];

  return { waiting, signals, error, reload, refreshing, dismiss,
           loaded, attention, level, summary, asks };
}

export default function PeopleWidget({ card, onPin }) {
  const { waiting, signals, error, reload, refreshing, dismiss, loaded, attention } = card;

  // /waiting is longest-silence-first (the full view's order); the card leads with the freshest, still actionable today.
  const peek = loaded ? waiting.slice().sort((a, b) => a.days - b.days) : [];
  const birthdays = loaded ? signals.birthdays : [];

  const pinData = loaded && {
    source: "people", waiting: waiting.length,
    birthdays: signals.birthdays, watch_due: signals.watch_due,
    top_waiting: waiting.slice(0, 10).map(({ to, days, kind }) => ({ to, days, kind })),
  };

  return (
    <section className="widget" {...dragPin("people · signals", pinData)}>
      <header>
        <h2>People <span className="widget-sub">crm-lite</span></h2>
        <div className="widget-actions">
          <RefreshButton onClick={reload} busy={refreshing} />
          <button className="pin" disabled={!loaded}
            onClick={() => onPin("people · signals", pinData)}
            title="Attach waiting/birthdays to your next console message"><Icon name="pin" /> pin</button>
        </div>
      </header>
      {error && <WidgetError error={error} />}
      {!loaded && !error && <><div className="skel" /><div className="skel w60" /></>}
      {loaded && (
        <div className="people-summary">
          {signals.birthdays.length > 0 && (
            <ul className="people-birthdays" {...dragPin("birthdays", signals.birthdays)}>
              {birthdays.map((b) => (
                <li key={b.name} {...dragPin(`birthday · ${b.name}`, b)}>
                  <Icon name="cake" size={12} /> <span className="people-name">{b.name}</span>
                  <span className="people-days">
                    {b.days_until === 0 ? "today" : `in ${b.days_until}d`}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {waiting.length === 0
            ? <p className="widget-empty">No stale threads — everyone's answered.</p>
            : <p className="people-count"><strong>{waiting.length}</strong> waiting on reply</p>}
          {waiting.length > 0 && (
            <ul className="people-peek" {...dragPin("waiting · the freshest", peek)}>
              {peek.map((w) => (
                <li key={`${w.kind}-${w.key}-${w.last_at}`}
                  {...dragPin(`waiting · ${w.to || w.key}`, w)}>
                  <span className="people-name">{w.to}</span>
                  <span className="people-days">{w.days}d</span>
                  {/* Dismiss is daily so it lives on the card; mute is permanent and stays in the expansion. */}
                  {w.key && (
                    <button className="people-dismiss" onClick={() => dismiss(w)}
                      title="Nothing to respond to — dismiss. They come back if they write again.">
                      <Icon name="check" size={12} />
                    </button>
                  )}
                  {/* Only messages carry a preview; email rows are threads you sent last. */}
                  {w.preview && <p className="people-preview">{w.preview}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
