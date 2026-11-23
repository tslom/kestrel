import { useState } from "react";
import { JsCode } from "../lib/jscode.jsx";
import { toolLabel } from "../plan/StatusBar.jsx";

// A run_script call opened up (console_code): the script coloured, folded
// past FOLD lines, then what it returned. The collapsed run already says it
// ran a script; this is the "what did it actually do". `t` is the log's tool
// entry: `raw` its input, `result` / `error` from its tool_result.
const FOLD = 10;

// The model gets the value as compact JSON (tokens); a person reads it laid
// out. A list of strings — text pulled out of each read — reads best as the
// strings themselves. The footer ("[3 calls: … · 1.2s]") stays as it came.
function readable(result) {
  const lines = result.split("\n");
  const foot = /^\[\d+ calls/.test(lines[lines.length - 1] || "") ? lines.pop() : null;
  const logAt = lines.indexOf("log:");
  const body = (logAt < 0 ? lines : lines.slice(0, logAt)).join("\n");
  const rest = logAt < 0 ? [] : lines.slice(logAt);
  let shown = body;
  try {
    const v = JSON.parse(body);
    shown = Array.isArray(v) && v.length && v.every((x) => typeof x === "string")
      ? v.join("\n\n") : JSON.stringify(v, null, 2);
  } catch { /* text, an error, or truncated: as it came */ }
  return [shown, ...rest, ...(foot ? [foot] : [])].join("\n");
}

export default function ScriptCall({ t }) {
  const [all, setAll] = useState(false);
  let script = "";
  try { script = JSON.parse(t.raw)?.script || ""; } catch { /* clipped or absent */ }
  const lines = script.split("\n");
  const long = lines.length > FOLD + 2;
  const shown = long && !all ? lines.slice(0, FOLD).join("\n") : script;
  return (
    <div className="entry tool has-cmd script-call">
      <span className="tool-name">{toolLabel(t.name)}</span>{" "}
      {t.desc && <span className="tool-desc">{t.desc}</span>}
      <pre className="tool-cmd"><JsCode code={shown || t.detail || ""} /></pre>
      {long && (
        <button className="note-more script-fold" onClick={() => setAll((v) => !v)}>
          {all ? "fold" : `${lines.length - FOLD} more lines`}
        </button>
      )}
      {t.result != null && (
        <pre className={`tool-cmd script-result${t.error ? " error" : ""}`}>{readable(t.result)}</pre>
      )}
    </div>
  );
}
