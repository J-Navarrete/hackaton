"""Routes for the live-stream fact-check feature.

Page routes:   GET  /live, GET /live/{live_id}
API routes:    POST /api/live/start, POST /api/live/stop/{live_id}, GET /api/live/{live_id}
WebSocket:     WS   /ws/live/{live_id}
"""
from __future__ import annotations

import asyncio
from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from web.db import LiveSession, Video, get_session
from web.live_jobs import LIVE_REGISTRY, run_live_job
from web.routes.pages import _ensure_user_id

# ---------------------------------------------------------------------------
# Two routers: one for pages + WebSocket, one for API endpoints
# ---------------------------------------------------------------------------
router = APIRouter()
api_router = APIRouter()


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@router.get("/live", response_class=HTMLResponse)
def live_index(request: Request, session: Session = Depends(get_session)):
    _ensure_user_id(request)
    sessions = (
        session.query(LiveSession)
        .order_by(desc(LiveSession.started_at))
        .limit(20)
        .all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "live.html", {"sessions": sessions})


@router.get("/live/{live_id}", response_class=HTMLResponse)
def live_session_detail(
    live_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    uid = _ensure_user_id(request)
    live = session.get(LiveSession, live_id)
    if not live:
        return RedirectResponse("/live", status_code=302)

    # In-memory job has the start_offset_seconds (not persisted in DB)
    job = LIVE_REGISTRY.get(live_id)
    live_start_offset = float(job.start_offset_seconds) if job else 0.0

    video = session.get(Video, live.video_id) if live.video_id else None

    verdicts: list = []
    skipped: list = []
    tally: Counter = Counter()
    all_sources: list = []
    vote_tallies: dict = {}
    user_votes: dict = {}

    if video:
        verdicts = [c for c in video.claims if not c.skipped]
        skipped = [c for c in video.claims if c.skipped]

        for c in verdicts:
            if c.verdict:
                tally[c.verdict.verdict] += 1

        seen_source_urls: set[str] = set()
        for c in verdicts:
            if c.verdict:
                for s in c.verdict.sources:
                    url = s.get("url", "")
                    if url and url not in seen_source_urls:
                        seen_source_urls.add(url)
                        all_sources.append(s)

        for c in video.claims:
            per_claim: Counter = Counter()
            for v in c.votes:
                per_claim[v.vote_type] += 1
                if v.user_id == uid:
                    user_votes[c.id] = v
            vote_tallies[c.id] = per_claim

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "live_session.html",
        {
            "live": live,
            "video": video,
            "verdicts": verdicts,
            "skipped": skipped,
            "tally": tally,
            "all_sources": all_sources,
            "vote_tallies": vote_tallies,
            "user_votes": user_votes,
            "user_id": uid,
            "live_start_offset": live_start_offset,
        },
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/ws/live/{live_id}")
async def live_stream(websocket: WebSocket, live_id: str):
    await websocket.accept()
    job = LIVE_REGISTRY.get(live_id)
    if not job:
        await websocket.send_json({"type": "error", "error": "live job not found"})
        await websocket.close()
        return

    queue = job.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(ev)
            if ev.get("type") == "stream_end":
                break
    except WebSocketDisconnect:
        pass
    finally:
        job.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# API routes (mounted with prefix /api in app.py)
# ---------------------------------------------------------------------------

_VALID_REPLAY_SPEEDS = {1.0, 2.0, 4.0}


@api_router.post("/live/start")
async def live_start(url: str = Form(...), replay_speed: float = Form(1.0)):
    if not url.strip():
        raise HTTPException(status_code=400, detail="url vacia")
    # replay_speed is no longer user-controlled; the live_session UI now handles
    # playback speed via the YouTube IFrame API. Always use 1.0 so ffmpeg uses -re.
    job = LIVE_REGISTRY.create(url.strip(), replay_speed=1.0)
    task = asyncio.create_task(run_live_job(job))
    job.task = task
    return {"live_id": job.id, "status": "running"}


@api_router.post("/live/stop/{live_id}")
async def live_stop(live_id: str):
    job = LIVE_REGISTRY.get(live_id)
    if not job:
        raise HTTPException(status_code=404, detail="live job no encontrado")
    if job.task and not job.task.done():
        job.task.cancel()
    return {"status": "stopping"}


@api_router.post("/live/sync/{live_id}")
async def live_sync(live_id: str, seconds: float = Form(...)):
    """Cancel the current live job and restart it from `seconds` into the source."""
    old_job = LIVE_REGISTRY.get(live_id)
    if not old_job:
        raise HTTPException(status_code=404, detail="job no existe")
    # Cancel the old job's task. The finally block in stream_chunks kills ffmpeg.
    if old_job.task is not None and not old_job.task.done():
        old_job.task.cancel()
    # Create a new job with the same URL and the new offset
    new_job = LIVE_REGISTRY.create(
        old_job.url,
        replay_speed=old_job.replay_speed,
        start_offset_seconds=max(0.0, float(seconds)),
    )
    new_job.task = asyncio.create_task(run_live_job(new_job))
    return {"live_id": new_job.id, "status": "running", "start_offset_seconds": new_job.start_offset_seconds}


@api_router.get("/live/detect")
async def live_detect(url: str):
    """Quick metadata probe so the UI can decide whether to show the replay-speed selector."""
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="url vacia")
    try:
        from steps.live_capture import resolve_stream_url
        # resolve_stream_url is sync; run in threadpool
        import anyio
        hls_url, metadata, is_live, _headers = await anyio.to_thread.run_sync(resolve_stream_url, url.strip())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"no se pudo resolver: {type(e).__name__}: {e}")
    return {
        "is_live": bool(is_live),
        "title": metadata.get("title"),
        "channel": metadata.get("channel") or metadata.get("uploader"),
        "duration": metadata.get("duration"),
    }


@api_router.get("/live/{live_id}")
async def live_status(live_id: str):
    job = LIVE_REGISTRY.get(live_id)
    if not job:
        raise HTTPException(status_code=404, detail="live job no encontrado")
    return {
        "id": job.id,
        "status": job.status,
        "video_id": job.video_id,
        "error": job.error,
        "events": job.events[-30:],
    }
