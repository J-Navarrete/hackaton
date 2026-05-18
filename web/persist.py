"""Persist pipeline outputs to the SQLAlchemy database.

These helpers are called by jobs.py as the streaming pipeline emits events.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from web.db import Claim, LiveSession, Verdict, Video


_PLATFORM_PATTERNS = [
    (re.compile(r"youtube\.com/shorts/", re.I), "youtube_short"),
    (re.compile(r"(youtube\.com|youtu\.be)", re.I), "youtube"),
    (re.compile(r"tiktok\.com", re.I), "tiktok"),
    (re.compile(r"instagram\.com", re.I), "instagram"),
    (re.compile(r"facebook\.com|fb\.watch", re.I), "facebook"),
    (re.compile(r"(twitter\.com|x\.com)", re.I), "twitter"),
]


def detect_platform(url: str) -> str:
    for pat, name in _PLATFORM_PATTERNS:
        if pat.search(url):
            return name
    return "other"


def build_embed_html(video: Video) -> str | None:
    """Build a self-contained embed HTML snippet. Only YouTube supports timestamp deep-links."""
    if video.platform in ("youtube", "youtube_short", "youtube_live"):
        return (
            f'<iframe id="yt-player" '
            f'data-video-id="{video.id}" '
            f'src="https://www.youtube.com/embed/{video.id}?rel=0&enablejsapi=1" '
            f'frameborder="0" '
            f'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
            f'referrerpolicy="strict-origin-when-cross-origin" '
            f'allowfullscreen></iframe>'
        )
    return None  # TikTok/IG render as deep-links in the template


def upsert_video(session: Session, url: str, metadata: dict, force_platform: str | None = None) -> Video:
    video_id = metadata.get("id")
    if not video_id:
        raise ValueError("metadata must contain 'id'")

    existing = session.get(Video, video_id)
    platform = force_platform if force_platform is not None else detect_platform(url)
    fields = dict(
        url=url,
        platform=platform,
        title=metadata.get("title") or "(sin titulo)",
        channel=metadata.get("channel"),
        uploader=metadata.get("uploader"),
        duration=metadata.get("duration"),
        upload_date=metadata.get("upload_date"),
        description=metadata.get("description"),
    )

    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        existing.analyzed_at = datetime.utcnow()
        existing.embed_html = build_embed_html(existing)
        session.flush()
        return existing

    video = Video(id=video_id, **fields)
    video.embed_html = build_embed_html(video)
    session.add(video)
    session.flush()
    return video


def replace_claims(session: Session, video_id: str) -> None:
    """Wipe existing claims for a video before re-persisting (re-analysis case).

    Cascade deletes verdicts and votes too. If preserving votes across re-analyses
    becomes important, this needs a different strategy.
    """
    for claim in (
        session.query(Claim).filter(Claim.video_id == video_id).all()
    ):
        session.delete(claim)
    session.flush()


def insert_claim_with_verdict(
    session: Session, video_id: str, claim: dict, verdict: dict | None
) -> Claim:
    local_id = claim["id"]
    pk = f"{video_id}_{local_id}"
    db_claim = Claim(
        id=pk,
        video_id=video_id,
        local_id=local_id,
        text=claim["claim"],
        speaker=claim.get("speaker"),
        t_start=claim.get("t_start"),
        t_end=claim.get("t_end"),
        claim_type=claim.get("claim_type"),
        rationale=claim.get("rationale"),
        skipped=False,
    )
    session.add(db_claim)
    if verdict:
        session.add(
            Verdict(
                claim_id=pk,
                verdict=verdict.get("verdict") or "",
                confidence=float(verdict.get("confidence") or 0),
                correction=verdict.get("correction") or "",
                sources=verdict.get("sources") or [],
            )
        )
    session.flush()
    return db_claim


def insert_skipped_claim(session: Session, video_id: str, skipped: dict) -> Claim:
    local_id = skipped["id"]
    pk = f"{video_id}_{local_id}"
    db_claim = Claim(
        id=pk,
        video_id=video_id,
        local_id=local_id,
        text=skipped["claim"],
        speaker=skipped.get("speaker"),
        t_start=skipped.get("t_start"),
        t_end=skipped.get("t_end"),
        claim_type=skipped.get("claim_type"),
        skipped=True,
        skipped_reason=skipped.get("reason"),
        search_summary=skipped.get("search_summary"),
    )
    session.add(db_claim)
    session.flush()
    return db_claim


# ---------------------------------------------------------------------------
# LiveSession helpers
# ---------------------------------------------------------------------------

_LIVE_ALLOWED_FIELDS = {
    "video_id", "title", "channel", "status",
    "ended_at", "pending_count", "verified_count", "skipped_count", "discarded_count",
}


def create_live_session(session: Session, live_id: str, url: str) -> LiveSession:
    ls = LiveSession(id=live_id, url=url, status="starting")
    session.add(ls)
    session.flush()
    return ls


def update_live_session(session: Session, live_id: str, **fields) -> "LiveSession | None":
    ls = session.get(LiveSession, live_id)
    if ls is None:
        return None
    for k, v in fields.items():
        if k in _LIVE_ALLOWED_FIELDS:
            setattr(ls, k, v)
    session.flush()
    return ls


def mark_live_stopped(session: Session, live_id: str, status: str = "stopped") -> None:
    ls = session.get(LiveSession, live_id)
    if ls:
        ls.status = status
        ls.ended_at = datetime.utcnow()
        session.flush()
