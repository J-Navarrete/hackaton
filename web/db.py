"""SQLAlchemy models + session factory.

Schema:
  videos   (1)---(N) claims (1)---(1) verdicts
                            (1)---(N) votes
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "outputs" / "politicheck.db"


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String, default="other")
    title: Mapped[str] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    uploader: Mapped[str | None] = mapped_column(String, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    upload_date: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    claims: Mapped[list["Claim"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Claim.t_start"
    )


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    local_id: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(String, nullable=True)
    t_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    t_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_type: Mapped[str | None] = mapped_column(String, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="claims")
    verdict: Mapped["Verdict | None"] = relationship(
        back_populates="claim", uselist=False, cascade="all, delete-orphan"
    )
    votes: Mapped[list["Vote"]] = relationship(
        back_populates="claim", cascade="all, delete-orphan"
    )


class Verdict(Base):
    __tablename__ = "verdicts"

    claim_id: Mapped[str] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    verdict: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    correction: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list] = mapped_column(JSON, default=list)
    judged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    claim: Mapped[Claim] = relationship(back_populates="verdict")


class Vote(Base):
    """One vote per (claim_id, user_id). Updates overwrite."""

    __tablename__ = "votes"

    claim_id: Mapped[str] = mapped_column(
        ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    vote_type: Mapped[str] = mapped_column(String)  # "acuerdo" | "desacuerdo" | "no-se"
    user_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    claim: Mapped[Claim] = relationship(back_populates="votes")


_engine = None
_SessionLocal = None


def _ensure_engine():
    global _engine, _SessionLocal
    if _engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH.as_posix()}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine, _SessionLocal


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session and closes it."""
    _, SessionLocal = _ensure_engine()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def session_scope() -> Session:
    """Direct session for non-request contexts (e.g. background tasks). Caller closes."""
    _, SessionLocal = _ensure_engine()
    return SessionLocal()
