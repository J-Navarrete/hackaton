import shutil
from functools import lru_cache
from pathlib import Path

import yt_dlp


@lru_cache(maxsize=1)
def _find_ffmpeg_dir() -> str | None:
    """Locate the directory that contains ffmpeg.exe so yt-dlp can postprocess.

    Tries PATH first, then common Windows install locations (WinGet, Program Files)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)

    candidates: list[Path] = []
    winget = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget.exists():
        candidates.extend(winget.glob("*FFmpeg*/ffmpeg-*-full_build/bin/ffmpeg.exe"))
        candidates.extend(winget.glob("*ffmpeg*/**/bin/ffmpeg.exe"))

    candidates.extend(
        [
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
            Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
            Path("C:/Program Files (x86)/ffmpeg/bin/ffmpeg.exe"),
        ]
    )

    for c in candidates:
        if c.exists():
            return str(c.parent)
    return None


def download_audio(url: str, output_dir: str | Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": False,
        "no_warnings": False,
        "noplaylist": True,
    }

    ffmpeg_dir = _find_ffmpeg_dir()
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        base = Path(ydl.prepare_filename(info))

    audio_path = base.with_suffix(".mp3")
    if not audio_path.exists():
        raise FileNotFoundError(f"Expected audio file not found: {audio_path}")

    return audio_path


def get_video_metadata(url: str) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "channel": info.get("channel"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url"),
        "description": info.get("description"),
        "tags": info.get("tags") or [],
    }
