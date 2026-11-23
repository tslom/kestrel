import { useState } from "react";
import BarMenu from "./BarMenu.jsx";
import { effortsFor, findModel, modelChoices, prettyModel } from "../lib/consolePrefs.js";

// The model choices every <select> offers (lib/consolePrefs.js modelChoices):
// "default" and the aliases, then the pinned versions in their own group.
export function ModelOptions({ choices }) {
  return (
    <>
      {choices.main.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
      {choices.pinned.length > 0 && (
        <optgroup label="pinned versions">
          {choices.pinned.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </optgroup>
      )}
    </>
  );
}

// What the tab is running, in the bar's words: "fable 5.2". The stream's own
// word (`live`, a resolved id) first, since that's what's actually answering
// (a /model mid-session included); before the first turn names it, what the
// pick resolves to.
export function runningName(models, { live, model, defaultModel }) {
  const picked = model === "default" ? defaultModel : model;
  const row = findModel(models, live) || (!live && findModel(models, picked));
  const name = row?.label || prettyModel(live) || picked || "default";
  return name.toLowerCase();
}

// The bar's model · effort chip, and the picker it opens: the model (aliases,
// the pinned versions folded) and the effort levels that model takes. Both
// are fixed at session start, so a pick reconnects the tab (resume keeps the
// conversation); mid-turn that would cut the turn, so it waits.
// In a crowded status bar the version goes first ("opus 5.5" → "opus";
// lib/barHost.js "model-short"), never the chip.
export function ModelChip({ models, model, defaultModel, effort, live, busy, onPick }) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState(null);
  const name = runningName(models, { live, model, defaultModel });
  const [, base, version] = /^(.*?)(\s[\d.]+)?$/.exec(name);
  const efforts = effortsFor(models, model, defaultModel);
  const effortWord = effort && efforts.includes(effort) ? effort : null;
  return (
    <>
      <button ref={setAnchor} className={`bar-btn model-chip${open ? " on" : ""}`}
        aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={`Running ${live || name}${effortWord ? ` at ${effortWord} effort` : " at the CLI's default effort"}`
          + " · click to change the model or effort"}>
        <span className="mc-name">{base}{version && <span data-cut="model-short">{version}</span>}</span>
        {efforts.length > 0 && <span className="mc-effort"> · {effortWord || "default"}</span>}
      </button>
      <BarMenu open={open} anchor={anchor} onClose={() => setOpen(false)}>
        <ModelMenu models={models} model={model} defaultModel={defaultModel} effort={effort}
          busy={busy} onPick={(m, e) => { onPick(m, e); setOpen(false); }} />
      </BarMenu>
    </>
  );
}

export function ModelMenu({ models, model, defaultModel, effort, busy, onPick }) {
  const { main, pinned } = modelChoices(models, defaultModel, model);
  const [older, setOlder] = useState(() => pinned.some(([v]) => v === model));
  const efforts = effortsFor(models, model, defaultModel);
  // A model that doesn't take the effort in force drops it, in the same reconnect.
  const pickModel = (m) => {
    if (m === model) return;
    const ok = effortsFor(models, m, defaultModel);
    onPick(m, effort && ok.includes(effort) ? effort : "");
  };
  const row = ([v, label]) => {
    const [alias, named] = label.includes(" · ") ? label.split(" · ") : [label, ""];
    return (
      <button key={v} role="menuitemradio" aria-checked={v === model} disabled={busy}
        className={`bm-item mp-row${v === model ? " on" : ""}`} onClick={() => pickModel(v)}>
        <span className="mp-alias">{alias}</span>
        {named && <span className="mp-named">{named}</span>}
      </button>
    );
  };
  return (
    <div className="model-menu">
      <div className="mp-head">model</div>
      {main.map(row)}
      {pinned.length > 0 && (
        <button className="bm-item mp-older" aria-expanded={older} onClick={() => setOlder((o) => !o)}>
          {older ? "▾" : "▸"} pinned versions ({pinned.length})
        </button>
      )}
      {older && pinned.map(row)}
      <div className="mp-head">effort</div>
      {efforts.length ? (
        <div className="mp-efforts" role="radiogroup" aria-label="Effort">
          {["", ...efforts].map((l) => (
            <button key={l || "default"} role="radio" aria-checked={(effort || "") === l} disabled={busy}
              className={(effort || "") === l ? "on" : ""}
              title={l ? `--effort ${l}` : "The CLI's own default for this model"}
              onClick={() => { if ((effort || "") !== l) onPick(model, l); }}>
              {l || "default"}
            </button>
          ))}
        </div>
      ) : (
        <div className="mp-note">This model has no effort setting.</div>
      )}
      <div className="mp-note">
        {busy ? "Changing either reconnects the tab, so it waits for this turn to end."
          : "Changing either reconnects the tab; the conversation carries on."}
      </div>
    </div>
  );
}
