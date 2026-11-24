"""Auto-reindex: rebuild index.db when vault markdown changes. No incremental
updates: watchfiles debounces bursts into one batch answered by the same full
rebuild() as startup and POST /api/reindex. Runs as a lifespan task beside the poller.
"""

import asyncio
from pathlib import Path

from watchfiles import awatch

from . import indexer
from .config import TASKS_VIEW_FILE, VAULT_PATH


def _relevant(change, raw_path: str) -> bool:
    path = Path(raw_path)
    if path.suffix.lower() != ".md":
        return False
    try:
        rel = path.resolve().relative_to(VAULT_PATH)
    except ValueError:
        return False
    if any(part.startswith(".") for part in rel.parts):
        return False  # .obsidian, .claude, .trash — same skip rule as rebuild()
    return str(rel) != TASKS_VIEW_FILE  # our own generated view; never re-ingest


async def watch_loop() -> None:
    async for changes in awatch(VAULT_PATH, watch_filter=_relevant):
        try:
            info = await asyncio.to_thread(indexer.rebuild)
            print(f"[watcher] vault changed → reindexed {info['indexed_notes']} notes")
        except Exception as e:
            print(f"[watcher] reindex failed: {e}")
