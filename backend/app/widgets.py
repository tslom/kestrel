"""REST API. Tasks and habits are DB-truth (app.db); tasks.md is a generated
view, regenerated after every task mutation (db's task-write hook, which
vault_views registers — so no route here has to remember to)."""

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import commands, courses, db, indexer, sessions, vault_views

router = APIRouter(prefix="/api")


# ---------- tasks (app.db is truth) ----------

class TaskIn(BaseModel):
    title: str
    # `due` is a day key; `due_time` is 'HH:MM' local, or NULL for all-day.
    # `due` may also arrive as "YYYY-MM-DDTHH:MM" — db.split_due separates it.
    due: str | None = None
    due_time: str | None = None
    priority: int | None = None
    project: str | None = None
    source: str = "ui"
    defer: str | None = None
    note: str | None = None
    waiting_on: str | None = None
    owed_to: str | None = None
    parent_id: int | None = None
    source_kind: str | None = None
    source_ref: str | None = None
    source_excerpt: str | None = None


class StatusIn(BaseModel):
    status: str  # open | waiting | someday | done | dropped
    waiting_on: str | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    due: str | None = None
    due_time: str | None = None
    priority: int | None = None
    project: str | None = None
    note: str | None = None
    defer: str | None = None
    waiting_on: str | None = None
    owed_to: str | None = None
    parent_id: int | None = None


@router.get("/tasks")
def get_tasks(status: str | None = "open"):
    return db.list_tasks(status=status)


@router.get("/tasks/today")
def get_tasks_today():
    return db.tasks_today()


@router.post("/tasks")
def post_task(body: TaskIn):
    try:
        task = db.create_task(**body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return task


@router.patch("/tasks/{task_id}")
def patch_task(task_id: int, body: TaskPatch):
    try:
        task = db.update_task(task_id, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@router.post("/tasks/{task_id}/status")
def post_task_status(task_id: int, body: StatusIn):
    task = db.set_task_status(task_id, body.status, waiting_on=body.waiting_on)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


# ---------- habits (app.db is truth) ----------

class ProposalIn(BaseModel):
    approve: bool


class HabitIn(BaseModel):
    name: str
    cadence: str = "daily"
    target: float = 1
    unit: str | None = None


class CheckinIn(BaseModel):
    value: float = 1
    day: str | None = None  # defaults to day_key() (4am rollover)
    note: str | None = None


class HabitPatch(BaseModel):
    name: str | None = None
    archived: bool | None = None


@router.get("/habits")
def get_habits():
    return {"day": db.day_key(), "habits": db.list_habits()}


@router.get("/habits/history")
def get_habits_history(days: int = 7):
    return db.habit_history(days=max(1, min(days, 31)))


@router.post("/habits")
def post_habit(body: HabitIn):
    return db.create_habit(**body.model_dump())


@router.patch("/habits/{habit_id}")
def patch_habit(habit_id: int, body: HabitPatch):
    habit = db.update_habit(habit_id, **body.model_dump(exclude_unset=True))
    if habit is None:
        raise HTTPException(status_code=404, detail="no such habit")
    return habit


@router.delete("/habits/{habit_id}")
def delete_habit(habit_id: int):
    """Permanent, and takes the check-in history with it — the reversible
    option is PATCH archived=true, which the card offers first."""
    habit = db.delete_habit(habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="no such habit")
    return {"ok": True, "deleted": habit}


@router.post("/habits/{habit_id}/checkin")
def post_checkin(habit_id: int, body: CheckinIn):
    db.checkin(habit_id, day=body.day, value=body.value, note=body.note)
    return {"ok": True, "day": body.day or db.day_key()}


# ---------- sessions / generated inbox / index ----------

@router.get("/sessions")
def get_sessions(project: str | None = None):
    return sessions.list_sessions(cwd=_project_cwd(project))


@router.get("/sessions/search")
def search_sessions(q: str, limit: int = 50, project: str | None = None):
    return sessions.search_sessions(q, limit=limit, cwd=_project_cwd(project))


def _project_cwd(project: str | None):
    """A dev tab's repo, for the session routes; None is the vault."""
    if not project:
        return None
    from . import console_profiles
    try:
        return console_profiles.resolve_project(project)
    except console_profiles.ProfileError as e:
        raise HTTPException(404, str(e))


@router.get("/sessions/{session_id}/transcript")
def get_session_transcript(session_id: str, project: str | None = None,
                           until: int | None = None):
    return {"entries": sessions.session_transcript(session_id, cwd=_project_cwd(project),
                                                   until=until)}


@router.get("/sessions/{session_id}/digest")
def get_session_digest(session_id: str, project: str | None = None):
    """A past session, small enough to attach to a message (#94)."""
    from . import session_digest
    out = session_digest.digest(session_id, cwd=_project_cwd(project))
    if out is None:
        raise HTTPException(404, "no transcript for that session yet")
    return out


@router.post("/sessions/{session_id}/title")
async def post_session_title(session_id: str, project: str | None = None):
    """A tab title read off the whole session, not its first message (#102).
    One Haiku turn; only on a click."""
    from . import agent, session_digest
    try:
        title = await session_digest.title(session_id, cwd=_project_cwd(project))
    except agent.AgentError as e:
        raise HTTPException(503, str(e))
    if not title:
        raise HTTPException(404, "no transcript for that session yet")
    return {"title": title}


@router.get("/sessions/{session_id}/subagent")
def get_session_subagent(session_id: str, tool: str, project: str | None = None):
    """The calls of the subagent that Agent call `tool` started (#77)."""
    return {"run": sessions.subagent_run(session_id, tool, cwd=_project_cwd(project))}


@router.get("/sessions/{session_id}/rewind")
def get_session_rewind(session_id: str, message: str, project: str | None = None):
    """The resume point that drops `message` and everything after it."""
    out = sessions.rewind_point(session_id, message, cwd=_project_cwd(project))
    if not out["found"]:
        raise HTTPException(404, "that message isn't in this session's transcript yet")
    return out


@router.get("/commands")
def get_commands():
    return commands.scan_commands()


@router.get("/courses")
def get_courses(term: str | None = None):
    """The week's hours, plus any amendments read out of mail and waiting on
    you. Folded into this one payload rather than given its own route —
    the card draws them together, and a proposal means nothing without the
    hours it would change."""
    from .providers import email_extract

    out = courses.load(term)
    try:
        out["proposals"] = email_extract.pending_oh_proposals()
    except sqlite3.Error:
        out["proposals"] = []   # pre-v48 DB, or the table dropped: no proposals
    # Study mode: an exam inside two weeks, with the review items logged for
    # it. Read off syllabus text a parser can misread, so a failure here costs
    # the line, never the hours.
    try:
        from . import study
        out["study"] = study.desk(out["term"])
    except Exception:
        out["study"] = []
    return out


@router.post("/courses/proposals/{proposal_id}")
def post_courses_proposal(proposal_id: int, body: ProposalIn):
    """Approve a proposed amendment — which writes the dated override to the
    vault note — or reject it. Nothing reached the vault before this call."""
    from .providers import email_extract

    try:
        return email_extract.decide_oh_proposal(proposal_id, body.approve)
    except KeyError:
        raise HTTPException(404, "no such proposal")
    except courses.CourseError as e:
        # The proposal stays pending: it failed to apply, so it's still
        # waiting, and saying "approved" would hide that the note is unchanged.
        raise HTTPException(409, str(e))


@router.post("/courses/proposals/{proposal_id}/undo")
def post_courses_proposal_undo(proposal_id: int):
    """Undo a decision: an approved override leaves the vault note again, and
    the proposal goes back to pending."""
    from .providers import email_extract

    try:
        return email_extract.undecide_oh_proposal(proposal_id)
    except KeyError:
        raise HTTPException(404, "no such proposal")
    except courses.CourseError as e:
        raise HTTPException(409, str(e))


@router.get("/generated")
def get_generated():
    return vault_views.list_generated_unreviewed()


class PromoteIn(BaseModel):
    path: str
    dest_dir: str | None = None
    curated: bool = False


@router.post("/generated/promote")
def post_promote(body: PromoteIn):
    try:
        result = vault_views.promote_generated(body.path, body.dest_dir, body.curated)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    indexer.rebuild()
    return result


@router.post("/reindex")
def reindex():
    return indexer.rebuild()


# ---------- open a note in Obsidian ----------

class OpenIn(BaseModel):
    target: str


@router.post("/vault/open")
def post_vault_open(body: OpenIn):
    """Hand a note to Obsidian. The frontend links optimistically — anything
    shaped like a vault path becomes a link — so a miss here is ordinary, not
    an error worth logging; 404 is the answer the link renders as "not in the
    vault"."""
    try:
        return vault_views.open_in_obsidian(body.target)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ---------- notes: search, one note, pinned (read index.db only) ----------

@router.get("/vault/search")
def get_vault_search(q: str = "", limit: int = 20):
    return {"q": q, "notes": vault_views.search_notes(q, max(1, min(limit, 100)))}


@router.get("/vault/note")
def get_vault_note(path: str):
    try:
        return vault_views.note_detail(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.get("/vault/recent")
def get_vault_recent(folder: str = "", limit: int = 30):
    return {"folder": folder, "notes": vault_views.recent_notes(folder, max(1, min(limit, 200)))}


@router.get("/vault/pinned")
def get_vault_pinned():
    return vault_views.pinned_notes()
