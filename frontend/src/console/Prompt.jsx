// The two cards that stop a turn to ask: tool approval and AskUserQuestion. Both copy the terminal's
// prompt (↑/↓, a digit jumps to a row, enter takes it, esc backs out): a keyboard answer beats hunting a scrolling transcript.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ShellCode } from "../lib/shell.jsx";
import { toolLabel } from "../plan/StatusBar.jsx";

// Free text is the escape hatch on every prompt (an unlisted answer, or a deny with instructions), in one bottom row.
const OTHER = "__other__";

// The card owns the keyboard while up. Capture phase: the composer's onKeyDown would eat digits and enter first.
function useCardKeys(active, handler) {
  const held = useRef(handler);
  held.current = handler;
  useEffect(() => {
    if (!active) return;
    const onKey = (e) => {
      // Someone who clicks into the composer means to write there: swallowing their digits/enter would strand a half-typed message.
      const t = e.target;
      if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement
          || t?.isContentEditable) return;
      held.current(e);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [active]);
}

// The card takes focus on appearing (the composer goes quiet; the answer is one keystroke away).
function useClaimFocus(active, ref) {
  useLayoutEffect(() => {
    if (active) ref.current?.focus({ preventScroll: true });
  }, [active, ref]);
}

function digit(e) {
  if (e.key.length !== 1) return -1;
  const n = e.key.charCodeAt(0) - 49;   // "1" -> 0
  return n >= 0 && n <= 8 ? n : -1;
}

function Choices({ options, cursor, onCursor, isOn, multi, onPick }) {
  return (
    <div className="choices" role="listbox" aria-multiselectable={multi || undefined}>
      {options.map((o, i) => {
        const on = isOn(o, i);
        return (
          <button
            key={o.key ?? o.label ?? i}
            type="button"
            role="option"
            aria-selected={on}
            className={`choice${i === cursor ? " at" : ""}${on ? " on" : ""}`}
            onMouseEnter={() => onCursor(i)}
            onClick={() => onPick(i)}
          >
            <span className="choice-cursor">{i === cursor ? "❯" : ""}</span>
            <span className="choice-num">{i + 1}.</span>
            {multi && <span className="choice-box">{on ? "◉" : "○"}</span>}
            <span className="choice-label">{o.label}</span>
            {o.description && <span className="choice-desc">{o.description}</span>}
            {o.hint && <span className="choice-hint">{o.hint}</span>}
          </button>
        );
      })}
    </div>
  );
}

// One free-text row for "Other" and the deny reason: Enter commits, escape goes back.
function FreeText({ label, value, onChange, onCommit, onCancel, placeholder }) {
  const ref = useRef(null);
  // Caret at the end: the box can open holding the letter that opened it.
  useLayoutEffect(() => {
    const el = ref.current;
    el?.focus({ preventScroll: true });
    el?.setSelectionRange(el.value.length, el.value.length);
  }, []);
  return (
    <div className="choice-other">
      <label>{label}</label>
      <input
        ref={ref}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); onCommit(); }
          if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel(); }
        }}
      />
    </div>
  );
}

/** AskUserQuestion: one question on screen at a time, like the terminal. */
export function AskCard({ entry, active, onAnswer }) {
  const questions = entry.questions || [];
  const [qi, setQi] = useState(0);
  const [answers, setAnswers] = useState({});
  const [cursor, setCursor] = useState(0);
  const [multiPick, setMultiPick] = useState([]);
  const [other, setOther] = useState(null);   // null = closed, string = typing
  const cardRef = useRef(null);
  useClaimFocus(active && entry.resolved === null, cardRef);

  const q = questions[qi];
  const multi = !!q?.multiSelect;
  const rows = [...(q?.options || []),
                { key: OTHER, label: "Other", description: "type your own answer" }];

  function commit(answer) {
    const next = { ...answers, [q.question]: answer };
    setAnswers(next);
    setOther(null);
    setMultiPick([]);
    setCursor(0);
    if (qi + 1 < questions.length) setQi(qi + 1);
    else onAnswer(entry.requestId, next);
  }

  function pick(i) {
    const row = rows[i];
    if (row.key === OTHER) { setCursor(i); setOther(""); return; }
    if (multi) {
      setMultiPick((p) => p.includes(row.label)
        ? p.filter((l) => l !== row.label) : [...p, row.label]);
      setCursor(i);
      return;
    }
    commit(row.label);
  }

  useCardKeys(active && entry.resolved === null && other === null, (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const stop = () => { e.preventDefault(); e.stopPropagation(); };
    if (e.key === "ArrowDown") { stop(); setCursor((c) => (c + 1) % rows.length); return; }
    if (e.key === "ArrowUp") { stop(); setCursor((c) => (c - 1 + rows.length) % rows.length); return; }
    if (e.key === "Escape") { stop(); onAnswer(entry.requestId, null); return; }
    if (e.key === " " && multi) { stop(); pick(cursor); return; }
    if (e.key === "Enter") {
      stop();
      // Multi-select enter with nothing picked commits the row under the cursor outright (pick() only
      // toggles in multi mode, so a second enter looked like the first did nothing); "Other" still uses pick().
      if (multi && multiPick.length > 0) commit(multiPick);
      else if (multi && rows[cursor].key !== OTHER) commit([rows[cursor].label]);
      else pick(cursor);
      return;
    }
    const d = digit(e);
    if (d >= 0 && d < rows.length) { stop(); pick(d); return; }
    // Any other letter is the start of "no, do this instead": the box opens
    // holding it, and Enter declines with what's typed (#99).
    if (e.key.length === 1 && e.key !== " " && !(d >= 0)) {
      stop();
      setCursor(rows.length - 1);
      setReason(e.key);
    }
  });

  if (entry.resolved !== null) {
    const picked = entry.resolved;
    return (
      <div className="ask-card settled">
        <div className="ask-head">answered</div>
        {!picked
          ? <div className="perm-settled">dismissed — Claude was told you didn't answer</div>
          : Object.entries(picked).map(([k, v]) => (
              <div key={k} className="ask-settled-row">
                <span className="ask-settled-q">{k}</span>
                <span className="ask-settled-a">
                  {Array.isArray(v) ? v.join(", ") : v}
                </span>
              </div>
            ))}
      </div>
    );
  }

  if (!q) return null;
  return (
    <div className="ask-card" ref={cardRef} tabIndex={-1}>
      <div className="ask-head">
        <span className="ask-tag">{q.header || "question"}</span>
        {questions.length > 1 && (
          <span className="ask-step">{qi + 1} of {questions.length}</span>
        )}
      </div>
      <div className="ask-q">{q.question}</div>
      <Choices
        options={rows}
        cursor={cursor}
        onCursor={setCursor}
        multi={multi}
        isOn={(row) => multi && multiPick.includes(row.label)}
        onPick={pick}
      />
      {other !== null && (
        <FreeText
          label="your answer"
          value={other}
          onChange={setOther}
          placeholder="anything the options don't cover"
          onCommit={() => { if (other.trim()) commit(other.trim()); }}
          onCancel={() => setOther(null)}
        />
      )}
      <div className="prompt-keys">
        {multi ? "space toggles · enter confirms" : "enter or 1-9 picks"} · ↑↓ moves · esc skips
      </div>
    </div>
  );
}

// One call as the thing it acts on (backend console_preview.py resolves ids): title, when, label/value
// rows, body, one warning. Every field optional and absent renders nothing (a "where —" row would
// claim no location when the cache just lacked one).
// A command folds past this many lines (a 60-line heredoc pushed the answer rows off screen).
const CMD_FOLD_LINES = 8;
const CMD_FOLDED_LINES = 5;

// A shell command: Claude's sentence, then the highlighted command, folded when long.
function CommandBlock({ lead, command }) {
  const lines = command.split("\n");
  const long = lines.length > CMD_FOLD_LINES;
  const [all, setAll] = useState(false);
  const shown = long && !all ? lines.slice(0, CMD_FOLDED_LINES).join("\n") : command;
  return (
    <>
      {lead && <div className="perm-call-lead">{lead}</div>}
      <pre className={`perm-call-cmd${long && !all ? " folded" : ""}`}><ShellCode code={shown} /></pre>
      {long && (
        <button type="button" className="perm-cmd-fold" tabIndex={-1} onClick={() => setAll((v) => !v)}>
          {all ? "fold" : `show all ${lines.length} lines`}
        </button>
      )}
    </>
  );
}

function CallPreview({ call }) {
  if (!call) return null;
  const rows = call.rows || [];
  return (
    <div className="perm-call">
      {call.title && call.mono
        ? <CommandBlock lead={call.lead} command={call.title} />
        : call.title && <div className="perm-call-title">{call.title}</div>}
      {call.when && <div className="perm-call-when">{call.when}</div>}
      {/* The id resolved to nothing: said so, or the card looks thin for no reason. */}
      {call.unresolved && <div className="perm-call-miss">{call.unresolved}</div>}
      {rows.length > 0 && (
        <dl className="perm-call-rows">
          {rows.map(([label, value], i) => (
            <div key={i} className="perm-call-row">
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}
      {call.note && <div className="perm-call-note">{call.note}</div>}
      {call.warn && <div className="perm-call-warn">{call.warn}</div>}
    </div>
  );
}

/** Tool approval, with the terminal's three answers. */
export function PermCard({ entry, active, onAnswer }) {
  const isPlan = entry.tool === "ExitPlanMode";
  const [raw, setRaw] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [reason, setReason] = useState(null);   // null = closed, string = typing
  const cardRef = useRef(null);
  useClaimFocus(active && entry.resolved === null, cardRef);

  const rows = [
    { key: "yes", label: isPlan ? "Yes, start executing" : "Yes" },
    // No "don't ask again" for plans (a standing yes to unread plans): the backend withholds the label, so the row goes.
    ...(entry.alwaysLabel
      ? [{ key: "always", label: "Yes, and don't ask again",
           // a Chrome card's label already says "until this turn ends"
           description: `for ${entry.alwaysLabel}${entry.alwaysScope === "turn" ? "" : ", this session"}` }]
      : []),
    { key: "no",
      label: isPlan ? "No, keep planning" : "No, and tell Claude what to do differently",
      hint: "esc" },
  ];

  function pick(i) {
    const row = rows[i];
    if (row.key === "no") { setCursor(i); setReason(""); return; }
    onAnswer(entry.requestId, { allow: true, always: row.key === "always" });
  }

  useCardKeys(active && entry.resolved === null && reason === null, (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const stop = () => { e.preventDefault(); e.stopPropagation(); };
    if (e.key === "ArrowDown") { stop(); setCursor((c) => (c + 1) % rows.length); return; }
    if (e.key === "ArrowUp") { stop(); setCursor((c) => (c - 1 + rows.length) % rows.length); return; }
    if (e.key === "Escape") { stop(); pick(rows.length - 1); return; }
    if (e.key === "Enter") { stop(); pick(cursor); return; }
    // Raw arguments by key; a letter so it can't collide with the digit answer rows.
    if (e.key === "r" && entry.preview?.length) { stop(); setRaw((v) => !v); return; }
    const d = digit(e);
    if (d >= 0 && d < rows.length) { stop(); pick(d); return; }
    // Any other letter is the start of "no, do this instead": the box opens
    // holding it, and Enter declines with what's typed (#99).
    if (e.key.length === 1 && e.key !== " " && !(d >= 0)) {
      stop();
      setCursor(rows.length - 1);
      setReason(e.key);
    }
  });

  if (entry.resolved !== null) {
    return (
      <div className="perm-card settled">
        <div className="perm-head">
          {isPlan ? "Plan ready — start executing?" : `Allow ${toolLabel(entry.tool)}?`}
        </div>
        <div className="perm-settled">
          {entry.resolved
            ? (entry.always ? `allowed — and won't ask again this ${entry.alwaysScope === "turn" ? "turn" : "session"}` : "allowed")
            : (entry.reason ? `denied — "${entry.reason}"` : "denied")}
        </div>
      </div>
    );
  }

  // The backend folds a turn's parallel calls to one tool into one card (console.py `bursts`); the answer applies to all inputs.
  const calls = entry.inputs?.length ? entry.inputs : [entry.input];

  return (
    <div className="perm-card" ref={cardRef} tabIndex={-1}>
      <div className="perm-head">
        {isPlan
          ? "Plan ready — start executing?"
          : calls.length > 1
            ? `Allow ${calls.length} ${toolLabel(entry.tool)} calls?`
            : `Allow ${toolLabel(entry.tool)}?`}
      </div>
      {/* Only auto mode sets this, for calls it won't make itself (else the card is inexplicable). */}
      {entry.guard && (
        <div className="perm-guard">
          paused — it would {entry.guard}
        </div>
      )}
      {/* Every mode, unlike the guard: a delete and a rename must not look alike. */}
      {!entry.guard && entry.danger && (
        <div className="perm-danger">this will {entry.danger}</div>
      )}
      {/* Claude's own sentence from just before asking, quoted: evidence for the user to weigh. */}
      {entry.why && !isPlan && <div className="perm-why">{entry.why}</div>}
      {isPlan && entry.plan
        ? <pre className="perm-plan">{entry.plan}</pre>
        : entry.preview?.length && !raw
          ? <div className="perm-calls">
              {entry.preview.map((call, i) => <CallPreview key={i} call={call} />)}
            </div>
          : <pre className="perm-input">
              {JSON.stringify(calls.length > 1 ? calls : entry.input, null, 2)}
            </pre>}
      {/* The raw arguments stay one click away: a preview can be wrong (unfamiliar tool, id resolved to the wrong row). */}
      {!isPlan && entry.preview?.length > 0 && (
        <button type="button" className="perm-raw-toggle" tabIndex={-1}
                onClick={() => setRaw((r) => !r)}>
          {raw ? "hide arguments" : "show arguments"}
        </button>
      )}
      <Choices
        options={rows}
        cursor={cursor}
        onCursor={setCursor}
        multi={false}
        isOn={() => false}
        onPick={pick}
      />
      {reason !== null && (
        <FreeText
          label={isPlan ? "what to change" : "what to do instead"}
          value={reason}
          onChange={setReason}
          placeholder="optional — goes back to Claude as the reason"
          onCommit={() => onAnswer(entry.requestId,
                                   { allow: false, reason: reason.trim() })}
          onCancel={() => setReason(null)}
        />
      )}
      <div className="prompt-keys">
        enter or 1-9 picks · ↑↓ moves · esc denies · type to decline and redirect
        {entry.preview?.length ? " · r shows the arguments" : ""}
      </div>
    </div>
  );
}
