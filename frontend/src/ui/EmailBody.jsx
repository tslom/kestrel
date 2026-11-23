import { GMAIL_URL } from "../lib/googleLinks";

// A message as text — the reader's Text view, and its only view for mail
// with no HTML part (EmailReader.jsx fetches; this only renders).
//
// The backend hands back the text/plain part (or the HTML part with its tags
// stripped), so there is nothing here to sanitize and no remote image to
// announce that you opened the mail.

// Trailing punctuation is far more often the sentence's than the URL's.
const URL_RE = /(https?:\/\/[^\s<>()[\]]+[^\s<>()[\].,;:!?'"])/g;

function linkify(line) {
  return line.split(URL_RE).map((part, i) =>
    i % 2 === 0
      ? part
      : <a key={i} href={part} target="_blank" rel="noopener noreferrer">{part}</a>);
}

// Quoted chains are most of the bytes in a reply and almost none of what you
// opened it for, so they render dimmed rather than being cut — the backend
// only drops them when a body is too long to return whole.
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
