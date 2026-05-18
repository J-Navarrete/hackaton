"""HLS audio capture for live streams via yt-dlp + ffmpeg.

Resolves the streaming manifest URL with yt-dlp, then spawns an ffmpeg
subprocess that writes rolling MP3 chunks to disk. A watcher coroutine
yields each completed chunk path as soon as it stabilizes (i.e., ffmpeg
finished writing it and started the next one).
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator

import yt_dlp

from steps.downloader import _find_ffmpeg_dir

# ---------------------------------------------------------------------------
# Process-wide registry of active ffmpeg Popen instances.
# Lets kill_all_ffmpegs() clean up orphans on shutdown / atexit.
# ---------------------------------------------------------------------------
_ACTIVE_FFMPEGS: set[subprocess.Popen] = set()


def kill_all_ffmpegs() -> int:
    """Kill every tracked ffmpeg subprocess that is still running.

    Returns the number of processes that were killed.
    Safe to call multiple times; processes are removed from the registry as
    they are handled. All exceptions from Popen.kill() are swallowed so that
    the atexit path never raises.
    """
    killed = 0
    for proc in list(_ACTIVE_FFMPEGS):
        try:
            if proc.poll() is None:
                proc.kill()
                killed += 1
        except Exception:
            pass
        _ACTIVE_FFMPEGS.discard(proc)
    return killed


atexit.register(kill_all_ffmpegs)


def _stderr(msg: str) -> None:
    """Direct print to stderr with flush. Bypasses logging config issues."""
    print(f"[live_capture] {msg}", file=sys.stderr, flush=True)


def resolve_stream_url(url: str) -> tuple[str, dict, bool, dict]:
    """Resolve HLS manifest URL and basic metadata for a live or VOD stream.

    Returns:
        (hls_url, metadata_dict, is_live, http_headers)
        http_headers are the headers yt-dlp would use to fetch the URL — ffmpeg
        needs the same ones (User-Agent, Cookie, etc.) or YouTube responds 403.
    """
    log = logging.getLogger(__name__)
    # Try the "android" client first: it returns MP4 audio with proper moov-atom
    # index, so ffmpeg can seek mid-file instantly. android_vr (yt-dlp default
    # for some content) returns WebM whose index sits at the end, breaking mid-file
    # seek. If android fails, fall through to default extraction.
    base_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    info = None
    try:
        opts = dict(base_opts, extractor_args={"youtube": {"player_client": ["android"]}})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("android client failed for %s, falling back to default: %s", url, e)
        try:
            with yt_dlp.YoutubeDL(base_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            log.exception("resolve_stream_url failed for %s", url)
            raise

    hls_url = info.get("url")
    http_headers: dict = info.get("http_headers") or {}
    if not hls_url:
        for fmt in (info.get("formats") or []):
            if fmt.get("url"):
                hls_url = fmt["url"]
                if not http_headers:
                    http_headers = fmt.get("http_headers") or {}
                break
    if not hls_url:
        raise ValueError(f"Could not resolve streaming URL for: {url}")

    is_live = bool(info.get("is_live"))

    metadata = {
        "id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel"),
        "uploader": info.get("uploader"),
        "webpage_url": info.get("webpage_url") or url,
        "description": info.get("description"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "tags": info.get("tags") or [],
        "is_live": is_live,
    }
    return hls_url, metadata, is_live, http_headers


def _chunk_index_from_name(path: Path) -> int | None:
    """Extract numeric index from chunk_%05d.mp3 filename."""
    m = re.search(r"chunk_(\d+)\.mp3$", path.name)
    return int(m.group(1)) if m else None


async def stream_chunks(
    hls_url: str,
    out_dir: Path,
    chunk_seconds: int = 20,
    readrate: float | None = None,
    http_headers: dict | None = None,
    start_offset_seconds: float | None = None,
) -> AsyncIterator[Path]:
    """Spawn ffmpeg to segment a live HLS stream into MP3 chunks.

    Yields each completed chunk Path in order. A chunk is considered complete
    when ffmpeg has moved on to writing the next chunk index, meaning the lower
    index file is closed and safe to read.

    Stops when ffmpeg exits or the consumer cancels the iteration.

    Args:
        readrate: Controls playback speed for VOD replay. None or 0 means no
            throttling (natural HLS rate). 1.0 uses -re (realtime). Values >1
            use -readrate <val> for faster-than-realtime VOD processing.
            For live streams, pass None so ffmpeg follows the live feed naturally.
        start_offset_seconds: If set and > 0, seek to this position in the source
            before starting capture. Uses -ss before -i for fast seek.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_dir = _find_ffmpeg_dir()
    if ffmpeg_dir:
        ffmpeg_exe = str(Path(ffmpeg_dir) / "ffmpeg.exe")
    else:
        ffmpeg_exe = "ffmpeg"  # fallback: hope it's on PATH

    # Build seek args. -ss before -i performs a fast seek in the source.
    # -noaccurate_seek skips the slow post-seek "decode forward to exact frame"
    # step which on YouTube WebM/Opus URLs can hang or take minutes.
    seek_args: list[str] = []
    if start_offset_seconds and start_offset_seconds > 0:
        seek_args = ["-noaccurate_seek", "-ss", str(start_offset_seconds)]

    # Build readrate flags. -re is equivalent to -readrate 1 but more widely
    # supported. For values other than 1, -readrate <val> is required (ffmpeg 4+).
    # If we're seeking into a VOD, skip readrate so ffmpeg can race to the
    # offset position. Once chunks are flowing the segmenter still produces
    # 20s-of-audio segments at the natural decode rate (fast on CPU).
    readrate_args: list[str] = []
    if readrate and readrate > 0 and not (start_offset_seconds and start_offset_seconds > 0):
        if readrate == 1.0:
            readrate_args = ["-re"]
        else:
            readrate_args = ["-readrate", str(readrate)]

    header_args: list[str] = []
    if http_headers:
        user_agent = http_headers.get("User-Agent")
        if user_agent:
            header_args += ["-user_agent", user_agent]
        non_ua_headers = {k: v for k, v in http_headers.items() if k.lower() != "user-agent"}
        if non_ua_headers:
            headers_blob = "".join(f"{k}: {v}\r\n" for k, v in non_ua_headers.items())
            header_args += ["-headers", headers_blob]

    cmd = [
        ffmpeg_exe,
        "-y",
        *readrate_args,
        *seek_args,
        *header_args,
        "-i", hls_url,
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        str(out_dir / "chunk_%05d.mp3"),
    ]

    _logger = logging.getLogger(__name__)

    log_path = out_dir / "ffmpeg.log"
    cmd_path = out_dir / "cmd.txt"
    try:
        cmd_path.write_text(
            " ".join(f'"{a}"' if " " in a or "\r" in a else a for a in cmd),
            encoding="utf-8",
        )
    except Exception:
        pass

    _stderr(f"about to spawn ffmpeg, see {cmd_path}")
    _stderr(f"hls_url prefix: {hls_url[:120]}")
    _stderr(f"http_headers keys: {list((http_headers or {}).keys())}")

    ffmpeg_log = open(log_path, "wb", buffering=0)

    # Use synchronous subprocess.Popen — asyncio.create_subprocess_exec raises
    # NotImplementedError on Windows under SelectorEventLoop (uvicorn default).
    # Popen.poll() and Popen.kill() are sync but constant-time, safe to call
    # from an async loop without an executor.
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=ffmpeg_log,
            stderr=ffmpeg_log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        _stderr(f"Popen FAILED: {type(e).__name__}: {e}")
        ffmpeg_log.close()
        raise

    _ACTIVE_FFMPEGS.add(proc)
    _stderr(f"ffmpeg spawned pid={proc.pid}")

    next_yield_index = 0

    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                # Drain any remaining closed chunks
                chunks = sorted(
                    [p for p in out_dir.glob("chunk_*.mp3") if _chunk_index_from_name(p) is not None],
                    key=lambda p: _chunk_index_from_name(p),
                )
                for chunk in chunks:
                    idx = _chunk_index_from_name(chunk)
                    if idx is not None and idx >= next_yield_index:
                        yield chunk
                        next_yield_index = idx + 1
                _stderr(f"ffmpeg exited with rc={rc}, chunks_produced={next_yield_index}")
                if next_yield_index == 0:
                    try:
                        log_bytes = log_path.read_bytes()
                        if log_bytes:
                            tail = log_bytes.decode("utf-8", errors="replace").splitlines()[-40:]
                            _stderr("=== last 40 lines of ffmpeg.log ===")
                            for line in tail:
                                _stderr(line)
                            _stderr("=== end ffmpeg.log ===")
                        else:
                            _stderr(
                                f"ffmpeg.log is EMPTY. The process likely crashed before opening "
                                f"the input. Open {cmd_path} and run that command manually in "
                                f"PowerShell to see the real error."
                            )
                    except Exception as e:
                        _stderr(f"failed to read ffmpeg.log: {e}")
                break

            await asyncio.sleep(1.5)

            chunks = sorted(
                [p for p in out_dir.glob("chunk_*.mp3") if _chunk_index_from_name(p) is not None],
                key=lambda p: _chunk_index_from_name(p),
            )
            if not chunks:
                continue

            max_index = _chunk_index_from_name(chunks[-1])

            for chunk in chunks:
                idx = _chunk_index_from_name(chunk)
                if idx is not None and idx >= next_yield_index and idx < max_index:
                    yield chunk
                    next_yield_index = idx + 1

    except asyncio.CancelledError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    finally:
        _ACTIVE_FFMPEGS.discard(proc)
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            ffmpeg_log.close()
        except Exception:
            pass
