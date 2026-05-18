"""Streaming async orchestrator for the PolitiCheck pipeline.

Yields events as each step completes. Consumed by the CLI (main.py) and
by the web app via WebSocket. Each event is a dict with a `type` field.

Pipeline (8 steps):
  1. download        - yt-dlp grabs audio + metadata
  2. prompter        - Claude generates a priming prompt for Whisper
  3. transcribe      - faster-whisper produces segmented transcript
  4. transcript_edit - Claude fixes obvious phonetic errors (optional)
  5. extract_claims  - Claude extracts verifiable claims as JSON
  6. research_judge  - per-claim (parallel): tiered web_search + verdict
  7. report          - HTML autocontenido

Event types:
  - "step_started":         {step, name, ...}
  - "step_completed":       {step, name, ...specifics}
  - "metadata_ready":       {metadata}
  - "claim_verdict_ready":  {verdict}            # streams in completion order
  - "claim_skipped":        {id, claim, reason, search_summary, ...}
  - "warn":                 {message}
  - "error":                {error}
  - "completed":            {report_path, verdicts_count, skipped_count, ...}

The research + verdict stages run in parallel via asyncio.as_completed, so the
first verdict to surface is the fastest, not the first in the claim list.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import AsyncIterator

from anthropic import AsyncAnthropic

from steps.downloader import download_audio, get_video_metadata
from steps.extractor import extract_claims
from steps.prompter import generate_initial_prompt
from steps.reporter import write_report
from steps.researcher import _research_one_tiered, load_sources
from steps.transcriber import transcribe_audio
from steps.transcript_editor import post_edit_transcript
from steps.verdicts import _judge_one


BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
AUDIO_DIR = OUTPUTS_DIR / "audio"
TRANSCRIPTS_DIR = OUTPUTS_DIR / "transcripts"
CLAIMS_DIR = OUTPUTS_DIR / "claims"
RESEARCH_DIR = OUTPUTS_DIR / "research"
VERDICTS_DIR = OUTPUTS_DIR / "verdicts"
REPORTS_DIR = OUTPUTS_DIR / "reports"
SOURCES_PATH = BASE_DIR / "sources.json"

TOTAL_STEPS = 7


def _event(type_: str, **data) -> dict:
    return {"type": type_, **data}


def _propagate_source_tier(verdict_record: dict, research: dict) -> None:
    """Match verdict.sources back to research.evidence by URL and copy source_tier."""
    url_to_tier: dict[str, str] = {}
    for e in research.get("evidence") or []:
        if isinstance(e, dict) and e.get("url"):
            tier = e.get("source_tier")
            if tier:
                url_to_tier[e["url"]] = tier
    for s in verdict_record.get("sources") or []:
        if isinstance(s, dict) and s.get("url") in url_to_tier:
            s["source_tier"] = url_to_tier[s["url"]]


async def _research_and_judge_one(
    client: AsyncAnthropic,
    claim: dict,
    allowed_domains: list[str],
    today: str,
    research_model: str = "claude-sonnet-4-6",
    verdict_model: str = "claude-sonnet-4-6",
    max_searches: int = 5,
    max_tokens_research: int = 4000,
    max_tokens_verdict: int = 4000,
    fallback_to_open_web: bool = True,
    verdict_provider: str = "claude",
) -> dict:
    research = await _research_one_tiered(
        client,
        claim,
        allowed_domains,
        research_model,
        today,
        max_searches,
        max_tokens_research,
        fallback_to_open_web=fallback_to_open_web,
    )
    if not research.get("verifiable"):
        return {"claim": claim, "research": research, "verdict": None, "skipped": True}

    verdict_record = await _judge_one(
        client, claim, research, verdict_model, max_tokens_verdict, provider=verdict_provider
    )
    _propagate_source_tier(verdict_record, research)
    # Add a flag to the verdict so the UI can show "extended search used"
    if research.get("fallback_used"):
        verdict_record["fallback_used"] = True
    return {"claim": claim, "research": research, "verdict": verdict_record, "skipped": False}


async def stream_pipeline(
    url: str,
    *,
    language: str | None = None,
    model_size: str = "large-v3",
    device: str = "auto",
    initial_prompt_override: str | None = None,
    skip_initial_prompt: bool = False,
    skip_transcript_edit: bool = False,
    sources_path: Path = SOURCES_PATH,
    concurrency: int = 5,
    fallback_to_open_web: bool = True,
    llm_provider: str = "claude",
) -> AsyncIterator[dict]:
    """Run the full fact-check pipeline, yielding events as each step progresses.

    Blocking work (yt-dlp, faster-whisper, file I/O) is dispatched to a thread
    pool via run_in_executor so the event loop stays responsive. Claude calls
    are already async.
    """
    loop = asyncio.get_running_loop()

    # [1/7] Download
    yield _event("step_started", step=1, name="download", url=url)
    try:
        metadata = await loop.run_in_executor(None, get_video_metadata, url)
    except Exception as e:
        yield _event("error", step=1, error=f"metadata: {type(e).__name__}: {e}")
        return
    yield _event("metadata_ready", metadata=metadata)
    try:
        audio_path = await loop.run_in_executor(None, download_audio, url, AUDIO_DIR)
    except Exception as e:
        yield _event("error", step=1, error=f"download: {type(e).__name__}: {e}")
        return
    yield _event("step_completed", step=1, name="download", audio_path=str(audio_path))

    # [2/7] Prompter
    yield _event("step_started", step=2, name="prompter")
    initial_prompt: str | None = None
    if skip_initial_prompt:
        pass
    elif initial_prompt_override:
        initial_prompt = initial_prompt_override
    else:
        try:
            initial_prompt = await loop.run_in_executor(
                None,
                lambda: generate_initial_prompt(
                    title=metadata.get("title") or "",
                    channel=metadata.get("channel") or metadata.get("uploader"),
                    description=metadata.get("description"),
                    tags=metadata.get("tags"),
                    provider=llm_provider,
                ),
            )
            initial_prompt = initial_prompt or None
        except Exception as e:
            yield _event("warn", message=f"prompter fallo: {e}")
            initial_prompt = None
    yield _event("step_completed", step=2, name="prompter", initial_prompt=initial_prompt)

    if llm_provider not in ("claude", "minimax"):
        yield _event("warn", message=f"llm_provider desconocido '{llm_provider}', usando 'claude'")
        llm_provider = "claude"

    # [3/7] Transcribe
    yield _event("step_started", step=3, name="transcribe", model=model_size)
    try:
        transcript = await loop.run_in_executor(
            None,
            lambda: transcribe_audio(
                audio_path,
                model_size=model_size,
                language=language,
                device=device,
                initial_prompt=initial_prompt,
            ),
        )
    except Exception as e:
        yield _event("error", step=3, error=f"transcribe: {type(e).__name__}: {e}")
        return

    yield _event(
        "step_completed",
        step=3,
        name="transcribe",
        segments=len(transcript.get("segments", [])),
        duration=transcript.get("duration"),
        device=transcript.get("device"),
        compute_type=transcript.get("compute_type"),
        language=transcript.get("language"),
    )

    # [4/7] Transcript post-edit with Claude (fixes phonetic errors)
    if skip_transcript_edit:
        yield _event("step_started", step=4, name="transcript_edit", skipped=True)
        yield _event("step_completed", step=4, name="transcript_edit", skipped=True)
    else:
        yield _event("step_started", step=4, name="transcript_edit")
        try:
            transcript = await loop.run_in_executor(
                None,
                lambda: post_edit_transcript(transcript=transcript, video_metadata=metadata, provider=llm_provider),
            )
            pe = transcript.get("post_edit") or {}
            yield _event(
                "step_completed",
                step=4,
                name="transcript_edit",
                n_corrected=pe.get("n_segments_corrected", 0),
                n_total=pe.get("n_segments_total", len(transcript.get("segments", []))),
                summary=pe.get("changes_summary", ""),
            )
        except Exception as e:
            yield _event("warn", message=f"transcript_edit fallo: {type(e).__name__}: {e}. Continuo con transcript original.")

    # Persist transcript (post-edited version if it ran)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPTS_DIR / f"{audio_path.stem}.json"
    transcript_payload = {"video": metadata, "transcript": transcript}
    transcript_path.write_text(
        json.dumps(transcript_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # [5/7] Extract claims
    yield _event("step_started", step=5, name="extract_claims")
    try:
        claims = await loop.run_in_executor(
            None, lambda: extract_claims(transcript=transcript, video_metadata=metadata, provider=llm_provider)
        )
    except Exception as e:
        yield _event("error", step=5, error=f"extract: {type(e).__name__}: {e}")
        return

    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    claims_path = CLAIMS_DIR / f"{audio_path.stem}.json"
    claims_path.write_text(
        json.dumps(
            {
                "video": metadata,
                "transcript_source": str(transcript_path.relative_to(BASE_DIR)),
                "claims": claims,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    yield _event("step_completed", step=5, name="extract_claims", claims=claims)

    # [6/7] Research + Verdict in parallel (tiered: strict allowlist, fallback to open web)
    sources = load_sources(sources_path)
    allowed_domains = [s["domain"] if isinstance(s, dict) else s for s in sources]
    yield _event(
        "step_started",
        step=6,
        name="research_and_judge",
        n_claims=len(claims),
        n_sources=len(sources),
        fallback_enabled=fallback_to_open_web,
    )

    research_results: list[dict] = []
    verdicts: list[dict] = []
    skipped: list[dict] = []

    if claims:
        today = date.today().isoformat()
        client = AsyncAnthropic()
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(claim: dict) -> dict:
            async with sem:
                return await _research_and_judge_one(
                    client, claim, allowed_domains, today,
                    fallback_to_open_web=fallback_to_open_web,
                    verdict_provider=llm_provider,
                )

        tasks = [asyncio.create_task(_bounded(c)) for c in claims]
        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
            except Exception as e:
                yield _event("warn", message=f"claim fallo: {type(e).__name__}: {e}")
                continue
            research_results.append(result["research"])
            if result["skipped"]:
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
                skipped.append(skipped_record)
                yield _event("claim_skipped", **skipped_record)
            else:
                verdicts.append(result["verdict"])
                yield _event(
                    "claim_verdict_ready",
                    verdict=result["verdict"],
                    research_evidence=result["research"].get("evidence") or [],
                )

    # Persist research + verdicts
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    research_path = RESEARCH_DIR / f"{audio_path.stem}.json"
    research_path.write_text(
        json.dumps(
            {
                "video": metadata,
                "claims_source": str(claims_path.relative_to(BASE_DIR)),
                "sources_used": sources,
                "fallback_enabled": fallback_to_open_web,
                "research": research_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    verdicts_path = VERDICTS_DIR / f"{audio_path.stem}.json"
    verdicts_payload = {
        "video": metadata,
        "research_source": str(research_path.relative_to(BASE_DIR)),
        "verdicts": verdicts,
        "skipped_claims": skipped,
    }
    verdicts_path.write_text(
        json.dumps(verdicts_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    fallback_count = sum(1 for v in verdicts if v.get("fallback_used"))
    yield _event(
        "step_completed",
        step=6,
        name="research_and_judge",
        verdicts=len(verdicts),
        skipped=len(skipped),
        used_fallback=fallback_count,
    )

    # [7/7] Report
    yield _event("step_started", step=7, name="report")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{audio_path.stem}.html"
    await loop.run_in_executor(
        None, lambda: write_report(verdicts_payload, report_path, sources=sources)
    )
    yield _event("step_completed", step=7, name="report", report_path=str(report_path))

    yield _event(
        "completed",
        video_id=audio_path.stem,
        report_path=str(report_path),
        verdicts_path=str(verdicts_path),
        verdicts_count=len(verdicts),
        skipped_count=len(skipped),
        fallback_count=fallback_count,
    )


def _print_event(ev: dict) -> None:
    t = ev["type"]
    if t == "step_started":
        step = ev.get("step")
        name = ev.get("name")
        extra = ""
        if name == "transcribe":
            extra = f" ({ev.get('model')})"
        elif name == "research_and_judge":
            extra = f" ({ev.get('n_claims')} claims en {ev.get('n_sources')} fuentes oficiales, fallback={'on' if ev.get('fallback_enabled') else 'off'})"
        elif name == "transcript_edit" and ev.get("skipped"):
            extra = " (skipped)"
        print(f"[{step}/{TOTAL_STEPS}] {name}{extra}...")
    elif t == "metadata_ready":
        m = ev["metadata"]
        print(f"      titulo  : {m.get('title')}")
        print(f"      canal   : {m.get('channel') or m.get('uploader')}")
        print(f"      duracion: {m.get('duration')}s")
    elif t == "step_completed":
        name = ev.get("name")
        if name == "download":
            print(f"      audio   : {ev.get('audio_path')}")
        elif name == "prompter":
            p = ev.get("initial_prompt") or "(sin prompt)"
            print(f"      > {p[:240]}")
        elif name == "transcribe":
            print(
                f"      idioma  : {ev.get('language')}, "
                f"segmentos: {ev.get('segments')}, "
                f"duracion: {ev.get('duration')}s, "
                f"device: {ev.get('device')}/{ev.get('compute_type')}"
            )
        elif name == "transcript_edit":
            if ev.get("skipped"):
                print(f"      (post-edit deshabilitado)")
            else:
                print(
                    f"      corregidos: {ev.get('n_corrected')}/{ev.get('n_total')} segmentos. "
                    f"resumen: {(ev.get('summary') or '')[:140]}"
                )
        elif name == "extract_claims":
            claims = ev.get("claims", [])
            print(f"      claims extraidos: {len(claims)}")
            for c in claims:
                t0 = c.get("t_start") or 0
                t1 = c.get("t_end") or 0
                ctype = c.get("claim_type", "?")
                snippet = (c.get("claim") or "")[:90].replace("\n", " ")
                print(f"      - [{t0:.1f}-{t1:.1f}s] ({ctype}) {snippet}")
        elif name == "research_and_judge":
            print(
                f"      veredictos: {ev.get('verdicts')}, "
                f"omitidos: {ev.get('skipped')}, "
                f"con fallback open-web: {ev.get('used_fallback', 0)}"
            )
        elif name == "report":
            print(f"      ->        {ev.get('report_path')}")
    elif t == "claim_verdict_ready":
        v = ev["verdict"]
        cid = v.get("id")
        verdict = v.get("verdict")
        conf = v.get("confidence", 0.0)
        tag = " [fallback]" if v.get("fallback_used") else ""
        snippet = (v.get("correction") or "")[:120].replace("\n", " ")
        print(f"      [V] {cid} [{verdict}, conf={conf:.2f}]{tag}: {snippet}")
    elif t == "claim_skipped":
        cid = ev.get("id")
        print(f"      [S] {cid}: sin evidencia (ni estricto ni extendido)")
    elif t == "warn":
        print(f"      [warn] {ev.get('message')}")
    elif t == "error":
        print(f"      [ERROR step {ev.get('step')}] {ev.get('error')}")
    elif t == "completed":
        print(f"[OK] Reporte: {ev.get('report_path')}")
        print(
            f"     Veredictos: {ev.get('verdicts_count')}, "
            f"omitidos: {ev.get('skipped_count')}, "
            f"con fallback open-web: {ev.get('fallback_count', 0)}"
        )


def run_pipeline(url: str, **kwargs) -> dict:
    """Synchronous CLI entrypoint. Consumes the async generator and prints events."""
    final: dict = {}

    async def _consume():
        nonlocal final
        async for ev in stream_pipeline(url, **kwargs):
            _print_event(ev)
            if ev["type"] == "completed":
                final = ev

    asyncio.run(_consume())
    return final
