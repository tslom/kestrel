// A file a reply names, made clickable — once the backend says it's real.
//
// Until then (and forever, if it isn't) the text renders exactly as written:
// a sentence mentioning a path that doesn't exist is ordinary, and a link
// that goes nowhere is worse than none. What's real is app/files.py's call.
//
// Click: an image, PDF, video, audio, markdown or text file opens in the
// console's viewer pane (lib/files.js FileContext); anything else, a folder,
// or a click outside a console opens with the default app (`open`). Apps and
// scripts are only ever revealed — the backend enforces that, this just says
// so. ⌥-click, or the folder button on hover, reveals any of them in Finder.
import { useState } from "react";
import Icon from "./icons.jsx";
import VaultLink from "./VaultLink.jsx";
import { useFiles, useResolved, openFile } from "../lib/files";
import { isVaultNote, lineless } from "../lib/paths";

const VERB = {
  image: "View", pdf: "View", video: "Play", audio: "Play", markdown: "View", text: "View",
  dir: "Open in Finder", app: "Reveal in Finder", exec: "Reveal in Finder", other: "Open",
};

export default function PathLink({ path, children }) {
  const files = useFiles();
  // A relative .md outside a project is a vault note: Obsidian, as before.
  if (isVaultNote(path, files?.bases)) return <VaultLink target={lineless(path)}>{children}</VaultLink>;
  return <FileLink path={path} files={files}>{children}</FileLink>;
}

function FileLink({ path, files, children }) {
  const hit = useResolved(path, files?.bases);
  const [error, setError] = useState(null);
  if (!hit) return children;

  const inApp = hit.viewable && files?.view;
  const fail = (e) => setError(String(e?.message || e));
  function click(e) {
    e.preventDefault();
    setError(null);
    if (e.altKey) openFile(hit.path, true).catch(fail);
    else if (inApp && !e.metaKey) files.view(hit);
    else openFile(hit.path).catch(fail);
  }
  function reveal(e) {
    e.preventDefault();
    e.stopPropagation();
    setError(null);
    openFile(hit.path, true).catch(fail);
  }
  const verb = inApp ? VERB[hit.kind] : hit.executable ? "Reveal in Finder" : VERB[hit.kind] || "Open";
  return (
    <span className="file-link-wrap">
      <a className={`file-link${error ? " vault-link-missing" : ""}`}
         href={`file://${encodeURI(hit.path)}`} onClick={click} data-kind={hit.kind}
         title={error || `${verb} · ${hit.path}${inApp ? "\n⌘-click: default app · ⌥-click: reveal in Finder" : ""}`}>
        {children}
      </a>
      <button type="button" className="file-reveal" onClick={reveal}
              title="Reveal in Finder" aria-label={`Reveal ${hit.name} in Finder`}>
        <Icon name="folder" size={11} />
      </button>
    </span>
  );
}
