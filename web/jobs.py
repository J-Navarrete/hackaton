"""In-memory job registry + background pipeline runner.

A "job" is one invocation of the streaming pipeline. Events from the pipeline
are fanned out to subscribers (currently: WebSocket connections) via asyncio
queues. Job state is volatile (lost on server restart) but persisted artifacts
(audio, transcripts, claims, verdicts, DB rows) survive.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from pipeline import stream_pipeline
from web.db import session_scope
from web.persist import insert_claim_with_verdict, insert_skipped_claim, replace_claims, upsert_video


@dataclass
class Job:
    id: str
    url: str
    status: str = "pending"  # pending | running | completed | failed
    video_id: str | None = None
    events: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    error: str | None = None

    async def emit(self, event: dict) -> None:
        self.events.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        for ev in self.events:  # replay history so late subscribers don't miss anything
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, url: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, url=url)
        self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


REGISTRY = JobRegistry()


async def run_job(job: Job, **pipeline_kwargs) -> None:
    """Async task that runs the pipeline and persists each event to DB + job log."""
    job.status = "running"
    video_id: str | None = None

    try:
        async for ev in stream_pipeline(job.url, **pipeline_kwargs):
            await job.emit(ev)

            t = ev.get("type")
            if t == "metadata_ready":
                metadata = ev["metadata"]
                video_id = metadata.get("id")
                job.video_id = video_id
                # Persist video immediately; clean prior claims if re-analysis
                with session_scope() as session:
                    upsert_video(session, job.url, metadata)
                    if video_id:
                        replace_claims(session, video_id)
                    session.commit()

            elif t == "claim_verdict_ready" and video_id:
                verdict = ev["verdict"]
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

            elif t == "error":
                job.error = ev.get("error")

        job.status = "failed" if job.error else "completed"

    except Exception as e:
        job.error = f"{type(e).__name__}: {e}"
        job.status = "failed"
        await job.emit({"type": "error", "error": job.error})

    finally:
        # Notify subscribers that no more events are coming
        await job.emit({"type": "stream_end", "status": job.status, "video_id": job.video_id})
