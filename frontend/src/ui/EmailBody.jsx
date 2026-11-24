import { GMAIL_URL } from "../lib/googleLinks";

// A message as text (EmailReader's Text view; the only view without an HTML part). The backend
// sends text/plain or tag-stripped HTML, so nothing to sanitize and no remote image.

// Trailing punctuation is far more often the sentence's than the URL's.
const URL_RE = /(https?:\/\/[^\s<>()[\]]+[^\s<>()[\].,;:!?'"])/g;

function linkify(line) {
  return line.split(URL_RE).map((part, i) =>
    i % 2 === 0
      ? part
      : <a key={i} href={part} target="_blank" rel="noopener noreferrer">{part}</a>);
}

// Quoted chains render dimmed, not cut (the backend drops them only when a body is too long).
const QUOTED = /^\s*>/;

export function EmailText({ msg, id }) {
  if (!msg.body.trim())
    return <p className="email-body dim">No text in this message.</p>;

  const notes = [];
  if (msg.quoted_trimmed) notes.push("quoted chain trimmed");
  if (msg.truncated) notes.push("truncated");

  return (
    <div className="email-body">
      {msg.body.split("\n").map((line, i) => (
        <p key={i} className={QUOTED.test(line) ? "quoted" : undefined}>
          {linkify(line) /* an empty line still wants its blank height */}
          {line.trim() === "" ? " " : null}
        </p>
      ))}
      {notes.length > 0 && (
        <p className="email-body-note">
          {notes.join(" · ")} — <a href={GMAIL_URL(id)}
            target="_blank" rel="noopener noreferrer">whole message in Gmail</a>
        </p>
      )}
    </div>
  );
}
