import argparse
import sys

from dotenv import load_dotenv

from pipeline import run_pipeline


def _force_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_force_utf8_stdio()


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="politicheck",
        description="Pipeline de fact-checking para discursos politicos en video",
    )
    parser.add_argument("url", help="URL de YouTube/Shorts/TikTok/Instagram Reel del video a verificar")
    parser.add_argument(
        "--language",
        default=None,
        help="Codigo ISO-639-1 del idioma del audio (ej. 'es', 'en'). Si se omite, Whisper lo detecta.",
    )
    parser.add_argument(
        "--model",
        default="large-v3",
        help="Tamano del modelo Whisper: tiny, base, small, medium, large-v3 (default: large-v3).",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="Backend de inferencia (default: auto -> usa CUDA si esta disponible, si no CPU).",
    )
    parser.add_argument(
        "--initial-prompt",
        default=None,
        help="Override manual del priming prompt. Si se omite, se genera con Claude desde la metadata.",
    )
    parser.add_argument(
        "--no-initial-prompt",
        action="store_true",
        help="Desactiva el priming prompt completamente.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Numero maximo de claims investigados en paralelo (default: 5).",
    )
    parser.add_argument(
        "--no-transcript-edit",
        action="store_true",
        help="Salta el paso de post-edicion del transcript con Claude (ahorra ~$0.03 por video).",
    )
    parser.add_argument(
        "--no-fallback-search",
        action="store_true",
        help="No hace busqueda extendida (open web) cuando el allowlist estricto no tiene evidencia.",
    )
    parser.add_argument(
        "--llm-provider",
        default="claude",
        choices=["claude", "minimax"],
        help="Proveedor LLM para pasos de texto (prompter, extractor, editor, veredictos). Default: claude. "
             "Con 'minimax' la investigacion (web_search) sigue usando Claude.",
    )
    args = parser.parse_args()

    run_pipeline(
        args.url,
        language=args.language,
        model_size=args.model,
        device=args.device,
        initial_prompt_override=args.initial_prompt,
        skip_initial_prompt=args.no_initial_prompt,
        concurrency=args.concurrency,
        skip_transcript_edit=args.no_transcript_edit,
        fallback_to_open_web=not args.no_fallback_search,
        llm_provider=args.llm_provider,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
