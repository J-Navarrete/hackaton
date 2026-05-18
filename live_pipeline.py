"""Streaming pipeline for live (HLS) sources.

Same event vocabulary as pipeline.py, with two additions:
  - "chunk_transcribed": {chunk_index, segments_count, t_end_seconds}
  - "claim_pending":     {claim}    # claim dict, before research starts
  - "live_status":       {pending, verified, skipped, chunks}

Reuses _research_and_judge_one from pipeline.py for parity with VOD path.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import AsyncIterator


def _stderr(msg: str) -> None:
    print(f"[live_pipeline] {msg}", file=sys.stderr, flush=True)

from anthropic import AsyncAnthropic

from pipeline import SOURCES_PATH, _research_and_judge_one
from steps.extractor import extract_claims
from steps.live_capture import resolve_stream_url, stream_chunks
from steps.researcher import load_sources
from steps.segment_classifier import classify_block
from steps.transcriber import transcribe_audio

BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"


def _event(type_: str, **data) -> dict:
    return {"type": type_, **data}


def _normalize_claim_text(text: str) -> str:
    """Lowercase + strip non-alphanumeric + collapse whitespace for dedup."""
    t = text.lower()
    t = re.sub(r"[^a-záéíóúüñ0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def stream_live_pipeline(
    url: str,
    *,
    session_id: str,
    model_size: str = "small",
    device: str = "auto",
    language: str = "es",
    chunk_seconds: int = 20,
    extract_every_seconds: int = 30,
    sources_path: Path = SOURCES_PATH,
    concurrency: int = 4,
    fallback_to_open_web: bool = True,
    replay_speed: float = 1.0,
    start_offset_seconds: float = 0.0,
) -> AsyncIterator[dict]:
    """Run the live fact-check pipeline, yielding events as the stream progresses.

    Transcribes rolling HLS audio chunks, periodically extracts claims from the
    accumulated transcript, and researches + judges them in parallel.
    """
    loop = asyncio.get_running_loop()
    today = date.today().isoformat()
    client = AsyncAnthropic()
    sources = load_sources(sources_path)
    allowed_domains = [s["domain"] if isinstance(s, dict) else s for s in sources]
    sem = asyncio.Semaphore(concurrency)

    _log = logging.getLogger(__name__)

    _stderr(f"pipeline starting with start_offset_seconds={start_offset_seconds}")

    # Log device that Whisper will use, so we can spot CUDA-vs-CPU surprises
    try:
        from steps.transcriber import _autodetect_device
        _stderr(f"pipeline starting. Whisper device autodetect: {_autodetect_device()} (model={model_size})")
    except Exception as e:
        _stderr(f"could not autodetect device: {e}")

    # --- Step 1: resolve stream ---
    yield _event("step_started", step=1, name="resolve")
    try:
        hls_url, metadata, is_live, http_headers = await loop.run_in_executor(None, resolve_stream_url, url)
    except Exception as e:
        err_msg = f"resolve: {type(e).__name__}: {e}"
        _log.warning("live_pipeline step=1 error for %s: %s", url, err_msg)
        yield _event("error", step=1, error=err_msg)
        return
    yield _event(
        "metadata_ready",
        metadata={
            **metadata,
            "is_live": is_live,
            "replay_speed": replay_speed if not is_live else None,
        },
    )
    yield _event("step_completed", step=1, name="resolve")

    # --- Step 2: capture + transcribe chunks ---
    out_dir = OUTPUTS_DIR / "live" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    yield _event("step_started", step=2, name="capture")

    # Rolling state
    all_segments: list[dict] = []
    unprocessed_segments: list[dict] = []
    seen_claim_texts: set[str] = set()
    pending_tasks: list[asyncio.Task] = []
    claim_counter = 0
    chunk_count = 0
    last_extract_time = time.monotonic()
    verified_count = 0
    skipped_count = 0
    pending_count = 0
    discarded_count = 0

    async def _bounded_research(claim: dict) -> dict:
        async with sem:
            return await _research_and_judge_one(
                client,
                claim,
                allowed_domains,
                today,
                fallback_to_open_web=fallback_to_open_web,
            )

    def _drain_done_tasks() -> list[dict]:
        """Return results from completed tasks without awaiting."""
        done_results = []
        still_running = []
        for t in pending_tasks:
            if t.done():
                exc = t.exception()
                if exc is not None:
                    done_results.append({"_error": str(exc)})
                else:
                    done_results.append(t.result())
            else:
                still_running.append(t)
        pending_tasks[:] = still_running
        return done_results

    try:
        readrate = None if is_live else replay_speed
        chunk_iter = stream_chunks(
            hls_url,
            out_dir,
            chunk_seconds=chunk_seconds,
            readrate=readrate,
            http_headers=http_headers,
            start_offset_seconds=start_offset_seconds if start_offset_seconds > 0 else None,
        )
        async for chunk_path in chunk_iter:
            chunk_index = chunk_count
            chunk_count += 1

            # Transcribe this chunk
            try:
                tail_prompt = None
                if all_segments:
                    recent_text = " ".join(
                        s["text"] for s in all_segments[-10:]
                    )
                    tail_prompt = recent_text[-200:] if len(recent_text) > 200 else recent_text or None

                transcript = await loop.run_in_executor(
                    None,
                    lambda cp=chunk_path, tp=tail_prompt: transcribe_audio(
                        cp,
                        model_size=model_size,
                        language=language,
                        device=device,
                        condition_on_previous_text=True,
                        initial_prompt=tp,
                    ),
                )
            except Exception as e:
                yield _event("warn", message=f"transcribe chunk {chunk_index} fallo: {type(e).__name__}: {e}")
                continue

            # Offset timestamps — add start_offset_seconds so timestamps are
            # absolute positions in the source (e.g. if start_offset=340 and
            # chunk_index=2, time_offset=380s into the source).
            time_offset = chunk_index * chunk_seconds + start_offset_seconds
            chunk_segments = []
            for seg in (transcript.get("segments") or []):
                adjusted = dict(seg)
                adjusted["start"] = round(seg["start"] + time_offset, 3)
                adjusted["end"] = round(seg["end"] + time_offset, 3)
                chunk_segments.append(adjusted)

            all_segments.extend(chunk_segments)
            unprocessed_segments.extend(chunk_segments)
            t_end = chunk_segments[-1]["end"] if chunk_segments else time_offset + chunk_seconds

            _stderr(f"chunk #{chunk_index} transcribed, {len(chunk_segments)} segments, buffer={len(unprocessed_segments)} unprocessed")
            yield _event(
                "chunk_transcribed",
                chunk_index=chunk_index,
                segments_count=len(chunk_segments),
                t_end_seconds=t_end,
            )

            # Extract claims if enough time has passed OR we have enough audio buffered
            now = time.monotonic()
            elapsed = now - last_extract_time
            audio_buffered = (
                unprocessed_segments[-1]["end"] - unprocessed_segments[0]["start"]
                if unprocessed_segments else 0
            )
            should_extract = unprocessed_segments and (
                elapsed >= extract_every_seconds or audio_buffered >= 40
            )
            _stderr(f"extract check: elapsed={elapsed:.1f}s audio_buf={audio_buffered:.1f}s should={should_extract}")
            if should_extract:
                last_extract_time = now
                segs_to_process = list(unprocessed_segments)
                unprocessed_segments.clear()

                block_text = " ".join(s["text"] for s in segs_to_process).strip()
                t_start = segs_to_process[0]["start"]
                t_end = segs_to_process[-1]["end"]
                _stderr(f"extracting from {len(segs_to_process)} segs ({len(block_text)} chars). classifying first...")
                classification = await loop.run_in_executor(
                    None,
                    lambda bt=block_text: classify_block(bt, video_metadata=metadata),
                )
                _stderr(f"classifier label={classification['label']} conf={classification.get('confidence')}")
                if classification["label"] != "editorial":
                    yield _event(
                        "block_discarded",
                        label=classification["label"],
                        reason=classification["reason"],
                        confidence=classification["confidence"],
                        t_start=t_start,
                        t_end=t_end,
                        text=block_text,
                    )
                    discarded_count += 1
                    yield _event(
                        "live_status",
                        pending=pending_count,
                        verified=verified_count,
                        skipped=skipped_count,
                        chunks=chunk_count,
                        discarded=discarded_count,
                    )
                    continue

                try:
                    new_claims = await loop.run_in_executor(
                        None,
                        lambda segs=segs_to_process: extract_claims(
                            transcript={"segments": segs},
                            video_metadata=metadata,
                            provider="claude",
                        ),
                    )
                    _stderr(f"extractor returned {len(new_claims)} claims")
                except Exception as e:
                    _stderr(f"extract_claims FAILED: {type(e).__name__}: {e}")
                    yield _event("warn", message=f"extract_claims fallo en chunk {chunk_index}: {type(e).__name__}: {e}")
                    new_claims = []

                for claim in new_claims:
                    norm = _normalize_claim_text(claim.get("claim", ""))
                    if norm in seen_claim_texts:
                        _stderr(f"claim duplicate skipped: {(claim.get('claim') or '')[:60]}")
                        continue
                    seen_claim_texts.add(norm)

                    claim_counter += 1
                    # Include session_id suffix so claim PKs are unique across
                    # multiple live sessions of the same video (sync, re-analysis).
                    claim = dict(claim, id=f"lc{claim_counter}_{session_id[:6]}")

                    pending_count += 1
                    _stderr(f"claim_pending lc{claim_counter}: {(claim.get('claim') or '')[:80]}")
                    yield _event("claim_pending", claim=claim)

                    task = asyncio.create_task(_bounded_research(claim))
                    pending_tasks.append(task)

            # Drain any completed research tasks
            for result in _drain_done_tasks():
                if "_error" in result:
                    _stderr(f"research task FAILED: {result['_error']}")
                    yield _event("warn", message=f"research task fallo: {result['_error']}")
                    pending_count = max(0, pending_count - 1)
                    continue

                if result.get("skipped"):
                    claim = result["claim"]
                    _stderr(f"claim skipped (no evidence): {(claim.get('claim') or '')[:60]}")
                    skipped_record = {
                        "id": claim["id"],
                        "claim": claim["claim"],
                        "speaker": claim.get("speaker"),
                        "claim_type": claim.get("claim_type"),
                        "t_start": claim.get("t_start"),
                        "t_end": claim.get("t_end"),
                        "segment_ids": claim.get("segment_ids", []),
                        "reason": "Sin evidencia concluyente ni en allowlist estricto ni en busqueda extendida",
                        "search_summary": result["research"].get("search_summary", ""),
                    }
                    pending_count = max(0, pending_count - 1)
                    skipped_count += 1
                    yield _event("claim_skipped", **skipped_record)
                else:
                    verdict = result["verdict"]
                    _stderr(f"verdict ready: {verdict.get('verdict')} for: {(verdict.get('claim') or '')[:60]}")
                    pending_count = max(0, pending_count - 1)
                    verified_count += 1
                    yield _event(
                        "claim_verdict_ready",
                        verdict=verdict,
                        research_evidence=result["research"].get("evidence") or [],
                    )

            yield _event(
                "live_status",
                pending=pending_count,
                verified=verified_count,
                skipped=skipped_count,
                chunks=chunk_count,
                discarded=discarded_count,
            )

    except asyncio.CancelledError:
        # Clean cancellation — fall through to final drain below
        pass
    except Exception as e:
        yield _event("error", error=f"live pipeline: {type(e).__name__}: {e}")

    # Drain remaining tasks
    if pending_tasks:
        done_results = await asyncio.gather(*pending_tasks, return_exceptions=True)
        for result in done_results:
            if isinstance(result, Exception):
                yield _event("warn", message=f"final research task fallo: {result}")
                pending_count = max(0, pending_count - 1)
                continue
            if result.get("skipped"):
                claim = result["claim"]
                skipped_record = {
                    "id": claim["id"],
                    "claim": claim["claim"],
                    "speaker": claim.get("speaker"),
                    "claim_type": claim.get("claim_type"),
                    "t_start": claim.get("t_start"),
                    "t_end": claim.get("t_end"),
                    "segment_ids": claim.get("segment_ids", []),
                    "reason": "Sin evidencia concluyente ni en allowlist estricto ni en busqueda extendida",
                    "search_summary": result["research"].get("search_summary", ""),
                }
                pending_count = max(0, pending_count - 1)
                skipped_count += 1
                yield _event("claim_skipped", **skipped_record)
            else:
                verdict = result["verdict"]
                pending_count = max(0, pending_count - 1)
                verified_count += 1
                yield _event(
                    "claim_verdict_ready",
                    verdict=verdict,
                    research_evidence=result["research"].get("evidence") or [],
                )

    yield _event(
        "live_status",
        pending=0,
        verified=verified_count,
        skipped=skipped_count,
        chunks=chunk_count,
        discarded=discarded_count,
    )
    yield _event("stream_end", status="stopped")
