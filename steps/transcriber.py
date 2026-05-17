import os
import sys
from pathlib import Path


def _add_nvidia_dll_dirs() -> None:
    if sys.platform != "win32":
        return
    for entry in sys.path:
        nvidia_root = Path(entry) / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for child in nvidia_root.iterdir():
            bin_dir = child / "bin"
            if bin_dir.is_dir():
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    pass


_add_nvidia_dll_dirs()

from faster_whisper import WhisperModel  # noqa: E402


def _autodetect_device() -> str:
    try:
        from ctranslate2 import get_cuda_device_count

        return "cuda" if get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def _default_compute_type(device: str) -> str:
    return "int8_float16" if device == "cuda" else "int8"


def transcribe_audio(
    audio_path: str | Path,
    model_size: str = "large-v3",
    language: str | None = None,
    device: str = "auto",
    compute_type: str | None = None,
    beam_size: int = 5,
    vad_filter: bool = False,
    initial_prompt: str | None = None,
    condition_on_previous_text: bool = False,
) -> dict:
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if device == "auto":
        device = _autodetect_device()

    if compute_type is None:
        compute_type = _default_compute_type(device)

    try:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        if device == "cuda":
            print(f"      [warn] carga en CUDA fallo ({type(e).__name__}: {e}). Cayendo a CPU.")
            device = "cpu"
            compute_type = _default_compute_type(device)
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
        else:
            raise

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        initial_prompt=initial_prompt,
        condition_on_previous_text=condition_on_previous_text,
    )

    segments: list[dict] = []
    text_parts: list[str] = []
    for s in segments_iter:
        segments.append(
            {
                "id": s.id,
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "text": s.text.strip(),
            }
        )
        text_parts.append(s.text)

    return {
        "text": "".join(text_parts).strip(),
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 3),
        "model": f"faster-whisper:{model_size}",
        "device": device,
        "compute_type": compute_type,
        "initial_prompt": initial_prompt,
        "segments": segments,
    }
