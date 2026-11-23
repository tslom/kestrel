"""Study attempts — where the console's quizzes and flashcards report back.

StudyBlock.jsx posts a block's answered items on "Send results"; coverage is
read back by the console's `study_coverage` tool (app/study.py). Results still
go to the model as a drafted message too — this is the durable copy.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import study

router = APIRouter(prefix="/api/study", tags=["study"])


class Attempts(BaseModel):
    block: str
    course: str | None = None
    items: list[dict]


@router.post("/attempts")
def attempts(body: Attempts):
    try:
        return {"kept": study.record_attempts(body.block, body.course, body.items[:100])}
    except ValueError as e:
        raise HTTPException(400, str(e))
