"""Post-edit a Whisper transcript with Claude to fix obvious phonetic errors.

Whisper produces fluent but sometimes wrong transcriptions ("hilos" vs "y los",
garbled proper nouns, mis-segmented acronyms). This step uses Claude Sonnet
to correct obvious errors based on context (video metadata + surrounding
segments) WITHOUT inventing new content or normalizing language.

Timestamps and segment IDs are preserved verbatim. Original text is kept in
each segment's `original_text` field for traceability.
"""
from __future__ import annotations

from anthropic import Anthropic


_SYSTEM = """Eres un editor experto de transcripciones automaticas producidas por Whisper.

Recibes una transcripcion segmentada de un video, mas su contexto (titulo, canal, descripcion). Whisper suele producir errores foneticos donde elige una palabra plausible pero incorrecta segun el contexto. Tu tarea es corregir esos errores SIN inventar contenido nuevo.

REGLAS DE CORRECCION:
- Corrige homofonos donde el contexto deja claro cual es la palabra correcta. Ejemplos:
  - "hilos del sector publico" -> "y los del sector publico"
  - "sale del Senado" si el contexto es legislativo -> "Sala del Senado"
  - "incidacion" -> "indicacion"
- Corrige nombres propios mal escritos cuando son evidentes del contexto del canal/titulo/descripcion:
  - "Marcel Bayejos" -> "Mario Marcel" si el video es sobre Hacienda
  - "Janet Jara" -> "Jeannette Jara" si el contexto es debate presidencial Chile
- Corrige siglas mal segmentadas o mal capitalizadas (PGU, ANEF, DIPRES, IPC, etc.)
- Corrige numeros y porcentajes mal escritos cuando son evidentes en contexto

LO QUE NO DEBES HACER:
- NO inventes informacion nueva ni agregues palabras que no estaban
- NO elimines palabras presentes en el original
- NO unifies ni dividas segmentos. Mantienes exactamente la misma cantidad de segmentos y los mismos IDs
- NO cambies palabras donde NO hay evidencia clara de error fonetico
- NO cambies el sentido literal de ninguna afirmacion
- NO normalices muletillas, jergas, "po", "ya", "este", repeticiones. Son parte del habla real
- NO traduzcas

ENTREGA:
Llama EXACTAMENTE UNA VEZ a la herramienta submit_corrected_segments.
- Devuelve TODOS los segmentos en el mismo orden y cantidad de entrada.
- Para cada uno, devuelve su id y el texto (corregido si era necesario, o el original sin cambios).
- changes_summary describe brevemente que tipos de correcciones aplicaste, en espanol formal neutro chileno."""


_TOOL = {
    "name": "submit_corrected_segments",
    "description": (
        "Devuelve la lista completa de segmentos con texto corregido. Mismo orden y cantidad de entrada."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "description": "Lista completa de segmentos. Mismo orden y cantidad que la entrada.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "El id original del segmento (sin cambios).",
                        },
                        "text": {
                            "type": "string",
                            "description": "Texto corregido (o identico al original si no requeria correccion).",
                        },
                    },
                    "required": ["id", "text"],
                },
            },
            "changes_summary": {
                "type": "string",
                "description": "Resumen breve en espanol formal neutro de las correcciones aplicadas (max 60 palabras).",
            },
        },
        "required": ["segments", "changes_summary"],
    },
}


def _format_segments(segments: list[dict]) -> str:
    lines = []
    for s in segments:
        lines.append(f"[{s['id']} @ {s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    return "\n".join(lines)


def post_edit_transcript(
    transcript: dict,
    video_metadata: dict | None = None,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 8000,
) -> dict:
    segments = transcript.get("segments") or []
    if not segments:
        return transcript

    parts: list[str] = []
    if video_metadata:
        parts.append("CONTEXTO DEL VIDEO:")
        if video_metadata.get("title"):
            parts.append(f"Titulo: {video_metadata['title']}")
        if video_metadata.get("channel") or video_metadata.get("uploader"):
            parts.append(
                f"Canal: {video_metadata.get('channel') or video_metadata.get('uploader')}"
            )
        if video_metadata.get("description"):
            parts.append(f"Descripcion: {video_metadata['description'][:500]}")
        parts.append("")

    parts.append(f"SEGMENTOS A REVISAR ({len(segments)} en total):")
    parts.append(_format_segments(segments))

    user_content = "\n".join(parts)

    client = Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_corrected_segments"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        result = dict(transcript)
        result["post_edit"] = {
            "model": model,
            "error": f"{type(e).__name__}: {e}",
            "n_segments_corrected": 0,
        }
        return result

    corrected_by_id: dict[int, str] = {}
    changes_summary = ""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_corrected_segments":
            for s in block.input.get("segments", []):
                if isinstance(s, dict) and "id" in s and "text" in s:
                    try:
                        corrected_by_id[int(s["id"])] = str(s["text"])
                    except (TypeError, ValueError):
                        continue
            changes_summary = block.input.get("changes_summary", "")
            break

    if not corrected_by_id:
        result = dict(transcript)
        result["post_edit"] = {
            "model": model,
            "n_segments_corrected": 0,
            "note": "El modelo no llamo submit_corrected_segments.",
        }
        return result

    new_segments: list[dict] = []
    text_parts: list[str] = []
    n_corrected = 0
    for s in segments:
        original = (s.get("text") or "").strip()
        corrected = corrected_by_id.get(s.get("id"))
        new_seg = dict(s)
        if corrected is not None and corrected.strip() != original:
            new_seg["original_text"] = s.get("text")
            new_seg["text"] = corrected.strip()
            text_parts.append(corrected.strip())
            n_corrected += 1
        else:
            text_parts.append(original)
        new_segments.append(new_seg)

    result = dict(transcript)
    result["segments"] = new_segments
    result["text"] = " ".join(text_parts).strip()
    result["post_edit"] = {
        "model": model,
        "changes_summary": changes_summary,
        "n_segments_corrected": n_corrected,
        "n_segments_total": len(segments),
    }
    return result
