// A ```quiz / ```cards fence of JSON (format in console_prompt.py) made interactive.
// Results are drafted into the composer, not sent; a fence still streaming is a placeholder, not a parse error.
// Design: DESIGN.md → Citations — what a sentence was read off (Quizzes and flashcards).
import { useMemo, useState } from "react";
import Cite from "./Cite.jsx";
import DocPage from "./DocPage.jsx";
import MathInput from "./MathInput.jsx";
import { draftConsole } from "../lib/turn.js";
import { postJSON } from "../lib/api.js";

// Durable copy of the answers (study_attempts → study_coverage), keyed by a hash of the block's
// source so a second send replaces. Fire and forget.
function hashOf(s) {
  let h = 2166136261;
  for (const ch of str(s)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return (h >>> 0).toString(16);
}
function record(data, items) {
  postJSON("/api/study/attempts", { block: data.block, course: data.course || null, items })
    .catch(() => {});
}

const DOC_CITE = /^([^#\s]{1,64})#p(\d{1,4})$/;
// A slide is only drawn for a real doc ref; anything looser has nothing to load.
const DOC_SLIDE = /^(d[0-9a-f]{5,10})#p(\d{1,4})$/;

function SourceCite({ cite, quote }) {
  const m = DOC_CITE.exec(String(cite || "").trim());
  if (!m) return null;
  return <Cite refId={m[1]} page={Number(m[2])} quote={quote || undefined} />;
}

// `slide`: the page an item is worked from, drawn beside it (`cite` is where the answer is, shown once answered).
function Slide({ slide }) {
  const m = DOC_SLIDE.exec(String(slide || "").trim().toLowerCase());
  if (!m) return null;
  return <div className="study-slide"><DocPage docRef={m[1]} page={Number(m[2])} width={520} /></div>;
}
const hasSlide = (item) => DOC_SLIDE.test(String(item?.slide || "").trim().toLowerCase());

const str = (v) => (typeof v === "string" ? v : v == null ? "" : String(v));

// The model favours one slot (first live quiz: all three at B), so choices are reordered,
// seeded by the question text so a streaming re-render keeps the order.
function shuffled(q) {
  let h = 2166136261;
  for (const ch of str(q.q)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  const rand = () => ((h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0) / 4294967296);
  const order = q.choices.map((_, i) => i);
  for (let i = order.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [order[i], order[j]] = [order[j], order[i]];
  }
  return { ...q, choices: order.map((i) => q.choices[i]), answer: order.indexOf(q.answer) };
}

// Free response: no `choices`; `answer` is text. `accept` lists short exact answers that self-check,
// else the user self-grades (their text goes back for regrading). `long` = textarea.
const isFree = (q) => !Array.isArray(q.choices);
const isChoice = (q) => Array.isArray(q.choices) && q.choices.length >= 2
  && Number.isInteger(q.answer) && q.answer >= 0 && q.answer < q.choices.length;
const norm = (s) => str(s).toLowerCase().replace(/[\s`'"“”‘’.,;:!?$\\]+/g, " ").trim();

// How sure (same values as study.CONFIDENCE): wrong+sure is a misconception, right+guessed won't hold.
// "don't know" is its own answer (a guess would score as one in four). Asked before the answer;
// given after, it is marked `late` (a rating made with the verdict on screen can be fitted to it).
const RATED = [["guess", "guess"], ["unsure", "unsure"], ["sure", "sure"]];
const SAID = { guess: "guessing", unsure: "unsure", sure: "sure" };
const DONT_KNOW = -1;   // `picked` value for a choice question answered "don't know"

function parse(kind, source) {
  let data;
  try { data = JSON.parse(source); } catch { return null; }
  if (Array.isArray(data)) data = kind === "quiz" ? { questions: data } : { cards: data };
  if (!data || typeof data !== "object") return null;
  const base = { title: str(data.title), course: str(data.course), block: hashOf(source) };
  if (kind === "quiz") {
    const questions = (Array.isArray(data.questions) ? data.questions : []).filter((q) =>
      q && str(q.q) && (isChoice(q) || (isFree(q) && str(q.answer))));
    const hints = (q) => (Array.isArray(q.hints) ? q.hints.map(str).filter(Boolean) : []);
    return questions.length
      ? { ...base, questions: questions.map((q) => (isFree(q) ? {
          ...q, free: true, hints: hints(q),
          accept: (Array.isArray(q.accept) ? q.accept : []).map(norm).filter(Boolean),
        } : { ...shuffled(q), hints: hints(q) })) }
      : null;
  }
  const cards = (Array.isArray(data.cards) ? data.cards : []).filter((c) =>
    c && str(c.front) && str(c.back));
  return cards.length ? { ...base, cards } : null;
}

function Quiz({ data, inline }) {
  const [picked, setPicked] = useState({});   // choice question index -> choice index
  const [typed, setTyped] = useState({});     // free question index -> what they wrote
  const [spelled, setSpelled] = useState({}); // … a math answer as typed text, for `accept`
  const [mathMode, setMathMode] = useState({}); // free question index -> math field on or off
  const [graded, setGraded] = useState({});   // free question index -> { ok, auto } once graded
  const [shown, setShown] = useState({});     // free question index -> answer revealed, not yet graded
  const [hinted, setHinted] = useState({});   // question index -> hints opened
  const [sure, setSure] = useState({});       // question index -> confidence
  const [late, setLate] = useState({});       // question index -> true when rated after the verdict
  const [notes, setNotes] = useState({});     // question index -> their question or comment on it
  const [noting, setNoting] = useState({});   // question index -> note box open
  const [struck, setStruck] = useState({});   // question index -> { choice index: true } crossed out
  const qs = data.questions;
  const strike = (i, j) => setStruck((s) => {
    const row = { ...s[i] };
    if (row[j]) delete row[j]; else row[j] = true;
    return { ...s, [i]: row };
  });
  const struckCount = (i) => Object.keys(struck[i] || {}).length;
  const hintNote = (i) => (hinted[i] ? ` (after ${hinted[i]} hint${hinted[i] > 1 ? "s" : ""})` : "");
  const lateNote = (i) => (late[i] ? ", rated after seeing the answer" : "");
  const sureNote = (i) => (SAID[sure[i]] ? `, I was ${SAID[sure[i]]}${lateNote(i)}` : "");
  const noted = qs.map((q, i) => i).filter((i) => str(notes[i]).trim());
  const ok =(q, i) => (q.free ? graded[i]?.ok : i in picked ? picked[i] === q.answer : undefined);
  const answered = qs.filter((q, i) => ok(q, i) !== undefined).length;
  const right = qs.filter((q, i) => ok(q, i) === true).length;

  function check(q, i) {
    const mine = norm(typed[i]);
    if (!mine) return;
    if (q.accept.includes(mine) || q.accept.includes(norm(spelled[i]))) setGraded((g) => ({ ...g, [i]: { ok: true, auto: true } }));
    else setShown((s) => ({ ...s, [i]: true }));
  }
  function reset() {
    setPicked({}); setTyped({}); setSpelled({}); setMathMode({}); setGraded({}); setShown({}); setHinted({}); setSure({});
    setLate({}); setNotes({}); setNoting({}); setStruck({});
  }
  function dontKnow(q, i) {
    setSure((s) => ({ ...s, [i]: "dont_know" }));
    if (q.free) setGraded((g) => ({ ...g, [i]: { ok: false } }));
    else setPicked((p) => ({ ...p, [i]: DONT_KNOW }));
  }

  function send() {
    const missed = qs.map((q, i) => [q, i]).filter(([q, i]) => ok(q, i) === false);
    const skipped = qs.length - answered;
    const lines = [`Quiz results${data.title ? ` (${data.title})` : ""}: ${right}/${qs.length} right`
      + (skipped ? `, ${skipped} skipped` : "") + "."];
    if (missed.length) {
      lines.push("Missed:");
      for (const [q, i] of missed)
        lines.push(q.free
          ? `- Q${i + 1}: ${str(q.q)} — `
            + (str(typed[i]).trim() ? `I wrote "${str(typed[i]).trim()}"` : "I didn't know")
            + `, the answer was "${str(q.answer)}"${sureNote(i)}${hintNote(i)}`
          : `- Q${i + 1}: ${str(q.q)} — `
            + (picked[i] === DONT_KNOW ? "I didn't know" : `I picked "${str(q.choices[picked[i]])}"`)
            + `, the answer was "${str(q.choices[q.answer])}"${sureNote(i)}${hintNote(i)}`
            + (struckCount(i) ? ` (had crossed out ${struckCount(i)} of ${q.choices.length})` : ""));
    }
    const helped = qs.map((q, i) => i).filter((i) => ok(qs[i], i) === true && hinted[i]);
    if (helped.length)
      lines.push("Right with hints: " + helped.map((i) => `Q${i + 1}${hintNote(i)}`).join(", "));
    const shaky = qs.map((q, i) => i).filter((i) => ok(qs[i], i) === true
      && (sure[i] === "guess" || sure[i] === "unsure"));
    if (shaky.length)
      lines.push("Right but not sure: " + shaky.map((i) => `Q${i + 1} (${SAID[sure[i]]}${lateNote(i)})`).join(", "));
    // Self-graded "right"s go back too: the console judges whether they covered the answer.
    const selfRight = qs.map((q, i) => [q, i]).filter(([q, i]) => q.free && graded[i]?.ok && !graded[i].auto);
    if (selfRight.length) {
      lines.push("Self-graded right (check me):");
      for (const [q, i] of selfRight)
        lines.push(`- Q${i + 1}: ${str(q.q)} — I wrote "${str(typed[i]).trim()}"${sureNote(i)}`);
    }
    // Last, question restated: a note may be on a right answer, which nothing above names.
    if (noted.length) {
      lines.push("My notes on specific questions:");
      for (const i of noted) {
        const verdict = ok(qs[i], i) === true ? "right" : ok(qs[i], i) === false ? "missed" : "not graded";
        lines.push(`- Q${i + 1} (${verdict}): ${str(qs[i].q)} — ${str(notes[i]).trim()}`);
      }
    }
    draftConsole(lines.join("\n"));
    record(data, qs.map((q, i) => [q, i]).filter(([q, i]) => ok(q, i) !== undefined)
      .map(([q, i]) => ({ idx: i, kind: q.free ? "free" : "choice", question: str(q.q),
                          correct: ok(q, i), self_graded: !!(q.free && !graded[i]?.auto),
                          ref: str(q.slide || q.cite), confidence: sure[i] || null,
                          rated_after: !!late[i] })));
  }

  // Clicking the lit one clears it. "don't know" is an answer, so it only
  // exists while the question is open.
  function confidence(q, i, open) {
    const rate = (v) => {
      setSure((s) => {
        const next = { ...s };
        if (next[i] === v) delete next[i]; else next[i] = v;
        return next;
      });
      setLate((l) => ({ ...l, [i]: !open && sure[i] !== v }));
    };
    return (
      <div className="study-conf" role="group" aria-label="How sure are you?">
        <span className="study-conf-label">{open ? "how sure?" : "how sure were you?"}</span>
        {RATED.map(([v, label]) => (
          <button key={v} className={`study-act${sure[i] === v ? " on" : ""}`} aria-pressed={sure[i] === v}
                  onClick={() => rate(v)}>
            {label}
          </button>
        ))}
        {open && <button className="study-act" onClick={() => dontKnow(q, i)}>don’t know</button>}
      </div>
    );
  }

  // After an answer: the rating if not given up front (one given first is locked) and a note for the results.
  function afterAnswer(q, i) {
    const open = noting[i] || str(notes[i]);
    return (
      <div className="study-after">
        <div className="study-after-row">
          {sure[i] === "dont_know" ? null
            : sure[i] && !late[i] ? <span className="study-conf-label">you said {RATED.find(([v]) => v === sure[i])[1]}</span>
            : confidence(q, i, false)}
          {!open && (
            <button className="study-act study-note-btn"
                    onClick={() => setNoting((n) => ({ ...n, [i]: true }))}>
              Ask or comment
            </button>
          )}
        </div>
        {open && (
          <textarea className="study-input study-note" rows={2} autoFocus={!!noting[i]}
                    aria-label={`Your question or comment on question ${i + 1}`}
                    placeholder="Ask about this, or leave a comment. It goes with the results."
                    value={notes[i] || ""}
                    onChange={(e) => setNotes((x) => ({ ...x, [i]: e.target.value }))} />
        )}
      </div>
    );
  }

  // Hints open one at a time while the question is open; once answered `why` covers them.
  function hints(q, i, open) {
    if (!q.hints.length) return null;
    const n = hinted[i] || 0;
    return (
      <>
        {q.hints.slice(0, n).map((h, k) => (
          <p key={k} className="study-hint">
            <span className="study-hint-num">hint {k + 1}</span> {inline(h, `q${i}h${k}`)}
          </p>
        ))}
        {open && n < q.hints.length && (
          <button className="study-act study-hint-btn"
                  onClick={() => setHinted((x) => ({ ...x, [i]: n + 1 }))}>
            {n ? "Another hint" : "Hint"} <span className="study-hint-count">{n}/{q.hints.length}</span>
          </button>
        )}
      </>
    );
  }

  return (
    <div className="study study-quiz">
      <div className="study-head">
        <span className="study-kind">quiz</span>
        {data.title && <span className="study-title">{inline(data.title, "qt")}</span>}
      </div>
      {/* One group per run on the same slide, drawn once and pinned. Numbered by hand: a grid item loses its marker. */}
      <div className="study-qs">
        {groupBySlide(qs).map((g) => (
          <div key={g.items[0]} className={`study-q${g.slide ? " with-slide" : ""}`}>
            {g.slide && <div className="study-slide-pin"><Slide slide={g.slide} /></div>}
            <div className="study-q-group">
              {g.items.map((i) => question(qs[i], i))}
            </div>
          </div>
        ))}
      </div>
      <div className="study-foot">
        <span>{answered}/{qs.length} answered{answered ? ` · ${right} right` : ""}</span>
        <button className="study-act" onClick={reset} disabled={!answered}>Reset</button>
        <button className="study-act primary" onClick={send} disabled={!answered && !noted.length}>
          Send results
        </button>
      </div>
    </div>
  );

  function question(q, i) {
          if (q.free) return freeQuestion(q, i);
          const pick = picked[i];
          const done = pick !== undefined;
          return (
              <div key={i} className="study-q-main">
              <div className="study-q-text">
                <span className="study-q-num">{i + 1}.</span> {inline(str(q.q), `q${i}`)}
              </div>
              {hints(q, i, !done)}
              {!done && confidence(q, i, true)}
              {/* Crossing out (✕ or right-click) doesn't rule a choice out: it can still be picked. */}
              <div className="study-choices">
                {q.choices.map((c, j) => {
                  const state = !done ? "" : j === q.answer ? " right" : j === pick ? " wrong" : " dim";
                  const out = !!struck[i]?.[j];
                  const letter = String.fromCharCode(65 + j);
                  return (
                    <div key={j} className={`study-choice-row${out ? " struck" : ""}`}>
                      <button className={`study-choice${state}`} disabled={done}
                              onClick={() => setPicked((p) => ({ ...p, [i]: j }))}
                              onContextMenu={(e) => { if (!done) { e.preventDefault(); strike(i, j); } }}>
                        <span className="study-letter">{letter}</span>
                        <span className="study-choice-text">{inline(str(c), `q${i}c${j}`)}</span>
                      </button>
                      {!done && (
                        <button className="study-strike" aria-pressed={out}
                                aria-label={out ? `Restore ${letter}` : `Cross out ${letter}`}
                                title={out ? "Restore" : "Cross out (or right-click the choice)"}
                                onClick={() => strike(i, j)}>✕</button>
                      )}
                    </div>
                  );
                })}
              </div>
              {done && (
                <p className={`study-why${pick === q.answer ? " right" : " wrong"}`}>
                  <strong>{pick === q.answer ? "Right." : `It’s ${String.fromCharCode(65 + q.answer)}.`}</strong>
                  {q.why ? <> {inline(str(q.why), `q${i}w`)}</> : null}
                  <SourceCite cite={q.cite} quote={q.quote} />
                </p>
              )}
              {done && afterAnswer(q, i)}
              </div>
          );
  }

  // Type, check; an `accept` match grades itself, else you call it. Locked once graded.
  function freeQuestion(q, i) {
    const g = graded[i];
    const open = !g && !shown[i];
    const Field = q.long ? "textarea" : "input";
    const math = mathMode[i] ?? !!q.math;
    const toggleMath = () => {
      // Each field's text means something else to the other, so it starts over.
      setTyped((t) => ({ ...t, [i]: "" }));
      setSpelled((t) => ({ ...t, [i]: "" }));
      setMathMode((m) => ({ ...m, [i]: !math }));
    };
    return (
      <div key={i} className="study-q-main">
        <div className="study-q-text">
          <span className="study-q-num">{i + 1}.</span> {inline(str(q.q), `q${i}`)}
        </div>
        {hints(q, i, open)}
        {open && confidence(q, i, true)}
        <div className="study-free">
          {math && open && (
            <div className="study-input study-math">
              <MathInput key={i} label="Your answer" placeholder="Your answer"
                onChange={(latex, ascii) => {
                  setTyped((t) => ({ ...t, [i]: latex ? `$${latex}$` : "" }));
                  setSpelled((t) => ({ ...t, [i]: ascii }));
                }}
                onEnter={() => check(q, i)} />
            </div>
          )}
          {math && !open && (
            <div className={`study-input study-math${g ? (g.ok ? " right" : " wrong") : ""}`}>
              {inline(str(typed[i]), `q${i}t`)}
            </div>
          )}
          {!math && (
          <Field className={`study-input${g ? (g.ok ? " right" : " wrong") : ""}`}
                 value={typed[i] || ""} readOnly={!open} rows={q.long ? 3 : undefined}
                 placeholder={q.long ? "Write your answer…" : "Your answer"}
                 onChange={(e) => setTyped((t) => ({ ...t, [i]: e.target.value }))}
                 onKeyDown={(e) => {
                   // Enter checks a one-liner; a textarea needs ⌘/Ctrl+Enter.
                   if (e.key === "Enter" && (!q.long || e.metaKey || e.ctrlKey)) {
                     e.preventDefault(); check(q, i);
                   }
                 }} />
          )}
          {open && (
            <div className="study-free-acts">
              <button className={`study-act study-math-toggle${math ? " on" : ""}`}
                      onClick={toggleMath} aria-pressed={math}
                      title={math ? "Back to plain text" : "Type this as math"}>ƒx</button>
              <button className="study-act primary" onClick={() => check(q, i)}
                      disabled={!norm(typed[i])}>Check</button>
              <button className="study-act" onClick={() => setShown((s) => ({ ...s, [i]: true }))}>
                Show answer
              </button>
            </div>
          )}
        </div>
        {(shown[i] || g) && (
          <div className={`study-why${g ? (g.ok ? " right" : " wrong") : ""}`}>
            {g ? <strong>{g.auto ? "Right." : g.ok ? "Marked right."
                          : sure[i] === "dont_know" ? "Didn’t know." : "Marked missed."}</strong>
               : <strong className="study-answer-label">Answer:</strong>}
            {" "}{inline(str(q.answer), `q${i}a`)}
            {q.why ? <> — {inline(str(q.why), `q${i}w`)}</> : null}
            <SourceCite cite={q.cite} quote={q.quote} />
            {!g && (
              <div className="study-free-acts">
                <button className="study-act wrong"
                        onClick={() => setGraded((x) => ({ ...x, [i]: { ok: false } }))}>Missed it</button>
                <button className="study-act right" disabled={!norm(typed[i])}
                        onClick={() => setGraded((x) => ({ ...x, [i]: { ok: true } }))}>Got it</button>
              </div>
            )}
          </div>
        )}
        {!open && afterAnswer(q, i)}
      </div>
    );
  }
}

// Consecutive questions sharing a slide, as [{ slide, items: [index…] }]; a return to slide 5 draws it again.
function groupBySlide(qs) {
  const groups = [];
  qs.forEach((q, i) => {
    const slide = hasSlide(q) ? String(q.slide).trim().toLowerCase() : null;
    const last = groups[groups.length - 1];
    if (last && slide && last.slide === slide) last.items.push(i);
    else groups.push({ slide, items: [i] });
  });
  return groups;
}

function Cards({ data, inline }) {
  const all = useMemo(() => data.cards.map((_, i) => i), [data]);
  const [order, setOrder] = useState(all);
  const [pos, setPos] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [known, setKnown] = useState({});    // card index -> true (got it) | false (missed)

  const done = pos >= order.length;
  const card = done ? null : data.cards[order[pos]];
  const missed = Object.keys(known).filter((k) => known[k] === false).map(Number);

  function mark(ok) {
    setKnown((k) => ({ ...k, [order[pos]]: ok }));
    setFlipped(false);
    setPos((p) => p + 1);
  }
  function again(indices) {
    setOrder(indices);
    setPos(0);
    setFlipped(false);
    setKnown((k) => {
      const next = { ...k };
      for (const i of indices) delete next[i];
      return next;
    });
  }
  function send() {
    const got = Object.values(known).filter(Boolean).length;
    const lines = [`Flashcard results${data.title ? ` (${data.title})` : ""}: `
                   + `${got}/${data.cards.length} known.`];
    if (missed.length) {
      lines.push("Missed:");
      for (const i of missed) lines.push(`- ${str(data.cards[i].front)}`);
    }
    draftConsole(lines.join("\n"));
    record(data, Object.keys(known).map(Number).map((i) => ({
      idx: i, kind: "card", question: str(data.cards[i].front), correct: known[i],
      self_graded: true, ref: str(data.cards[i].slide || data.cards[i].cite) })));
  }
  // Keys only while the deck has focus (typing in the composer must not flip): space/enter flips, 1/← missed, 2/→ got it.
  function onKey(e) {
    if (done) return;
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); setFlipped((f) => !f); }
    else if (flipped && (e.key === "1" || e.key === "ArrowLeft")) { e.preventDefault(); mark(false); }
    else if (flipped && (e.key === "2" || e.key === "ArrowRight")) { e.preventDefault(); mark(true); }
  }

  return (
    <div className="study study-cards" tabIndex={0} onKeyDown={onKey}>
      <div className="study-head">
        <span className="study-kind">cards</span>
        {data.title && <span className="study-title">{inline(data.title, "ct")}</span>}
        <span className="study-count">{done ? "done" : `${pos + 1} / ${order.length}`}</span>
      </div>
      {!done && (
        <div className={`study-cardrow${hasSlide(card) ? " with-slide" : ""}`}>
          <Slide key={order[pos]} slide={card.slide} />
          <div className="study-q-main">
          <button className={`study-card${flipped ? " flipped" : ""}`}
                  onClick={() => setFlipped((f) => !f)}
                  title={flipped ? "Show the front" : "Flip (space)"}>
            <span className="study-side">{flipped ? "back" : "front"}</span>
            <span className="study-face">
              {inline(str(flipped ? card.back : card.front), `c${order[pos]}${flipped ? "b" : "f"}`)}
            </span>
          </button>
          <div className="study-foot">
            {/* Outside the card: the pill is a button, and the card is one. */}
            {flipped && card.cite && (
              <span className="study-source">source <SourceCite cite={card.cite} quote={card.quote} /></span>
            )}
            {flipped ? (
              <>
                <button className="study-act wrong" onClick={() => mark(false)}>Missed it <kbd>1</kbd></button>
                <button className="study-act right" onClick={() => mark(true)}>Got it <kbd>2</kbd></button>
              </>
            ) : (
              <button className="study-act" onClick={() => setFlipped(true)}>Flip <kbd>space</kbd></button>
            )}
          </div>
          </div>
        </div>
      )}
      {done && (
        <div className="study-foot">
          <span>
            {Object.values(known).filter(Boolean).length}/{data.cards.length} known
            {missed.length ? ` · ${missed.length} missed` : ""}
          </span>
          {missed.length > 0 && (
            <button className="study-act" onClick={() => again(missed)}>Again, missed only</button>
          )}
          <button className="study-act" onClick={() => { setKnown({}); again(all); }}>Start over</button>
          <button className="study-act primary" onClick={send}>Send results</button>
        </div>
      )}
    </div>
  );
}

// The model sometimes double-escapes newlines in the JSON string (literal `\n`, even mixed with real
// breaks, which leaves an unclosed fence). Literal ones become breaks outside math (`$\nabla$`, `\neq`)
// and outside well-formed code (`printf("\n")`).
const KEEP_SPLIT = new RegExp(`(${[
  /\$\$[^$]*\$\$|\$[^$]*\$|\\\([\s\S]*?\\\)/.source,   // math
  /```[^\n]*\n[\s\S]*?```/.source,                     // a fence with real breaks
  /(?<!`)`[^`\n]+`(?!`)/.source,                       // inline code
].join("|")})`);
export function unescapeBreaks(text) {
  const s = str(text);
  if (!s.includes("\\n")) return s;
  return s.split(KEEP_SPLIT).map((part, k) => (k % 2 ? part : part.replace(/\\n/g, "\n"))).join("");
}

export default function StudyBlock({ kind, source, closed, renderInline }) {
  const data = useMemo(() => (closed ? parse(kind, source) : null), [kind, source, closed]);
  const inline = (text, key) => renderInline(unescapeBreaks(text), `study-${key}`);
  if (!closed) {
    return <div className="study study-pending">Writing the {kind === "quiz" ? "quiz" : "cards"}…</div>;
  }
  if (!data) {
    // Malformed JSON shouldn't eat the content: show it as the code it is.
    return (
      <div className="study study-broken">
        <p className="study-note">Couldn’t read this as a {kind === "quiz" ? "quiz" : "card deck"}; here’s what was written.</p>
        <pre className="md-code">{source}</pre>
      </div>
    );
  }
  return kind === "quiz"
    ? <Quiz data={data} inline={inline} />
    : <Cards data={data} inline={inline} />;
}
