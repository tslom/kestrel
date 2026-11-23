import { useCallback, useRef } from "react";
import { fetchJSON, postJSON } from "./api";
import { useTicker } from "./poll";
import { useLoad } from "./refresh";

// Scheduled prompts' runner (app/schedules.py): once a minute, and on load,
// ask what's due, claim each, and open it as a new tab through `handOff`,
// the same door an open_tab brief comes in by, so the working-tab cap and
// the queue apply. A refused handoff (every tab slot taken) leaves the claim
// to lapse, and the prompt is offered again in five minutes.
//
// The tick runs while the page is hidden too: a window behind others or
// minimised is still a window that should run Sunday's digest.

const TICK_MS = 60_000;

export function useScheduledPrompts(handOff) {
  const busy = useRef(false);
  const ref = useRef(handOff);
  ref.current = handOff;
  const check = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const { due = [] } = await fetchJSON("/api/schedules/due");
      for (const d of due) {
        const { ok, schedule: s } = await postJSON(`/api/schedules/${d.id}/claim`, {});
        if (!ok || !s) continue;
        const res = ref.current({ mode: s.mode, project: s.project_path || null,
                                  brief: s.brief, from: null, model: s.model || null });
        if (res?.ok) await postJSON(`/api/schedules/${d.id}/ran`, { tab: res.id });
      }
    } catch { /* offline or restarting: the next tick asks again */ } finally {
      busy.current = false;
    }
  }, []);
  useLoad(check);
  useTicker(check, TICK_MS, { whenHidden: true });
}
