import { useEffect, useState } from "react";
import { fetchJSON } from "../lib/api.js";
import { useTicker, useVisiblePoll } from "../lib/poll.js";
import { autoResumeOn, eventWindows, planResume, readAuto, windowState as stateOf,
         writeAuto } from "../lib/autoResume.js";

// A turn the usage limit cut off (backend result.limited): says when the window reopens; Resume sends
// "continue" into the same session, and waits for the window (a closed one just cuts it off again).
// With "Resume tabs automatically" (Settings, default) the banner resumes itself after the reset
// (lib/autoResume.js), off-screen tabs too (every banner stays mounted); "Don't" leaves it for a click.

// The polled windows alone (PlanApp's "Resume all" row, which has no tab).
export function windowState(usage) {
  return stateOf(usage?.windows || []);
}

function useUsage() {
  const [usage, setUsage] = useState(null);
  const load = () => fetchJSON("/api/console/usage").then(setUsage).catch(() => {});
  useEffect(() => { load(); }, []);
  useVisiblePoll(load, 60_000, { catchUp: true });
  return usage;
}

export function useUsageWindow() {
  return windowState(useUsage());
}

const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

// `limited`: the tab's record ({ at, manual }); `rateLimits`: its socket's events; `model`: a per-model
// week binds only that model. `onAuto` sends the resume as the tab and says whether it went; `onDecline` marks it left alone.
export default function LimitBanner({ tab, limited, rateLimits, model, onResume, onAuto, onDecline }) {
  const usage = useUsage();
  const windows = [...(usage?.windows || []), ...eventWindows(rateLimits)];
  const { open, resetsAt } = stateOf(windows, { model });
  const on = autoResumeOn();
  const plan = planResume({ limited, windows, model, tab, auto: readAuto(tab), on });
  // A clock that runs while hidden; the plan is anchored to fixed times, so a late tick fires late.
  const [, tick] = useState(0);
  useTicker(() => {
    tick((n) => n + 1);
    if (!plan || Date.now() < plan.at) return;
    if (!onAuto?.()) return;            // not connected or mid-turn: the next tick tries
    const was = readAuto(tab);
    writeAuto(tab, { last: Date.now(), tries: (was.tries || 0) + 1 });
  }, 20_000, { whenHidden: true });

  const when = plan && (plan.at - Date.now() < 60_000 ? "in a moment" : `at ${clock(plan.at)}`);
  const auto = plan ? `the tab resumes automatically ${when}` : null;
  const said = !open && resetsAt ? `It resets at ${clock(resetsAt)}${auto ? `; ${auto}` : ""}.`
    : open ? `The window is open again${auto ? `; ${auto}` : ""}.`
      : auto ? `${auto[0].toUpperCase()}${auto.slice(1)}.` : "";
  return (
    <div className="limit-banner" role="status">
      <span className="limit-text">The usage limit stopped this turn mid-work. {said}</span>
      {plan && (
        <button className="limit-decline" onClick={onDecline}
          title="Leave this tab for a click on Resume">don't</button>
      )}
      <button className="limit-resume" onClick={onResume} disabled={!open}
              title={open ? "Send a 'continue' into this conversation" : "Waits for the window to reset"}>
        Resume
      </button>
    </div>
  );
}
