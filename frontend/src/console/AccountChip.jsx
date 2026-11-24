import { useRef, useState } from "react";
import BarMenu from "./BarMenu.jsx";
import { formatResetIn } from "./UsageChips.jsx";
import { pctOf, shortName, useAccounts, windowOf } from "../lib/claudeAccounts.js";

// The console bar's Claude account (app/claude_accounts.py): this tab's account, and a switch menu with
// each account's 5h/weekly usage. Silent under two accounts. A switch reaches only a CLI that starts
// after it: when the active account leaves `sessionAccount` (start frame) an idle tab reconnects and
// resumes on the new one; a busy tab finishes its turn first and the next poll moves it.

export default function AccountChip({ sessionAccount, busyRef, onReconnect }) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState(null);
  const moved = useRef(null);     // the account a reconnect was already asked for
  const { data, error, act } = useAccounts({
    onData: (d) => {
      const now = d.active?.uuid;
      if (now === sessionAccount) moved.current = null;
      if (!sessionAccount || !now || now === sessionAccount || busyRef.current) return;
      if (moved.current === now) return;
      moved.current = now;
      onReconnect();
    },
  });
  const accounts = data?.accounts || [];
  if (accounts.length < 2) return null;
  const active = accounts.find((a) => a.active);
  const stopped = !!data.refusal;
  const title = stopped ? `Stopped: ${data.refusal}`
    : `${active?.label || "No account"} · ${data.mode === "hold" ? "picked by hand" : "auto"} · click to switch`;
  return (
    <>
      <button ref={setAnchor} data-cut="account"
        className={`bar-btn acct-chip${open ? " on" : ""}${stopped ? " bad" : ""}`}
        aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)} title={title}>
        {shortName(active?.label) || "account"}{data.mode === "hold" && <span className="acct-hold"> · held</span>}
      </button>
      <BarMenu open={open} anchor={anchor} onClose={() => setOpen(false)}>
        <div className="model-menu acct-menu">
          <div className="mp-head">Claude account</div>
          {accounts.map((a) => (
            <button key={a.uuid} role="menuitemradio" aria-checked={a.active}
              className={`bm-item acct-row${a.active ? " on" : ""}`} disabled={a.active}
              onClick={() => act(`/${a.uuid}/use`).then(() => setOpen(false))}
              title={a.why ? `Outside its rules: ${a.why}` : a.active ? "In use" : "Use this account (rules off until auto)"}>
              <span className="acct-name">{shortName(a.label)}</span>
              {["five_hour", "seven_day"].map((k) => {
                const w = windowOf(a, k);
                return (
                  <span key={k} className="acct-pct" title={w ? formatResetIn(w.resets_at) || undefined : "no reading yet"}>
                    {k === "five_hour" ? "5h" : "7d"} {pctOf(w)}
                  </span>
                );
              })}
              <span className={`acct-why${a.why ? " out" : ""}`}>{a.active ? "in use" : a.why ? "out" : ""}</span>
            </button>
          ))}
          {data.mode === "hold" && (
            <button className="bm-item acct-auto" onClick={() => act("/auto").then(() => setOpen(false))}>
              Back to auto: the rules pick
            </button>
          )}
          {(stopped || error) && <div className="mp-note bad">{error || `Stopped: ${data.refusal}`}</div>}
          <div className="mp-note">Rules and new accounts are in Settings → Claude accounts.</div>
        </div>
      </BarMenu>
    </>
  );
}
