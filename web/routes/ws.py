"""WebSocket endpoint that streams pipeline events to the browser."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from web.jobs import REGISTRY


router = APIRouter()


@router.websocket("/ws/job/{job_id}")
async def job_stream(websocket: WebSocket, job_id: str):
    await websocket.accept()
    job = REGISTRY.get(job_id)
    if not job:
        await websocket.send_json({"type": "error", "error": "job not found"})
        await websocket.close()
        return

    queue = job.subscribe()
    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                # keepalive ping
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
