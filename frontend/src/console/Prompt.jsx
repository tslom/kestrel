// The two cards that stop a turn to ask the user something: tool approval and
// AskUserQuestion. Both copy the terminal's prompt, because that UX is already
// in everyone's fingers — ↑/↓ moves, a digit jumps straight to a row, enter
// takes it, esc backs out — and because a keyboard answer beats hunting for a
// button in a transcript that is still scrolling.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ShellCode } from "../lib/shell.jsx";
import { toolLabel } from "../plan/StatusBar.jsx";

// Free text is the escape hatch on every prompt: the terminal lets you answer
// a question with something nobody listed, and deny a tool with a sentence
// about what to do instead. Both land in the same row at the bottom.
const OTHER = "__other__";

// The card owns the keyboard while it's up. Capture phase, because the
// composer's textarea has its own onKeyDown and would otherwise eat the digits
// and the enter before this ever ran.
function useCardKeys(active, handler) {
  const held = useRef(handler);
  held.current = handler;
  useEffect(() => {
    if (!active) return;
    const onKey = (e) => {
      // The card takes focus when it appears, so this is the default path.
      // Someone who clicks into the composer anyway means to write there —
      // swallowing their digits and their enter would strand them with a
      // half-typed message and no way to send it.
      const t = e.target;
      if (t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement
          || t?.isContentEditable) return;
      held.current(e);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [active]);
}

// Taking focus is what makes the card feel like the terminal's prompt rather
// than a widget floating in the log: the composer goes quiet, and the answer
// is one keystroke away without clicking first.
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

// One free-text row, shared by "Other" and by the deny reason. Enter commits,
// escape goes back to the list — the same two keys as everywhere else.
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
      // In a multi-select, enter is "done with this question" — but an empty
      // answer is worse than no answer, so it takes the row under the cursor
      // instead of committing nothing. That has to commit it outright: routing
      // through pick() only *toggles* in multi mode, so the question sat there
      // needing a second enter that looked like the first one did nothing.
      // "Other" still goes through pick(), which opens the free-text row.
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

// One call, as the thing it acts on rather than as its arguments. The backend
// resolved the ids (console_preview.py); this is the shape it hands over —
// title, when, label/value rows, a body, and the one warning that changes the
// answer. Every field is optional, and an absent one renders nothing: a row
// reading "where —" next to a delete button says the event has no location
// when it may only mean the cache didn't have one.
// Past this many lines a command folds to its first few, with the rest a
// click away: a 60-line heredoc pushed the answer rows off the screen.
const CMD_FOLD_LINES = 8;
const CMD_FOLDED_LINES = 5;

// A shell command on the card: Claude's sentence for it first, then the
// command, highlighted, folded when long.
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
      {/* The id resolved to nothing. Said out loud, because the alternative is
          a card that looks thin for no stated reason. */}
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
    // A plan is approved one plan at a time; "don't ask again" would be a
    // standing yes to plans nobody has read yet, so the backend withholds the
    // label and the row disappears with it.
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
    // The raw arguments, without reaching for the mouse. A letter rather than
    // a digit so it can't collide with the answer rows, which are the keys
    // this card exists to be answered with.
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

  // The backend folds one turn's parallel calls to the same tool into a
  // single card (see console.py, `bursts`), so the card has to be able to
  // show more than one input — the answer applies to all of them.
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
      {/* Only auto mode sets this, and only for the calls it won't make on
          its own. Without it the card is inexplicable: prompting was
          supposed to be off. */}
      {entry.guard && (
        <div className="perm-guard">
          paused — it would {entry.guard}
        </div>
      )}
      {/* Shown in every mode, unlike the guard line above it: "ask" mode was
          rendering a delete and a rename in the same neutral card, and the
          word that separates them was nowhere on screen. */}
      {!entry.guard && entry.danger && (
        <div className="perm-danger">this will {entry.danger}</div>
      )}
      {/* Claude's own sentence for why, taken from what it wrote immediately
          before asking. Quoted, not paraphrased — it is evidence about the
          call, and the user is being asked to weigh it. */}
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
      {/* The arguments are still one click away. The preview is a reading of
          them, and a reading can be wrong — an unfamiliar tool, an id that
          resolved to the wrong row — so the thing being approved stays
          reachable. */}
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
