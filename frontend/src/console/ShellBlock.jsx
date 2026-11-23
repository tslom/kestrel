import { Suspense, lazy, useLayoutEffect, useRef, useState } from "react";
import { useDebounce } from "../lib/timers.js";

// One `!` command: what you ran, how it's going, what it printed. The output
// scrolls in its own box, so the log following the conversation can't move it:
// this keeps to the newest line itself, until you scroll up in it to read, and
// picks up again when you come back down. Same rule as the log (onLogScroll).
//
// The command runs on a pty (backend: console.run_shell). If it's still going
// after TERM_AFTER_MS, the box turns into a live terminal on it, so a prompt,
// a password, vim or top can be answered right here; when it ends, the box is
// the plain text again, which is what Claude reads (New Features #58).

const AT_BOTTOM_PX = 24;
const TERM_AFTER_MS = 600;

// xterm loads only when a command outlives the wait. Never rejects: a lazy()
// rejection rethrows on every render (see ViewPane.jsx).
const TerminalPane = lazy(() => import("./TerminalPane.jsx").catch(() => ({
  default: () => <div className="shell-empty">The terminal didn’t load. Reload (⌘R) to open it.</div>,
})));

// A progress bar redraws its line with \r; text keeps only the last draw.
const settle = (text) => text.split("\n").map((l) => l.slice(l.lastIndexOf("\r") + 1)).join("\n");

export default function ShellBlock({ command, output, code, background, live, ttyId, onBackground }) {
  const box = useRef(null);
  const following = useRef(true);
  const [term, setTerm] = useState(false);

  const tty = Boolean(live && ttyId);
  useDebounce(() => setTerm(tty), tty ? TERM_AFTER_MS : 0, [live, ttyId]);

  useLayoutEffect(() => {
    if (following.current && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [output]);

  const onScroll = () => {
    const el = box.current;
    following.current = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_PX;
  };

  const failed = !live && !background && code != null && code !== 0;
  const status = live ? (term ? "running · type here" : "running") : background ? "in the background"
    : failed ? `exit ${code}` : null;
  const showTerm = term && live;

  return (
    <div className={`shell-block${live ? " live" : ""}${failed ? " failed" : ""}`}>
      <div className="shell-head">
        <span className="shell-sigil">$</span>
        <code className="shell-cmd">{command}</code>
        {status && <span className="shell-status">{status}</span>}
        {live && (
          <button className="shell-bg" onClick={onBackground}
            title="Keep it running in the background (Ctrl+B)">background</button>
        )}
      </div>
      {showTerm
        ? <Suspense fallback={<div className="shell-empty">opening a terminal…</div>}>
            <TerminalPane path={`/ws/console/shell?id=${ttyId}`} className="shell-term" />
          </Suspense>
        : output ? <pre ref={box} className="shell-output" onScroll={onScroll}>{settle(output)}</pre>
        : !live && <div className="shell-empty">{background ? "See Tasks in Claude’s view" : "no output"}</div>}
    </div>
  );
}
