"""Pre-extraction classifier that flags non-editorial transcription blocks (anuncios, transiciones, ruido) so they don't waste tokens on the extractor + researcher."""
from __future__ import annotations

from anthropic import Anthropic

_CLIENT: Anthropic | None = None


def _get_client() -> Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Anthropic()
    return _CLIENT


_SYSTEM = """Eres un clasificador de bloques de transcripción de transmisiones chilenas de TV y radio en vivo.

Tu única tarea es determinar la categoría de un bloque de texto transcrito, usando una de estas cuatro etiquetas:

- editorial: contenido informativo, declaraciones de personas, datos, entrevistas, debate político, lectura de noticias. ESTOS deben pasar al fact-checker.
- anuncio: publicidad comercial (productos, marcas, ofertas, llamados a la acción comercial como "compre", "visite", "disponible en"), auspicios, autopromos del canal.
- transicion: cortinas musicales, presentaciones del programa, saludos de bienvenida o despedida, anuncios de "ya volvemos", música transcrita como letra, lectura de patrocinadores al inicio o fin de bloque.
- ruido: transcripción incoherente (errores groseros de Whisper, repeticiones sin sentido, fragmentos truncados que no forman frases legibles).

REGLAS:
1. Si el bloque mezcla contenido editorial CON publicidad o transición, asigna "editorial" (priorizar no perder afirmaciones verificables). Solo asigna "anuncio" o "transicion" si ese tipo de contenido DOMINA todo el bloque.
2. Si el bloque es muy corto (menos de 25 palabras) o ambiguo, prefiere "editorial" para no perder material.
3. El campo "confidence" (0.0 a 1.0) debe reflejar qué tan claro fue el caso: 0.9+ para casos obvios, 0.5–0.7 para casos moderadamente claros.
4. El campo "reason" debe ser una oración breve en español neutro chileno que explique la decisión."""


def classify_block(
    block_text: str,
    video_metadata: dict | None = None,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 400,
) -> dict:
    """Classifies a transcription block as editorial/anuncio/transicion/ruido.

    Returns: {"label": str, "reason": str, "confidence": float}
    """
    try:
        user_lines: list[str] = []
        if video_metadata:
            title = video_metadata.get("title", "")
            channel = video_metadata.get("channel") or video_metadata.get("uploader", "")
            if title:
                user_lines.append(f"Título del programa: {title}")
            if channel:
                user_lines.append(f"Canal: {channel}")
            if user_lines:
                user_lines.append("")  # blank line before block
        user_lines.append("Bloque de transcripción a clasificar:")
        user_lines.append(block_text)
        user_content = "\n".join(user_lines)

        tool_def = {
            "name": "classify_block",
            "description": "Clasifica el bloque de transcripción.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "enum": ["editorial", "anuncio", "transicion", "ruido"],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Razón breve en una oración en español neutro chileno.",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": ["label", "reason", "confidence"],
            },
        }

        client = _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": "classify_block"},
            messages=[{"role": "user", "content": user_content}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == "classify_block":
                inp = block.input
                return {
                    "label": inp["label"],
                    "reason": inp["reason"],
                    "confidence": float(inp["confidence"]),
                }

        # If no tool_use block found (shouldn't happen with tool_choice forced), fail-open
        return {"label": "editorial", "reason": "fallback: no tool_use block in response", "confidence": 0.0}

    except Exception as e:
        return {"label": "editorial", "reason": f"fallback: {e}", "confidence": 0.0}
