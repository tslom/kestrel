import { useState } from "react";
import BarMenu from "./BarMenu.jsx";
import { findModel, modelChoices, prettyModel } from "../lib/consolePrefs.js";

// What a model id is called in the log: "Opus 5.5".
export const modelName = (models, id) => findModel(models, id)?.label || prettyModel(id) || id;

// Where the model answering changed mid-conversation (New Features #101): a
// rule across the log, just above the first reply the new one wrote.
export function ModelMark({ models, from, to }) {
  return (
    <div className="model-mark" role="separator" data-testid="model-mark"
      title={`${from} answered up to here; ${to} from here on`}>
      <span className="mm-was">{modelName(models, from)}</span>
      <span className="mm-arrow" aria-hidden="true">→</span>
      <span className="mm-now">{modelName(models, to)}</span>
    </div>
  );
}

// "Retry with…" on a reply (New Features #104): the same message, answered
// again by the model you pick, in a fork. The conversation you're looking at
// stays whole under Resume, so the two answers can be compared.
//
// Claude's models only. Another provider would have to be handed this
// conversation to answer it, and the conversation is the personal part
// (backend app/delegates/boundary.py).
export default function RetryMenu({ models, model, defaultModel, answeredBy, disabled, blocked, onRetry }) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState(null);
  const { main } = modelChoices(models, defaultModel, model);
  const was = answeredBy ? modelName(models, answeredBy) : null;
  return (
    <span className="retry-wrap">
      <button ref={setAnchor} className={`bar-btn retry-btn${open ? " on" : ""}`}
        aria-haspopup="menu" aria-expanded={open} disabled={disabled}
        title={disabled ? "After this turn" : "Answer this again with another model, as a fork"}
        onClick={() => setOpen((v) => !v)}>
        retry with…
      </button>
      <BarMenu open={open} anchor={anchor} onClose={() => setOpen(false)}>
        <div className="model-menu retry-menu">
          <div className="mp-head">answer again with</div>
          {blocked
            ? <div className="mp-note">{blocked}</div>
            : main.map(([v, label]) => {
              const [alias, named] = label.includes(" · ") ? label.split(" · ") : [label, ""];
              const same = v !== "default" && findModel(models, v)?.resolved === answeredBy;
              return (
                <button key={v} role="menuitem" className="bm-item mp-row"
                  onClick={() => { setOpen(false); onRetry(v); }}>
                  <span className="mp-alias">{alias}</span>
                  {named && <span className="mp-named">{named}</span>}
                  {same && <span className="mp-named rt-same">wrote this one</span>}
                </button>
              );
            })}
          <div className="mp-note">
            {was ? `${was} wrote this reply. ` : ""}
            The retry is a fork: this conversation stays under Resume. Claude models only;
            other providers never see a conversation.
          </div>
        </div>
      </BarMenu>
    </span>
  );
}
