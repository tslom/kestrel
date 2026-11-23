import { useCallback, useState } from "react";
import { fetchJSON } from "./api.js";

// Context pills: [{ id, label, data }] — attached to the NEXT console message.
// Every pin button, drag and drop in the app ends up in one of these.
//
// A pin whose data is { sessionRef, project } is another tab or a past
// session (#94: an `@` pick, or a session row dragged in). It lands as a
// `session` pill holding that session's digest, not its transcript
// (app/session_digest.py), read here so every way in shares one path.
export const sessionPin = (id, project = null) => ({ sessionRef: id, project: project || null });

export function usePills() {
  const [pills, setPills] = useState([]);

  const pinContext = useCallback((label, data) => {
    if (data?.sessionRef) {
      const id = `${label}-${Date.now()}`;
      setPills((prev) => [...prev.filter((p) => p.label !== label),
        { id, label, kind: "session", data: "", note: "reading…", pending: true }]);
      const q = data.project ? `?project=${encodeURIComponent(data.project)}` : "";
      fetchJSON(`/api/sessions/${encodeURIComponent(data.sessionRef)}/digest${q}`)
        .then((r) => setPills((prev) => prev.map((p) => (p.id === id ? {
          ...p, data: r.text, pending: false,
          note: `${r.asked} message${r.asked === 1 ? "" : "s"}${r.files?.length ? `, ${r.files.length} file${r.files.length === 1 ? "" : "s"} changed` : ""} · a digest, not the transcript`,
        } : p))))
        .catch((err) => setPills((prev) => prev.map((p) => (p.id === id
          ? { ...p, data: `(couldn't read that session: ${err.message})`, note: `couldn't read it: ${err.message}`, pending: false }
          : p))));
      return;
    }
    setPills((prev) => {
      const next = prev.filter((p) => p.label !== label); // re-pin replaces
      return [...next, { id: `${label}-${Date.now()}`, label, data }];
    });
  }, []);

  const removePill = useCallback((id) => {
    setPills((prev) => prev.filter((p) => p.id !== id));
  }, []);

  const clearPills = useCallback(() => setPills([]), []);

  // Dropped files pin like widgets do, with two differences: several can carry
  // the same name, and each one appears before its contents are known, so it
  // needs an id back and a way to be filled in when the read finishes.
  const addPill = useCallback((pill) => {
    const id = `${pill.label}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    setPills((prev) => [...prev, { id, ...pill }]);
    return id;
  }, []);

  const updatePill = useCallback((id, patch) => {
    setPills((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  }, []);

  return { pills, pinContext, removePill, clearPills, addPill, updatePill };
}
