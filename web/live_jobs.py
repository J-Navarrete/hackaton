"""In-memory registry + background runner for live-stream fact-check jobs.

Mirrors the structure of web/jobs.py but for live HLS pipelines.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from live_pipeline import stream_live_pipeline
from web.db import LiveSession, session_scope
from web.persist import (
    create_live_session,
    insert_claim_with_verdict,
    insert_skipped_claim,
    mark_live_stopped,
    update_live_session,
    upsert_video,
)


@dataclass
class LiveJob:
    id: str
    url: str
    status: str = "pending"  # pending | running | stopped | failed
    video_id: str | None = None
    events: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    error: str | None = None
    cancel_token: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    replay_speed: float = 1.0
    start_offset_seconds: float = 0.0

    async def emit(self, event: dict) -> None:
        self.events.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        # Replay history so late subscribers don't miss anything
        for ev in self.events:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)


class LiveRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, LiveJob] = {}

    def create(self, url: str, replay_speed: float = 1.0, start_offset_seconds: float = 0.0) -> LiveJob:
        job_id = uuid.uuid4().hex[:12]
        job = LiveJob(id=job_id, url=url, replay_speed=replay_speed, start_offset_seconds=start_offset_seconds)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> LiveJob | None:
        return self._jobs.get(job_id)


LIVE_REGISTRY = LiveRegistry()


async def run_live_job(job: LiveJob) -> None:
    """Async task: runs the live pipeline and persists each event to DB + job log."""
    job.status = "running"
    video_id: str | None = None

    # Create DB row
    with session_scope() as session:
        create_live_session(session, job.id, job.url)
        session.commit()

    try:
        async for ev in stream_live_pipeline(job.url, session_id=job.id, replay_speed=job.replay_speed, start_offset_seconds=job.start_offset_seconds):
            # Check if the job was cancelled externally
            if job.cancel_token.is_set():
                break

            await job.emit(ev)
            t = ev.get("type")

            if t == "metadata_ready":
                metadata = ev["metadata"]
                video_id = metadata.get("id")
                job.video_id = video_id
                with session_scope() as session:
                    if video_id:
                        upsert_video(session, job.url, metadata, force_platform="youtube_live")
                    update_live_session(
                        session,
                        job.id,
                        video_id=video_id,
                        title=metadata.get("title"),
                        channel=metadata.get("channel") or metadata.get("uploader"),
                        status="running",
                    )
                    session.commit()

            elif t == "claim_verdict_ready" and video_id:
                verdict = ev["verdict"]
                existing_sources = verdict.get("sources") or []
                if len(existing_sources) < 2:
                    existing_urls = {s["url"] for s in existing_sources if s.get("url")}
                    extra = [
                        {
                            "title": e["title"],
                            "url": e["url"],
                            "retrieved_date": e["retrieved_date"],
                            "excerpt": e.get("excerpt", ""),
                        }
                        for e in (ev.get("research_evidence") or [])
                        if isinstance(e, dict) and e.get("url") and e["url"] not in existing_urls
                    ]
                    verdict = dict(verdict, sources=existing_sources + extra)
                claim_dict = {
                    "id": verdict.get("id"),
                    "claim": verdict.get("claim"),
                    "speaker": verdict.get("speaker"),
                    "t_start": verdict.get("t_start"),
                    "t_end": verdict.get("t_end"),
                    "claim_type": verdict.get("claim_type"),
                    "rationale": None,
                }
                with session_scope() as session:
                    insert_claim_with_verdict(session, video_id, claim_dict, verdict)
                    session.commit()

            elif t == "claim_skipped" and video_id:
                with session_scope() as session:
                    insert_skipped_claim(session, video_id, ev)
                    session.commit()

            elif t == "block_discarded":
                with session_scope() as session:
                    ls = session.get(LiveSession, job.id)
                    if ls is not None:
                        ls.discarded_count = (ls.discarded_count or 0) + 1
                        session.commit()

            elif t == "live_status":
                with session_scope() as session:
                    update_live_session(
                        session,
                        job.id,
                        pending_count=ev.get("pending", 0),
                        verified_count=ev.get("verified", 0),
                        skipped_count=ev.get("skipped", 0),
                        discarded_count=ev.get("discarded", 0),
                    )
                    session.commit()

            elif t == "error":
                job.error = ev.get("error")

        job.status = "failed" if job.error else "stopped"

    except asyncio.CancelledError:
        job.status = "stopped"
        # Don't re-raise; just clean up below
    except Exception as e:
        job.error = f"{type(e).__name__}: {e}"
        job.status = "failed"
        await job.emit({"type": "error", "error": job.error})

    finally:
        final_status = job.status
        with session_scope() as session:
            mark_live_stopped(session, job.id, status=final_status)
            session.commit()
        await job.emit({"type": "stream_end", "status": final_status, "video_id": job.video_id, "error": job.error})
