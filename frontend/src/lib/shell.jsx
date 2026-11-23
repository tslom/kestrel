// A shell command, coloured enough to read at a glance: which program runs,
// its flags, its strings, where it pipes and redirects. Not a parser — a
// tokenizer that only has to be right about where words start and stop, so
// a heredoc or an odd quote degrades to plain text rather than to a lie.
//
// Used by the approval card and the opened tool run (New Features #56).

const OPS = ["&&", "||", ";;", ">>", "<<-", "<<", "2>&1", "2>", "&>", "|&", "|", ";", "&", ">", "<", "(", ")", "$(", "`"];
// After one of these the next word is a program again.
const STARTS = new Set(["&&", "||", "|", "|&", ";", "&", "(", "$(", "`", "\n"]);
// Words that hand the command position on to the word after them.
const PREFIX = new Set(["sudo", "env", "exec", "time", "nohup", "command", "builtin", "xargs",
                        "do", "then", "else", "if", "while", "until", "!"]);

/** [{ t: "cmd"|"flag"|"str"|"var"|"op"|"com"|"", s }] for `src`. */
export function shellTokens(src) {
  const out = [];
  const push = (t, s) => {
    const last = out[out.length - 1];
    if (last && last.t === t && t === "") last.s += s;
    else out.push({ t, s });
  };
  let i = 0, cmdNext = true;
  let heredoc = null, wantDelim = false;   // `<<EOF`: its body is data, not commands
  while (i < src.length) {
    const c = src[i];
    if (c === "\n" && heredoc) {
      const m = new RegExp(`\\n[\\t ]*${heredoc.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=\\n|$)`).exec(src.slice(i));
      const stop = m ? i + m.index : src.length;
      push("", "\n");
      if (m && m.index === 0) push("op", m[0].slice(1));      // empty body
      else {
        if (stop > i + 1) push("str", src.slice(i + 1, stop));
        if (m) { push("", "\n"); push("op", m[0].slice(1)); }
      }
      i = m ? stop + m[0].length : src.length;
      heredoc = null; cmdNext = true;
      continue;
    }
    if (c === "\n") { push("", c); i++; cmdNext = true; continue; }
    if (c === " " || c === "\t") { push("", c); i++; continue; }
    if (c === "\\" && src[i + 1] === "\n") { push("", "\\\n"); i += 2; continue; }
    // a comment only starts a word, so `a#b` and `${#x}` stay put
    if (c === "#" && (i === 0 || /\s/.test(src[i - 1]))) {
      const end = src.indexOf("\n", i);
      const stop = end < 0 ? src.length : end;
      push("com", src.slice(i, stop)); i = stop; continue;
    }
    const op = OPS.find((o) => src.startsWith(o, i));
    if (op) {
      push("op", op); i += op.length;
      if (STARTS.has(op)) cmdNext = true;
      if (op.startsWith("<<")) wantDelim = true;
      continue;
    }
    // a word: runs of quoted and bare text glued together, as the shell sees it
    let j = i, word = [];
    while (j < src.length && !/[\s|;&<>()`]/.test(src[j])) {
      if (src[j] === "'" || src[j] === '"') {
        const q = src[j];
        let k = j + 1;
        while (k < src.length && src[k] !== q) k += (q === '"' && src[k] === "\\") ? 2 : 1;
        word.push({ t: "str", s: src.slice(j, Math.min(k + 1, src.length)) });
        j = k + 1;
      } else if (src[j] === "$" && src[j + 1] !== "(") {
        const m = /^\$(\{[^}]*\}?|[A-Za-z_][A-Za-z0-9_]*|[0-9@#?$!*-])/.exec(src.slice(j));
        if (m) { word.push({ t: "var", s: m[0] }); j += m[0].length; }
        else { word.push({ t: "", s: "$" }); j++; }
      } else if (src[j] === "$") {
        break;   // `$(` — the operator branch takes it
      } else {
        let k = j;
        while (k < src.length && !/[\s|;&<>()`'"$]/.test(src[k])) k++;
        word.push({ t: "", s: src.slice(j, k) });
        j = k;
      }
    }
    if (j === i) { push("", c); i++; continue; }   // stray char, keep moving
    i = j;
    const text = word.map((w) => w.s).join("");
    const plain = word.length === 1 && word[0].t === "";
    if (wantDelim) {
      wantDelim = false;
      heredoc = text.replace(/['"]/g, "");
      push("op", text);
    } else if (cmdNext && plain && /^[A-Za-z_][A-Za-z0-9_]*=/.test(text)) {
      push("var", text);                       // FOO=bar cmd: still waiting for cmd
    } else if (cmdNext && plain) {
      push("cmd", text);
      cmdNext = PREFIX.has(text);
    } else if (plain && /^--?[A-Za-z0-9]/.test(text)) {
      const eq = text.indexOf("=");
      if (eq > 0) { push("flag", text.slice(0, eq)); push("", text.slice(eq)); }
      else push("flag", text);
    } else {
      for (const w of word) push(w.t, w.s);
      cmdNext = false;
    }
  }
  return out;
}

export function ShellCode({ code, className = "" }) {
  return (
    <code className={`sh ${className}`}>
      {shellTokens(code).map((tok, i) =>
        tok.t ? <span key={i} className={`sh-${tok.t}`}>{tok.s}</span> : tok.s)}
    </code>
  );
}
