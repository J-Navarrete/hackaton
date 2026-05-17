"""JSON API: trigger analysis, submit votes, query job status."""
from __future__ import annotations

import asyncio
from collections import Counter

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from web.db import Claim, Vote, get_session
from web.jobs import REGISTRY, run_job
from web.routes.pages import _ensure_user_id


router = APIRouter()


_VALID_VOTE_TYPES = {"acuerdo", "desacuerdo", "no-se"}
_VALID_USER_VERDICTS = {"Exacto", "Parcialmente exacto", "Inexacto", "Ridiculo"}


@router.post("/analyze")
async def analyze(url: str = Form(...), language: str | None = Form("es")):
    if not url.strip():
        raise HTTPException(status_code=400, detail="url vacia")
    job = REGISTRY.create(url.strip())
    asyncio.create_task(run_job(job, language=language or None))
    return {"job_id": job.id, "status": "running"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = REGISTRY.get(job_id)
    if not job:
        raise HTTPException(status_code=404)
    return {
        "id": job.id,
        "status": job.status,
        "video_id": job.video_id,
        "error": job.error,
        "events": job.events[-30:],  # last 30 events
    }


@router.post("/vote/{claim_id}", response_class=HTMLResponse)
async def vote(
    claim_id: str,
    request: Request,
    vote_type: str = Form(...),
    user_verdict: str | None = Form(None),
    reasoning: str | None = Form(None),
    session: Session = Depends(get_session),
):
    uid = _ensure_user_id(request)
    if vote_type not in _VALID_VOTE_TYPES:
        raise HTTPException(status_code=400, detail="vote_type invalido")
    if user_verdict and user_verdict not in _VALID_USER_VERDICTS:
        raise HTTPException(status_code=400, detail="user_verdict invalido")

    claim = session.get(Claim, claim_id)
    if not claim:
        raise HTTPException(status_code=404, detail="claim no existe")

    existing = (
        session.query(Vote)
        .filter(Vote.claim_id == claim_id, Vote.user_id == uid)
        .one_or_none()
    )
    if existing:
        existing.vote_type = vote_type
        existing.user_verdict = user_verdict if vote_type == "desacuerdo" else None
        existing.reasoning = reasoning if vote_type == "desacuerdo" else None
    else:
        session.add(
            Vote(
                claim_id=claim_id,
                user_id=uid,
                vote_type=vote_type,
                user_verdict=user_verdict if vote_type == "desacuerdo" else None,
                reasoning=reasoning if vote_type == "desacuerdo" else None,
            )
        )
    session.commit()
    session.refresh(claim)

    tally = Counter(v.vote_type for v in claim.votes)
    user_vote = next((v for v in claim.votes if v.user_id == uid), None)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/vote_tally.html",
        {
            "claim": claim,
            "tally": tally,
            "user_vote": user_vote,
        },
    )
