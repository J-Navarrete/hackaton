"""HTML pages: home (list of videos) + detail (player + claims + voting)."""
from __future__ import annotations

import secrets
from collections import Counter

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from web.db import Claim, Video, Vote, get_session


router = APIRouter()


def _ensure_user_id(request: Request) -> str:
    uid = request.session.get("uid")
    if not uid:
        uid = secrets.token_hex(8)
        request.session["uid"] = uid
    return uid


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    _ensure_user_id(request)
    videos = (
        session.query(Video).order_by(desc(Video.analyzed_at)).limit(50).all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "home.html",
        {"videos": videos},
    )


@router.get("/analyze", response_class=HTMLResponse)
def analyze_form(request: Request):
    _ensure_user_id(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "analyze.html", {})


@router.get("/v/{video_id}", response_class=HTMLResponse)
def video_detail(
    video_id: str, request: Request, session: Session = Depends(get_session)
):
    uid = _ensure_user_id(request)
    video = session.get(Video, video_id)
    if not video:
        return RedirectResponse("/", status_code=302)

    claims_with_verdict = [c for c in video.claims if not c.skipped]
    skipped = [c for c in video.claims if c.skipped]

    tally_by_verdict: Counter[str] = Counter()
    for c in claims_with_verdict:
        if c.verdict:
            tally_by_verdict[c.verdict.verdict] += 1

<<<<<<< HEAD
    seen_source_urls: set[str] = set()
    all_sources: list[dict] = []
    for c in claims_with_verdict:
        if c.verdict:
            for s in c.verdict.sources:
                url = s.get("url", "")
                if url and url not in seen_source_urls:
                    seen_source_urls.add(url)
                    all_sources.append(s)

=======
>>>>>>> c52ca7a4c3418b51214353c8a145d9a5cfc4dac6
    vote_tallies = {}
    user_votes = {}
    for c in video.claims:
        per_claim = Counter()
        for v in c.votes:
            per_claim[v.vote_type] += 1
            if v.user_id == uid:
                user_votes[c.id] = v
        vote_tallies[c.id] = per_claim

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "video.html",
        {
            "video": video,
            "verdicts": claims_with_verdict,
            "skipped": skipped,
            "tally": tally_by_verdict,
<<<<<<< HEAD
            "all_sources": all_sources,
=======
>>>>>>> c52ca7a4c3418b51214353c8a145d9a5cfc4dac6
            "vote_tallies": vote_tallies,
            "user_votes": user_votes,
            "user_id": uid,
        },
    )
