"""One errand, or N versions of one task across providers.

Each agent gets the brief and a file name to hand back (`artifact`), and writes it in
its scratch dir `<data>/delegates/<batch>/v<n>/` (the only place an outside agent can
write) or replies with the contents. Claude goes through `agent.ask` (no tools, one
turn), the others `boundary.launch`; all get the scrubbed brief so the comparison is fair.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import agent
from ..config import DATA_DIR, DEFAULT_MODEL
from . import accounts, boundary, registry, store

MAX_VARIANTS = 8
PARALLEL = 4
RUN_LIMIT_S = 10 * 60
ARTIFACT_RE = re.compile(r"[A-Za-z0-9][\w.-]{0,60}")
DEFAULT_ARTIFACT = "result.md"
BATCH_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")

HANDBACK = ("\n\n---\nYou have this brief and nothing else: an empty working directory, "
            "no other files, nobody to ask. Make the calls yourself. Hand the result back "
            "as a file named `{artifact}` in the current directory, or reply with exactly "
            "that file's contents and nothing around them.")
ANGLE = "\n\nThis is one of several independent takes. Yours: {angle}"
DISTINCT = ("\n\nThis is take {n} of {of}, each made independently. Commit to one clear "
            "direction of your own instead of the safest middle.")


class DelegateError(RuntimeError):
    """Refused before anything ran. Written to be shown to Claude."""


@dataclass(frozen=True)
class Send:
    """The work (`purpose`, boundary.PURPOSES), whose it is (`work_for`: which accounts may take it), its tab."""
    purpose: str | None = None
    work_for: str | None = None
    origin: boundary.Origin | None = None


async def _clear(task: str, names: tuple[str, ...], send: Send) -> None:
    """The gate, once on the whole task before anything runs (scrub with a purpose runs boundary.gate)."""
    if send.work_for not in boundary.WORK_FOR:
        raise DelegateError(f"work_for must be one of: {', '.join(boundary.WORK_FOR)} "
                            "(commercial: any business's or client's work)")
    await asyncio.to_thread(boundary.scrub, task, names, purpose=send.purpose or "",
                            origin=send.origin)


def root() -> Path:
    return DATA_DIR / "delegates"


def variant_dir(batch: str, variant: int) -> Path:
    if not BATCH_RE.fullmatch(batch):
        raise DelegateError(f"no batch '{batch}'")
    return root() / batch / f"v{int(variant)}"


def _new_batch() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def artifact_name(name: str | None) -> str:
    name = (name or "").strip() or DEFAULT_ARTIFACT
    if not ARTIFACT_RE.fullmatch(name):
        raise DelegateError("artifact is a plain file name, like hero.html or notes.md")
    return name


_FENCE = re.compile(r"\A\s*```[\w+-]*\n(.*?)\n```\s*\Z", re.S)


def unfence(text: str) -> str:
    """A reply that is one fenced block is the file's contents with a fence
    around them."""
    m = _FENCE.match(text)
    return m.group(1) if m else text


# Appended for agents with caps: told the limits (agy once spent 50 steps and 5 minutes on a
# business card), it plans, and a capped run still leaves the file.
LIMITS = (" You have at most {turns} steps and {minutes} minutes; past either you are stopped "
          "and only what is already in `{artifact}` is kept. Write it early and improve it in "
          "place. Don't build tooling or screenshot your work to check it.")


def _compose(task: str, artifact: str, angle: str | None, n: int, of: int,
             max_turns: int = 0) -> str:
    extra = ANGLE.format(angle=angle) if angle else DISTINCT.format(n=n, of=of) if of > 1 else ""
    limits = LIMITS.format(turns=max_turns, minutes=RUN_LIMIT_S // 60, artifact=artifact) \
        if max_turns else ""
    return task.strip() + extra + HANDBACK.format(artifact=artifact) + limits


def _why_failed(done: boundary.Finished) -> str:
    if done.code is None:
        return f"no answer in {RUN_LIMIT_S // 60} minutes; stopped"
    # codex --json says why on stdout: turn.failed {"error":{"message"}} or error {"message"}.
    for ev in reversed(list(registry.json_lines(done.stdout))):
        err = ev.get("error") if isinstance(ev.get("error"), dict) else ev
        if ev.get("type") in ("turn.failed", "error") and isinstance(err.get("message"), str):
            return err["message"][:300]
    lines = [ln.strip() for ln in (done.stderr or done.stdout).splitlines() if ln.strip()]
    return (lines[-1][:300] if lines else "") or f"exited {done.code} with nothing said"


async def _run_one(batch: str, n: int, of: int, kind: str, provider_id: str, task: str,
                   names: tuple[str, ...], artifact: str, model: str | None,
                   angle: str | None, session_id: str | None, send: Send = Send(),
                   account: dict | None = None, via: str = "") -> dict:
    p = registry.PROVIDERS[provider_id]
    brief = boundary.scrub(_compose(task, artifact, angle, n, of, p.max_turns), names,
                           purpose=(send.purpose or "") if p.outside else None, origin=send.origin)
    folder = variant_dir(batch, n)
    folder.mkdir(parents=True, exist_ok=True)
    model = model or (DEFAULT_MODEL if not p.outside else None)
    if not model and account and p.home_model:
        # Name what it would pick anyway, so the row records what ran.
        model = await asyncio.to_thread(p.home_model, Path(account["home"]))
    run_id = await asyncio.to_thread(store.start_run, batch, n, kind, p.id, model, angle,
                                     brief.redactions, session_id,
                                     account["id"] if account else None)
    out = {"variant": n, "provider": p.id, "model": model, "angle": angle,
           "status": "failed", "error": None, "file": None, "text": "",
           "redactions": list(brief.redactions),
           "account": account["label"] if account else None, "via": via}
    began = time.monotonic()
    reply = registry.Reply("")
    cut = None
    # An account's run uses its own home; the sandbox opens only that.
    home = Path(account["home"]) if account else None
    env_set = (*p.env_set, *p.home_env(home)) if home and p.home_env else p.env_set
    config = home or p.config_dir
    try:
        if p.outside:
            done = await boundary.launch(
                lambda text: p.argv(text, folder, model), brief, folder,
                env_keys=p.env_keys, env_set=env_set, config_dirs=(config,) if config else (),
                stdin_brief=p.stdin_brief, timeout=RUN_LIMIT_S,
                ends_turn=p.ends_turn, max_turns=p.max_turns)
            if done.cut:
                cut = (f"cut off after {p.max_turns} turns" if done.cut == "turns"
                       else f"cut off at {RUN_LIMIT_S // 60} minutes")
            if done.code == 0 or done.cut:
                reply = p.parse(done.stdout, folder)
            else:
                out["error"] = (p.explain and p.explain(done.stdout, done.stderr)) or _why_failed(done)
        else:
            with agent.interactive():
                reply = registry.Reply(await agent.ask(brief.text, model=model))
    except (boundary.BoundaryError, agent.AgentError, OSError) as e:
        out["error"] = str(e)[:300]
    shutil.rmtree(folder / ".tmp", ignore_errors=True)

    target = folder / artifact
    text = ""
    if target.is_file() and target.stat().st_size:
        try:
            text = target.read_text()
        except UnicodeDecodeError:
            text = ""                       # an image: the file is the result
        out["status"] = "ok"
    elif reply.text.strip():
        text = unfence(reply.text.strip())
        target.write_text(text)
        out["status"] = "ok"
    out["files"] = sorted(f.name for f in folder.iterdir()
                          if f.is_file() and not f.name.startswith(".") and f.name != artifact)
    # A run cut off mid-way often has its first files written.
    left = f"; it left {', '.join(out['files'])} in {folder}" if out["files"] else ""
    if out["status"] != "ok" and cut:
        out["error"] = f"{cut} before it wrote {artifact}" + (left or ", with nothing to hand back yet")
    elif out["status"] != "ok" and not out["error"]:
        out["error"] = "it finished without a reply or the file" + left
    elif out["status"] == "ok":
        # A capped run keeps what it had; the error column says so.
        out.update(file=str(target), text=text,
                   error=f"{cut}: this is what it had" if cut else None)
    out["cut_off"] = cut
    out["model"] = reply.model or model
    out["duration_ms"] = int((time.monotonic() - began) * 1000)
    await asyncio.to_thread(
        store.finish_run, run_id, status=out["status"], error=out["error"],
        file=artifact if out["status"] == "ok" else None, chars=len(text),
        tokens_in=reply.tokens_in, tokens_out=reply.tokens_out, tokens_cached=reply.tokens_cached,
        duration_ms=out["duration_ms"], model=out["model"])
    return out


def _needs(rows: list[dict], provider_id: str) -> str:
    row = next((r for r in rows if r["id"] == provider_id), None)
    if row is None:
        return f"no provider '{provider_id}'; there are: {', '.join(r['id'] for r in rows)}"
    return f"{row['label']} isn't ready: {row['needs']}"


async def _accounts_for(pids, work_for: str | None) -> dict[str, tuple[dict | None, str]]:
    """The account each outside provider runs this on (accounts.pick)."""
    out = {}
    for pid in dict.fromkeys(pids):
        if registry.PROVIDERS[pid].outside:
            try:
                out[pid] = await asyncio.to_thread(accounts.pick, pid, work_for)
            except accounts.AccountError as e:
                raise DelegateError(str(e)) from None
    return out


async def delegate(provider_id: str, brief: str, *, artifact: str | None = None,
                   model: str | None = None, session_id: str | None = None,
                   send: Send = Send()) -> dict:
    """Send one outside agent on one errand."""
    rows = await asyncio.to_thread(registry.status)
    row = next((r for r in rows if r["id"] == provider_id), None)
    if row is None or not row["ready"]:
        ready = [r["id"] for r in rows if r["ready"] and r["outside"]]
        raise DelegateError(_needs(rows, provider_id) + (
            f". Ready now: {', '.join(ready)}" if ready else
            ". No outside provider is ready, so do this one yourself"))
    if not row["outside"]:
        raise DelegateError("delegate is for the other providers; this one is you")
    names = await asyncio.to_thread(store.contact_names)
    await _clear(brief, names, send)
    name = artifact_name(artifact)
    account, via = (await _accounts_for([provider_id], send.work_for))[provider_id]
    batch = _new_batch()
    out = await _run_one(batch, 1, 1, "delegate", provider_id, brief, names,
                         name, model, None, session_id, send, account, via)
    return {"batch": batch, **out}


def plan_mix(ready: list[str], n: int | None, mix: list[str] | None) -> list[str]:
    """Who makes each variant. A given mix is used as is; otherwise the ready
    providers take turns, Claude first."""
    if mix:
        return [m.strip().lower() for m in mix]
    order = [p for p in registry.PROVIDERS if p in ready]
    return [order[i % len(order)] for i in range(n or 3)] if order else []


async def fan_out(task: str, *, n: int | None = None, mix: list[str] | None = None,
                  angles: list[str] | None = None, artifact: str | None = None,
                  claude_model: str | None = None, session_id: str | None = None,
                  models: dict[str, str | None] | None = None, send: Send = Send()) -> dict:
    """N versions of one task side by side. `models`: an outside provider's model id, else its own.
    A mix with an outside provider passes the gate first; Claude-only batches don't."""
    rows = await asyncio.to_thread(registry.status)
    ready = [r["id"] for r in rows if r["ready"]]
    who = plan_mix(ready, n, mix)
    if not who:
        raise DelegateError("no provider is ready, not even Claude's CLI")
    if len(who) > MAX_VARIANTS:
        raise DelegateError(f"{MAX_VARIANTS} variants is the most in one batch")
    for pid in dict.fromkeys(who):
        if pid not in ready:
            raise DelegateError(_needs(rows, pid) + f". Ready now: {', '.join(ready)}")
    angles = [a.strip() for a in angles or [] if a and a.strip()]
    if len(angles) > len(who):
        raise DelegateError(f"{len(angles)} angles for {len(who)} variants")
    name = artifact_name(artifact)
    names = await asyncio.to_thread(store.contact_names)
    if any(registry.PROVIDERS[pid].outside for pid in who):
        await _clear(task, names, send)
    on = await _accounts_for(who, send.work_for)
    batch = _new_batch()
    gate = asyncio.Semaphore(PARALLEL)

    async def one(i: int, pid: str) -> dict:
        account, via = on.get(pid, (None, ""))
        async with gate:
            return await _run_one(
                batch, i + 1, len(who), "fan_out", pid, task, names, name,
                claude_model if pid == "claude" else (models or {}).get(pid),
                angles[i] if i < len(angles) else None, session_id, send, account, via)

    variants = await asyncio.gather(*(one(i, pid) for i, pid in enumerate(who)))
    waiting = [f"{r['label']}: {r['needs']}" for r in rows if r["outside"] and not r["ready"]]
    return {"batch": batch, "artifact": name, "variants": list(variants), "not_ready": waiting}
