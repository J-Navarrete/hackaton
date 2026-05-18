import asyncio
import json

from anthropic import AsyncAnthropic


def _normalize_dict_list(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    return []


_SYSTEM = """Eres un fact-checker profesional. Recibes UNA afirmacion politica y la evidencia que recopilo otro investigador en fuentes oficiales chilenas. Tu mision es emitir un veredicto formal segun esta taxonomia exacta:

TAXONOMIA DE VEREDICTOS:
- "Exacto": la afirmacion coincide con la evidencia dentro de un margen razonable (redondeo, fraseo equivalente). Todos los elementos sustanciales son correctos.
- "Parcialmente exacto": la afirmacion contiene elementos verdaderos pero omite contexto relevante, selecciona datos de forma sesgada, generaliza indebidamente, o es tecnicamente cierta pero enganosa.
- "Inexacto": la afirmacion es factualmente incorrecta segun la evidencia disponible.
- "Ridiculo": la afirmacion no tiene base en la realidad y contradice hechos basicos establecidos.

REGLAS DURAS:
- Apoya el veredicto UNICAMENTE en la evidencia entregada. NO uses conocimiento general. NO inventes datos, fechas, ni fuentes.
- Si la evidencia muestra contradicciones internas con el claim (por ejemplo, el claim dice "integramente aprobado" pero la evidencia muestra articulos rechazados), eso usualmente cae en "Parcialmente exacto", no en "Inexacto".
- La correccion debe ser un parrafo corto en espanol formal neutro chileno que presente los datos verificados sin editorializar ni hacer juicios politicos. Cita cifras y referencias concretas.
- confidence (0 a 1): refleja la robustez de la evidencia. Multiples fuentes oficiales concurrentes -> alta (>=0.85). Evidencia parcial, indirecta o truncada -> media (0.5-0.75). Evidencia minima -> baja (<0.5).
- key_sources: incluye las 1 a 4 fuentes mas determinantes para tu fallo, copiando title, url, retrieved_date y excerpt tal como aparecen en la evidencia.

REGLA IMPORTANTE SOBRE ATRIBUCION DE SPEAKER:
- El extractor a veces atribuye una frase a la persona equivocada porque Whisper no identifica speakers. EJ: claim dice "Kast afirmo X" pero la evidencia muestra que fue Boric quien dijo X.
- En estos casos NO marques Inexacto. El veredicto debe evaluar SOLO el contenido factual del claim, NO quien lo dijo.
- Si el contenido factual del claim coincide con la evidencia (aunque el speaker este mal atribuido), el veredicto es Exacto o Parcialmente exacto segun corresponda al hecho mismo.
- En la correccion, puedes mencionar la correccion de atribucion ("segun la evidencia, la frase fue dicha por X y no por Y"), pero eso NO degrada el veredicto del hecho.
- Solo marca Inexacto cuando el HECHO EN SI es factualmente incorrecto segun la evidencia, no por errores de atribucion del speaker.

Idioma: TODO el texto que produces en espanol formal neutro adecuado para Chile.

Llama EXACTAMENTE UNA VEZ a la herramienta submit_verdict con tu fallo."""


_SUBMIT_VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": (
        "Registra el veredicto final para el claim, basado unicamente en la evidencia entregada. "
        "Debe llamarse exactamente una vez."
    ),
    "cache_control": {"type": "ephemeral"},
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["Exacto", "Parcialmente exacto", "Inexacto", "Ridiculo"],
                "description": "El veredicto segun la taxonomia.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confianza en el veredicto (0 a 1), basada en calidad y cantidad de evidencia.",
            },
            "correction": {
                "type": "string",
                "description": "Parrafo corto en espanol formal neutro chileno con datos verificados y razonamiento. Sin editorializar.",
            },
            "key_sources": {
                "type": "array",
                "description": "Las 1 a 4 fuentes mas determinantes para el fallo.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "retrieved_date": {"type": "string"},
                        "excerpt": {"type": "string"},
                    },
                    "required": ["title", "url", "retrieved_date", "excerpt"],
                },
            },
        },
        "required": ["verdict", "confidence", "correction", "key_sources"],
    },
}


def _format_evidence(evidence) -> str:
    evidence = _normalize_dict_list(evidence)
    if not evidence:
        return "(sin evidencia)"
    lines = []
    for i, e in enumerate(evidence, 1):
        lines.append(f"[{i}] titulo: {e.get('title', '')}")
        lines.append(f"    url: {e.get('url', '')}")
        lines.append(f"    fecha consulta: {e.get('retrieved_date', '')}")
        if e.get("data_point"):
            lines.append(f"    dato: {e['data_point']}")
        if e.get("excerpt"):
            lines.append(f"    cita: \"{e['excerpt']}\"")
        lines.append("")
    return "\n".join(lines)


def _format_user_msg(claim: dict, research: dict) -> str:
    parts = [
        "CLAIM A EVALUAR:",
        f"- id: {claim['id']}",
        f"- texto: {claim['claim']}",
    ]
    if claim.get("speaker"):
        parts.append(
            f"- atribucion sugerida: {claim['speaker']} "
            "(metadata no confiable; no la evalues como parte del hecho)"
        )
    if claim.get("claim_type"):
        parts.append(f"- tipo: {claim['claim_type']}")
    parts.append("")

    evidence = research.get("evidence", [])
    parts.append(f"EVIDENCIA RECOPILADA ({len(evidence)} piezas):")
    parts.append(_format_evidence(evidence))

    if research.get("search_summary"):
        parts.append("RESUMEN DEL INVESTIGADOR:")
        parts.append(research["search_summary"])
        parts.append("")

    parts.append("Emite el veredicto llamando a submit_verdict.")
    return "\n".join(parts)


def _build_verdict_record(claim: dict, v: dict) -> dict:
    return {
        "id": claim["id"],
        "claim": claim["claim"],
        "speaker": claim.get("speaker"),
        "claim_type": claim.get("claim_type"),
        "t_start": claim.get("t_start"),
        "t_end": claim.get("t_end"),
        "segment_ids": claim.get("segment_ids", []),
        "verdict": v.get("verdict"),
        "confidence": v.get("confidence"),
        "correction": v.get("correction"),
        "sources": _normalize_dict_list(v.get("key_sources")),
    }


def _build_error_record(claim: dict, error: str) -> dict:
    return {
        "id": claim["id"],
        "claim": claim["claim"],
        "speaker": claim.get("speaker"),
        "claim_type": claim.get("claim_type"),
        "t_start": claim.get("t_start"),
        "t_end": claim.get("t_end"),
        "segment_ids": claim.get("segment_ids", []),
        "verdict": None,
        "confidence": 0.0,
        "correction": "",
        "sources": [],
        "_error": error,
    }


def _judge_one_sync_minimax(claim: dict, research: dict, model: str, max_tokens: int) -> dict:
    from .minimax_client import DEFAULT_MODEL, chat, get_tool_args, to_openai_tool, to_openai_tool_choice
    user_msg = _format_user_msg(claim, research)
    tool = dict(_SUBMIT_VERDICT_TOOL)
    tool.pop("cache_control", None)
    try:
        msg = chat(
            messages=[{"role": "user", "content": user_msg}],
            system=_SYSTEM,
            tools=[to_openai_tool(tool)],
            tool_choice=to_openai_tool_choice("submit_verdict"),
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens + 1000,
        )
    except Exception as e:
        return _build_error_record(claim, f"{type(e).__name__}: {e}")

    args = get_tool_args(msg, "submit_verdict")
    if args is None:
        return _build_error_record(claim, "MiniMax no llamo submit_verdict")
    return _build_verdict_record(claim, args)


async def _judge_one(
    client: AsyncAnthropic,
    claim: dict,
    research: dict,
    model: str,
    max_tokens: int,
    provider: str = "claude",
) -> dict:
    if provider == "minimax":
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: _judge_one_sync_minimax(claim, research, model, max_tokens)
        )

    user_msg = _format_user_msg(claim, research)
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            tools=[_SUBMIT_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "submit_verdict"},
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return _build_error_record(claim, f"{type(e).__name__}: {e}")

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_verdict":
            record = _build_verdict_record(claim, dict(block.input))
            return record

    return _build_error_record(claim, f"no submit_verdict call, stop_reason={response.stop_reason}")


async def _judge_all_async(
    pairs: list[tuple[dict, dict]],
    model: str,
    concurrency: int,
    max_tokens: int,
) -> list[dict]:
    client = AsyncAnthropic()
    sem = asyncio.Semaphore(concurrency)

    async def bounded(pair: tuple[dict, dict]) -> dict:
        async with sem:
            return await _judge_one(client, pair[0], pair[1], model, max_tokens)

    return await asyncio.gather(*[bounded(p) for p in pairs])


def judge_claims(
    claims: list[dict],
    research: list[dict],
    model: str = "claude-sonnet-4-6",
    concurrency: int = 5,
    max_tokens: int = 4000,
) -> dict:
    research_by_id = {r["claim_id"]: r for r in research}

    pairs: list[tuple[dict, dict]] = []
    skipped: list[dict] = []
    for c in claims:
        r = research_by_id.get(c["id"])
        if not r or not r.get("verifiable"):
            skipped.append(
                {
                    "id": c["id"],
                    "claim": c["claim"],
                    "speaker": c.get("speaker"),
                    "claim_type": c.get("claim_type"),
                    "t_start": c.get("t_start"),
                    "t_end": c.get("t_end"),
                    "segment_ids": c.get("segment_ids", []),
                    "reason": "Sin evidencia concluyente en las fuentes oficiales permitidas",
                    "search_summary": (r or {}).get("search_summary", ""),
                }
            )
            continue
        pairs.append((c, r))

    if not pairs:
        return {"verdicts": [], "skipped_claims": skipped}

    verdicts = asyncio.run(_judge_all_async(pairs, model, concurrency, max_tokens))
    return {"verdicts": verdicts, "skipped_claims": skipped}
