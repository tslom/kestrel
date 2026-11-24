import { useRef, useState } from "react";
import Icon from "../ui/icons.jsx";
import { fetchJSON, postJSON } from "../lib/api.js";
import { useTicker } from "../lib/poll.js";
import { broadcastRefresh, useWidgetData } from "../lib/refresh.js";
import { prSummary, prTone } from "../lib/prWatch.js";

// A dev tab's row: the repo's .kestrel/project.json scripts (app/console_project.py) and its watched PR
// (lib/prWatch.js); keyed by project and worktree. Scripts run in the worktree on its own port (click
// starts/stops; output in the view pane's Tasks via the "runs" event); "preview" opens the pane on
// previewUrl, and autoOpenPreview does so once a script this tab started is listening.

const ICON = { run: "play", test: "flask", lint: "sparkle", build: "hammer" };
const enc = encodeURIComponent;

export default function ProjectBar({ project, worktree, emit, prw, menu = false, parts = "both", onDone }) {
  const url = `/api/console/project?project=${enc(project)}${worktree ? `&worktree=${enc(worktree)}` : ""}`;
  // Runs alive at first load predate this mount: their server coming up isn't an ask.
  const known = useRef(null);
  const opened = useRef(new Set());
  const [err, setErr] = useState(null);

  function publish(d) {
    const live = d.runs.filter((r) => r.running);
    if (known.current == null) known.current = new Set(live.map((r) => r.id));
    emit({ type: "runs", items: d.runs, url: d.preview_url });
    const fresh = live.find((r) => r.script !== "setup" && !known.current.has(r.id) && !opened.current.has(r.id));
    if (fresh && d.listening && d.config?.autoOpenPreview && d.preview_url) {
      opened.current.add(fresh.id);
      emit({ type: "preview", url: d.preview_url });
    }
    return d;
  }
  // The bar's copy and ⋯'s are two mounts: a click in either reloads both.
  const topic = `project-runs:${(worktree || project).replace(/\s/g, "_")}`;
  const { data, reload } = useWidgetData(() => fetchJSON(url).then(publish), topic);
  const running = (data?.runs || []).filter((r) => r.running);
  // Fast while a server comes up, slower once serving.
  useTicker(reload, running.length ? (data.listening ? 10000 : 2000) : null);

  if (!data || (!data.config && !data.error && !prw?.pr && !prw?.error)) return null;

  async function toggle(script) {
    setErr(null);
    const live = running.find((r) => r.script === script.id);
    try {
      if (live) await postJSON("/api/console/project/stop", { id: live.id });
      else await postJSON("/api/console/project/run", { project, worktree, script: script.id });
    } catch (e) { setErr(e.message); }
    broadcastRefresh(topic);
    onDone?.();
  }

  const cls = menu ? "bm-item" : "bar-btn";
  const scripts = data.config?.scripts || [];
  const setup = (data.runs || []).findLast((r) => r.script === "setup");
  const pr = prw?.pr;
  // On the bar the scripts and the chip fold separately, the scripts first:
  // the chip is live state, which the bar keeps longest. ⋯ shows what folded.
  const showScripts = parts !== "pr";
  const showPr = parts !== "scripts";
  return (
    <span className={menu ? "bm-acts pb-menu" : "pb-row"}>
      {showScripts && <span className="bar-fold" data-cut={menu ? undefined : "scripts"}>
      {data.error && (
        <span className={`${cls} pb-warn`} title={data.error}>project.json ⚠</span>
      )}
      {setup && (setup.running || setup.code) ? (
        <span className={`${cls} pb-setup${setup.code ? " bad" : ""}`}
          title={`${setup.command}\n\n${(setup.output || "").trim().split("\n").slice(-6).join("\n")}`}>
          {setup.running ? "setting up…" : `setup exited ${setup.code}`}
        </span>
      ) : null}
      {scripts.map((s) => {
        const live = running.some((r) => r.script === s.id);
        return (
          <button key={s.id} className={`${cls} pb-script${live ? " on" : ""}`}
            role={menu ? "menuitem" : undefined} onClick={() => toggle(s)}
            title={`${live ? "Stop" : "Run"} ${s.command}  (in this tab's worktree, PORT=${data.port})`}>
            <Icon name={live ? "stop" : ICON[s.icon] || "play"} size={11} />
            <span data-cut={menu ? undefined : "scripts-short"}>{s.name}</span>
          </button>
        );
      })}
      {data.preview_url && (
        <button className={`${cls} pb-preview`} role={menu ? "menuitem" : undefined}
          onClick={() => { emit({ type: "preview", url: data.preview_url }); onDone?.(); }}
          title={`Open ${data.preview_url} in the preview pane${data.listening ? "" : " (nothing is listening there yet)"}`}>
          preview{data.listening ? "" : " ·"}
        </button>
      )}
      </span>}
      {showPr && pr && (
        <span className="bar-fold" data-cut={menu ? undefined : "pr"}>
        <a className={`${cls} pr-chip tone-${prTone(pr)}`} href={pr.url} target="_blank"
          rel="noopener noreferrer" role={menu ? "menuitem" : undefined}
          title={`${pr.title}\n${pr.head} → ${pr.base}${prw.watching
            ? "\nWatching: this tab wakes when checks, reviews, comments or conflicts change" : ""}`}>
          <Icon name="pullRequest" size={11} />
          <span>#{pr.number}</span>
          {prSummary(pr) && <span className="pr-sum" data-cut={menu ? undefined : "scripts-short"}>{prSummary(pr)}</span>}
        </a>
        </span>
      )}
      {err && <span className={`${cls} pb-warn`} title={err}>⚠</span>}
    </span>
  );
}
