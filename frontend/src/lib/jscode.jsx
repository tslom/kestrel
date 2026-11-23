// A run_script body (console_code), coloured enough to read: keywords,
// strings and templates, comments, numbers, and the calls that are tool
// reads. Like shell.jsx, a tokenizer rather than a parser — an odd regex
// literal degrades to plain text, never to a wrong colour across the file.
// Reuses shell.jsx's sh-* classes so both read as one palette.

const KEYWORDS = new Set([
  "const", "let", "var", "await", "async", "return", "if", "else", "for", "of",
  "in", "while", "do", "break", "continue", "function", "new", "try", "catch",
  "finally", "throw", "typeof", "instanceof", "null", "undefined", "true",
  "false", "this", "switch", "case", "default", "class", "extends",
]);

// [{ t: "cmd"|"flag"|"str"|"var"|"op"|"com"|"", s }]: cmd = a call, op = a
// keyword, var = a number, str = a string, com = a comment.
export function jsTokens(src) {
  const out = [];
  const push = (t, s) => {
    const last = out[out.length - 1];
    if (last && last.t === t && t === "") last.s += s;
    else out.push({ t, s });
  };
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "/" && src[i + 1] === "/") {
      const end = src.indexOf("\n", i);
      const stop = end < 0 ? src.length : end;
      push("com", src.slice(i, stop)); i = stop; continue;
    }
    if (c === "/" && src[i + 1] === "*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end < 0 ? src.length : end + 2;
      push("com", src.slice(i, stop)); i = stop; continue;
    }
    if (c === '"' || c === "'" || c === "`") {
      let k = i + 1;
      while (k < src.length && src[k] !== c) k += src[k] === "\\" ? 2 : 1;
      push("str", src.slice(i, Math.min(k + 1, src.length)));
      i = k + 1; continue;
    }
    const num = /^\d[\d_]*(\.\d+)?/.exec(src.slice(i, i + 32));
    if (num && !/[\w$]/.test(src[i - 1] || "")) {
      push("var", num[0]); i += num[0].length; continue;
    }
    const word = /^[A-Za-z_$][\w$]*/.exec(src.slice(i, i + 64));
    if (word) {
      const w = word[0];
      const after = src.slice(i + w.length).match(/^\s*\(/);
      const prev = src.slice(0, i).trimEnd().slice(-1);
      push(KEYWORDS.has(w) ? "op" : after && prev !== "." ? "cmd" : "", w);
      i += w.length; continue;
    }
    push("", c); i++;
  }
  return out;
}

export function JsCode({ code, className = "" }) {
  return (
    <code className={`sh ${className}`}>
      {jsTokens(code).map((tok, i) =>
        tok.t ? <span key={i} className={`sh-${tok.t}`}>{tok.s}</span> : tok.s)}
    </code>
  );
}
