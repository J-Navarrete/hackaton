from anthropic import Anthropic


_SYSTEM = """Eres un asistente experto en fact-checking de discursos politicos. Tu tarea es leer la transcripcion de un video y extraer UNICAMENTE las afirmaciones verificables con datos objetivos.

REGLAS DE INCLUSION (afirmaciones que SI extraes):
- Hechos pasados o presentes con dato concreto: cifras, porcentajes, fechas, montos.
- Citas de leyes, decretos, normativas, indicaciones o glosas identificables.
- Estados legislativos verificables (ej: "aprobada por ambas camaras", "vigente desde X", "ingresada al Senado").
- Estadisticas atribuibles a una fuente o medibles (desempleo, inflacion, presupuesto, cobertura).
- Acciones concretas atribuidas a una persona, organismo, gobierno o empresa, ya ocurridas.
- Resultados electorales, votaciones, resoluciones de tribunales.

REGLAS DE EXCLUSION (afirmaciones que NO extraes):
- Opiniones, valoraciones o juicios subjetivos ("creo que", "es lo correcto", "es lo mas importante para Chile").
- Predicciones, promesas o compromisos a futuro ("vamos a hacer X", "la economia crecera", "se aprobara").
- Retorica, slogans, frases motivacionales o de cortesia (saludos, agradecimientos).
- Generalidades sin dato concreto ("hay mucho avance", "el pais esta mejor").
- Preguntas, hipoteticos, ironias.

REGLAS DE FIDELIDAD AL TEXTO:
- El campo "claim" debe contener el texto LITERAL de la transcripcion, sin parafraseo ni limpieza editorial.
- Puedes unir segmentos consecutivos si una sola afirmacion abarca varios, pero NO reescribas palabras.
- La transcripcion puede tener errores foneticos (Whisper). Si una palabra parece incoherente, ignora la afirmacion en vez de inventarla a partir de ruido.
- Si una misma afirmacion se repite, incluyela solo en su primera aparicion.

REGLAS DE ATRIBUCION DE SPEAKER:
- La transcripcion de Whisper NO incluye etiquetas de speaker, asi que no puedes saber con certeza quien dijo cada cosa.
- USA "speaker": null SIEMPRE en estos casos:
  (a) Videos de DEBATE entre 2+ personas (ej. titulo dice "debate", "cruce entre X y Y", "encuentro de candidatos").
  (b) Paneles, foros, mesas redondas con multiples voces.
  (c) Cualquier contenido donde no este obvio quien dice cada frase.
- SOLO completa "speaker" cuando el video es claramente de UN SOLO LOCUTOR identificable del titulo (ej. "Entrevista a X", "Discurso del Ministro Y", "Conferencia de X"). En estos casos pon el cargo o nombre.
- NUNCA inventes o adivines speaker desde la mitad del transcript. Si no estas seguro, null. Es mucho peor atribuir mal una frase que dejar sin atribuir.

SALIDA: debes llamar a la herramienta `submit_claims` con la lista de afirmaciones extraidas. Si no hay afirmaciones verificables, llama a la herramienta con `claims: []`. No respondas con texto, solo usa la herramienta.

Todo el texto dentro de la llamada (campos "claim", "rationale", "speaker") debe estar en espanol formal neutro adecuado para Chile."""


_CLAIM_TYPES = [
    "estadistica",
    "ley",
    "presupuesto",
    "fecha",
    "afirmacion-historica",
    "estado-legislativo",
    "cita-de-norma",
    "accion-atribuida",
    "resultado-electoral",
    "otro",
]


_SUBMIT_CLAIMS_TOOL = {
    "name": "submit_claims",
    "description": (
        "Registra la lista de afirmaciones verificables extraidas de la transcripcion. "
        "Llama esta herramienta exactamente una vez con todas las afirmaciones que cumplen las reglas de inclusion."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "description": "Lista de afirmaciones verificables. Vacia si no hay ninguna.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Identificador secuencial: 'c1', 'c2', ...",
                        },
                        "claim": {
                            "type": "string",
                            "description": "Texto literal de la afirmacion tal como aparece en la transcripcion, sin parafraseo.",
                        },
                        "speaker": {
                            "type": ["string", "null"],
                            "description": "Persona que enuncia la afirmacion, si es identificable desde el contexto del video. null si no es claro.",
                        },
                        "t_start": {
                            "type": "number",
                            "description": "Inicio de la afirmacion en segundos (start del primer segmento).",
                        },
                        "t_end": {
                            "type": "number",
                            "description": "Fin de la afirmacion en segundos (end del ultimo segmento).",
                        },
                        "segment_ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "IDs de los segmentos que cubre la afirmacion.",
                        },
                        "claim_type": {
                            "type": "string",
                            "enum": _CLAIM_TYPES,
                            "description": "Categoria del hecho verificable.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Que hay que verificar y por que es verificable, en espanol formal neutro.",
                        },
                    },
                    "required": [
                        "id",
                        "claim",
                        "t_start",
                        "t_end",
                        "segment_ids",
                        "claim_type",
                        "rationale",
                    ],
                },
            }
        },
        "required": ["claims"],
    },
}


def _format_segments(segments: list[dict]) -> str:
    lines = []
    for s in segments:
        lines.append(f"[{s['id']} @ {s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    return "\n".join(lines)


def extract_claims(
    transcript: dict,
    video_metadata: dict | None = None,
    model: str | None = None,
    max_tokens: int = 8000,
    provider: str = "claude",
) -> list[dict]:
    segments = transcript.get("segments") or []
    if not segments:
        return []

    parts: list[str] = []
    if video_metadata:
        parts.append("CONTEXTO DEL VIDEO:")
        if video_metadata.get("title"):
            parts.append(f"Titulo: {video_metadata['title']}")
        speaker_hint = video_metadata.get("channel") or video_metadata.get("uploader")
        if speaker_hint:
            parts.append(f"Canal: {speaker_hint}")
        if video_metadata.get("upload_date"):
            parts.append(f"Fecha de publicacion: {video_metadata['upload_date']}")
        parts.append("")

    parts.append("TRANSCRIPCION (formato [segment_id @ t_start-t_end] texto):")
    parts.append(_format_segments(segments))

    user_content = "\n".join(parts)

    if provider == "minimax":
        from .minimax_client import DEFAULT_MODEL, chat, get_tool_args, to_openai_tool, to_openai_tool_choice
        msg = chat(
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM,
            tools=[to_openai_tool(_SUBMIT_CLAIMS_TOOL)],
            tool_choice=to_openai_tool_choice("submit_claims"),
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens + 1000,
        )
        args = get_tool_args(msg, "submit_claims")
        if args is None:
            raise ValueError("MiniMax no llamo submit_claims")
        return args.get("claims", [])

    claude_model = model or "claude-sonnet-4-6"
    client = Anthropic()
    response = client.messages.create(
        model=claude_model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        tools=[_SUBMIT_CLAIMS_TOOL],
        tool_choice={"type": "tool", "name": "submit_claims"},
        messages=[{"role": "user", "content": user_content}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_claims":
            return block.input.get("claims", [])

    raise ValueError(
        f"Claude no llamo la herramienta submit_claims. stop_reason={response.stop_reason}"
    )
