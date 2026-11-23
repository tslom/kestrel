// A file a reply named, open beside the chat (FileLink.jsx → Console's
// FileContext). Same slot and chrome as the document pane, which shows the
// PDFs; this one shows the rest of what's worth seeing without leaving:
// images, video, audio, markdown (rendered) and text (as written).
//
// The bytes come from /api/files/raw, which serves only these kinds, text of
// every sort as text/plain, under a sandbox CSP (app/files.py) — so an .html
// or an .svg reads as its source or as a picture, never as a page.
import { useState } from "react";
import Icon from "../ui/icons.jsx";
import { renderMarkdownCached } from "../ui/markdown.jsx";
import { rawUrl, openFile, useText, forgetText } from "../lib/files";

// Open with the default app, reveal in Finder: for this pane and DocPane's
// file view alike.
export function FileActions({ path }) {
  const [err, setErr] = useState(null);
  const go = (reveal) => { setErr(null); openFile(path, reveal).catch((e) => setErr(String(e.message || e))); };
  return (
    <>
      {err && <span className="fpn-err" title={err}>couldn’t open</span>}
      <button className="dp-x" onClick={() => go(false)} title="Open with the default app">
        <Icon name="external" size={11} /></button>
      <button className="dp-x" onClick={() => go(true)} title="Reveal in Finder">
        <Icon name="folder" size={11} /></button>
    </>
  );
}

function Text({ path, markdown }) {
  const got = useText(path);
  if (!got) return <p className="dpn-note">Opening…</p>;
  if (got.error) return <p className="dpn-note doc-bad">Couldn’t open the file — {got.error}.</p>;
  return markdown
    ? <div className="fpn-md">{renderMarkdownCached(got.text)}</div>
    : <pre className="fpn-text">{got.text}</pre>;
}

function Body({ file }) {
  const src = rawUrl(file.path);
  switch (file.kind) {
    case "image": return <img className="fpn-img" src={src} alt={file.name} />;
    case "video": return <video className="fpn-media" src={src} controls autoPlay={false} />;
    case "audio": return <audio className="fpn-audio" src={src} controls />;
    case "markdown": return <Text path={file.path} markdown />;
    default: return <Text path={file.path} />;
  }
}

// `view` = { file: { path, name, kind }, seq }
export default function FilePane({ view, onClose }) {
  const { file } = view;
  const close = () => { forgetText(file.path); onClose(); };
  return (
    <aside className="doc-pane file-pane" tabIndex={-1} aria-label={`${file.name}, beside the chat`}
      onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } }}>
      <div className="dpn-head">
        <span className="dpn-title" title={file.path}>{file.name}</span>
        <FileActions path={file.path} />
        <button className="dp-x" onClick={close} aria-label="Close the file" title="Close · Esc">
          <Icon name="close" size={11} /></button>
      </div>
      <div className={`dpn-pages fpn-body fpn-${file.kind}`}>
        <Body key={view.seq} file={file} />
      </div>
    </aside>
  );
}
