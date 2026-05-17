import asyncio
import json
from datetime import date
from pathlib import Path

from anthropic import AsyncAnthropic


def _normalize_evidence(raw) -> list[dict]:
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


_SYSTEM_STRICT = """Eres un investigador de fact-checking. Tu mision es buscar evidencia objetiva en fuentes oficiales chilenas para verificar UNA afirmacion politica especifica.

FLUJO OBLIGATORIO:
1. Usa la herramienta `web_search` para buscar evidencia. Puedes invocarla varias veces si necesitas refinar la busqueda con diferentes queries.
2. Cuando hayas recopilado evidencia suficiente, o concluyas que no hay evidencia accesible en las fuentes permitidas, llama EXACTAMENTE UNA VEZ a la herramienta `submit_evidence` con tu hallazgo final.

REGLAS DE INVESTIGACION:
- El allowlist de dominios esta configurado en la herramienta `web_search` y NO puedes salir de el. Solo cuentan resultados de fuentes oficiales.
- Para cada pieza de evidencia, registra: title (titulo del documento o pagina), url (URL exacta del resultado), retrieved_date (fecha de hoy en formato YYYY-MM-DD), excerpt (cita literal del texto en su idioma original), data_point (el dato especifico que confirma o refuta el claim, ej: "131 articulos aprobados").
- NO inventes URLs ni datos. Solo registra lo que aparece en los resultados de la busqueda.
- Si la afirmacion contiene errores foneticos obvios de transcripcion (Whisper), intenta interpretar la intencion al construir las queries (ej. si lees "hilos" pero el contexto sugiere "y los", busca por la intencion).
- Si recibes una atribucion de speaker, tratala solo como metadata no confiable. NO la uses como restriccion principal de busqueda ni descartes evidencia porque la frase haya sido dicha por otra persona. Investiga el contenido factual del texto.
- Usa el `id` del claim que se te entrega como valor del campo `claim_id`.

REGLAS DE NO-VEREDICTO:
- NO clasifiques el claim como Exacto/Inexacto/etc. Eso lo hace otro paso del pipeline.
- Tu unica tarea es recolectar evidencia y dejarla disponible para que otro modelo emita el veredicto.

SI NO HAY EVIDENCIA:
- Marca `verifiable=false`, `evidence=[]`, y explica en `search_summary` que queries intentaste y por que no se encontro informacion concluyente en las fuentes permitidas.

IDIOMA:
- `search_summary` y `data_point` en espanol formal neutro chileno.
- `excerpt` en el idioma original del documento (puede ser espanol u otro)."""


_SYSTEM_OPEN = """Eres un investigador de fact-checking. La busqueda restringida a fuentes oficiales chilenas no encontro evidencia concluyente para UN claim especifico. Ahora haces una BUSQUEDA EXTENDIDA sin restriccion de dominio, pero priorizando fuertemente la confiabilidad de la fuente.

ORDEN DE PRIORIDAD DE FUENTES (mas confiable arriba):
1. Sitios gubernamentales chilenos e internacionales (.gob.cl, .gov, ministerios, organismos estatales)
2. Organizaciones internacionales reconocidas (ONU, OCDE, OMS, FMI, BID, CEPAL, BM, OPS/PAHO, UNICEF)
3. Instituciones academicas con peer-review (universidades, revistas indexadas, papers cientificos)
4. Medios profesionales establecidos (BBC, Reuters, AP, AFP, EFE, La Tercera, Emol, El Mostrador, Cooperativa, T13)
5. Iniciativas de fact-checking dedicadas (FastCheck CL, Chequeado, PolitiFact, AFP Factual)

PROHIBIDO usar como evidencia primaria:
- Wikipedia (es secundaria; sigue su fuente original citada y verifica esa)
- Blogs personales, sustacks
- Foros (Reddit, 4chan, etc.)
- Redes sociales (X/Twitter, TikTok, Facebook, Instagram), salvo cuentas verificadas de organismos oficiales
- Sitios partidarios o militantes como evidencia neutra
- Contenido generado por IA sin atribucion humana

FLUJO OBLIGATORIO:
1. Usa la herramienta `web_search` (puedes invocarla varias veces refinando queries) para buscar evidencia priorizando fuentes confiables.
2. Llama EXACTAMENTE UNA VEZ a `submit_evidence` con tu hallazgo final.

REGLAS DE REGISTRO:
- Para cada pieza de evidencia, registra: title, url (exacta), retrieved_date (YYYY-MM-DD), excerpt (cita literal), data_point.
- NO inventes URLs ni datos. Solo lo que aparece en los resultados de la busqueda.
- En `search_summary`, ademas de describir que buscaste y que encontraste, INDICA EXPLICITAMENTE que esto fue una busqueda extendida y por que (la busqueda estricta no encontro evidencia).
- Si recibes una atribucion de speaker, tratala solo como metadata no confiable. NO la uses como restriccion principal de busqueda ni descartes evidencia porque la frase haya sido dicha por otra persona. Investiga el contenido factual del texto.
- Usa el `id` del claim que se te entrega como valor del campo `claim_id`.

NO emites veredicto. NO clasificas. Solo recolectas evidencia.

Si tampoco encuentras evidencia confiable: verifiable=false, evidence=[], y explica la busqueda intentada.

IDIOMA: search_summary y data_point en espanol formal neutro chileno. excerpt en idioma original."""


_SUBMIT_EVIDENCE_TOOL = {
    "name": "submit_evidence",
    "description": (
        "Registra la evidencia recopilada para el claim investigado. "
        "Llamala UNA SOLA VEZ al final, despues de haber hecho las busquedas necesarias."
    ),
    "cache_control": {"type": "ephemeral"},
    "input_schema": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "El id del claim investigado (ej: 'c1').",
            },
            "verifiable": {
                "type": "boolean",
                "description": (
                    "True si encontraste evidencia en las fuentes permitidas que permite emitir un veredicto "
                    "sobre el claim. False si ninguna fuente del allowlist tiene informacion concluyente."
                ),
            },
            "evidence": {
                "type": "array",
                "description": "Lista de piezas de evidencia. Vacia si verifiable=false.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Titulo del documento o pagina."},
                        "url": {"type": "string", "description": "URL exacta de la fuente."},
                        "retrieved_date": {
                            "type": "string",
                            "description": "Fecha de consulta en formato YYYY-MM-DD.",
                        },
                        "excerpt": {
                            "type": "string",
                            "description": "Cita literal del texto en su idioma original.",
                        },
                        "data_point": {
                            "type": "string",
                            "description": "El dato especifico (cifra, fecha, articulo, votacion, etc) que confirma o refuta el claim.",
                        },
                    },
                    "required": ["title", "url", "retrieved_date", "excerpt", "data_point"],
                },
            },
            "search_summary": {
                "type": "string",
                "description": "Resumen breve en espanol formal neutro chileno: que se busco y que se encontro o no se encontro.",
            },
        },
        "required": ["claim_id", "verifiable", "evidence", "search_summary"],
    },
}


def _web_search_tool(allowed_domains: list[str] | None, max_uses: int) -> dict:
    tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
    }
    if allowed_domains:
        tool["allowed_domains"] = list(allowed_domains)
    return tool


def _format_claim_prompt(claim: dict, today: str) -> str:
    lines = [
        f"FECHA DE HOY: {today}",
        "",
        "CLAIM A INVESTIGAR:",
        f"- id: {claim['id']}",
        f"- texto: {claim['claim']}",
    ]
    if claim.get("speaker"):
        lines.append(
            f"- atribucion sugerida: {claim['speaker']} "
            "(metadata no confiable; no usar como filtro principal)"
        )
    if claim.get("claim_type"):
        lines.append(f"- tipo: {claim['claim_type']}")
    if claim.get("rationale"):
        lines.append(f"- que verificar: {claim['rationale']}")
    lines.append("")
    lines.append(
        "Procede: 1) usa web_search para buscar evidencia en las fuentes permitidas. "
        "2) cuando tengas suficiente, llama submit_evidence con tu hallazgo."
    )
    return "\n".join(lines)


async def _research_one(
    client: AsyncAnthropic,
    claim: dict,
    allowed_domains: list[str] | None,
    model: str,
    today: str,
    max_searches: int,
    max_tokens: int,
    mode: str = "strict",
) -> dict:
    """One research pass. mode='strict' uses allowed_domains; mode='open' has no restriction
    and uses a different system prompt that emphasizes source prioritization."""
    user_msg = _format_claim_prompt(claim, today)
    system_prompt = _SYSTEM_OPEN if mode == "open" else _SYSTEM_STRICT
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=[_web_search_tool(allowed_domains, max_searches), _SUBMIT_EVIDENCE_TOOL],
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        return {
            "claim_id": claim["id"],
            "verifiable": False,
            "evidence": [],
            "search_summary": f"Error en llamada a API: {type(e).__name__}: {e}",
            "_error": True,
            "_mode": mode,
        }

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_evidence":
            result = dict(block.input)
            raw_evidence = result.get("evidence")
            normalized = _normalize_evidence(raw_evidence)
            if not isinstance(raw_evidence, list):
                print(
                    f"      [warn] {claim['id']}: evidence vino como {type(raw_evidence).__name__}, "
                    f"normalizado a {len(normalized)} items."
                )
            result["evidence"] = normalized
            result["claim_id"] = claim["id"]
            result["_stop_reason"] = response.stop_reason
            result["_mode"] = mode
            return result

    return {
        "claim_id": claim["id"],
        "verifiable": False,
        "evidence": [],
        "search_summary": (
            f"El modelo no llamo a submit_evidence. stop_reason={response.stop_reason}. "
            "Esto puede indicar que se agoto el max_uses de web_search sin conclusion, o que el modelo decidio no investigar."
        ),
        "_stop_reason": response.stop_reason,
        "_mode": mode,
    }


def _tag_evidence_tier(result: dict, tier: str) -> None:
    """Annotate each evidence item with source_tier in place."""
    for e in result.get("evidence") or []:
        if isinstance(e, dict):
            e["source_tier"] = tier


async def _research_one_tiered(
    client: AsyncAnthropic,
    claim: dict,
    allowed_domains: list[str],
    model: str,
    today: str,
    max_searches: int,
    max_tokens: int,
    fallback_to_open_web: bool = True,
) -> dict:
    """Strict allowlist first; if not verifiable and fallback enabled, retry with open web.

    Each evidence item is tagged with source_tier:
      - "official_allowlist" - found via the strict allowlist
      - "open_web"           - found via the fallback open-web search
    """
    strict = await _research_one(
        client, claim, allowed_domains, model, today, max_searches, max_tokens, mode="strict"
    )
    _tag_evidence_tier(strict, "official_allowlist")

    if strict.get("verifiable") or not fallback_to_open_web:
        return strict

    open_res = await _research_one(
        client, claim, None, model, today, max_searches, max_tokens, mode="open"
    )
    _tag_evidence_tier(open_res, "open_web")

    if open_res.get("verifiable"):
        open_res["fallback_used"] = True
        original_summary = strict.get("search_summary") or ""
        open_summary = open_res.get("search_summary") or ""
        open_res["search_summary"] = (
            "[BUSQUEDA EXTENDIDA] No se encontro evidencia en el allowlist estricto de fuentes oficiales; "
            "se amplio la busqueda a fuentes confiables sin restriccion de dominio.\n\n"
            f"Allowlist estricto: {original_summary}\n\n"
            f"Busqueda extendida: {open_summary}"
        )
        return open_res

    strict["fallback_attempted"] = True
    return strict


async def _research_all_async(
    claims: list[dict],
    allowed_domains: list[str],
    model: str,
    concurrency: int,
    max_searches_per_claim: int,
    max_tokens: int,
    fallback_to_open_web: bool = True,
) -> list[dict]:
    client = AsyncAnthropic()
    today = date.today().isoformat()
    sem = asyncio.Semaphore(concurrency)

    async def bounded(claim: dict) -> dict:
        async with sem:
            return await _research_one_tiered(
                client,
                claim,
                allowed_domains,
                model,
                today,
                max_searches_per_claim,
                max_tokens,
                fallback_to_open_web=fallback_to_open_web,
            )

    return await asyncio.gather(*[bounded(c) for c in claims])


def load_sources(path: str | Path) -> list[dict]:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def research_claims(
    claims: list[dict],
    sources: list[dict] | list[str],
    model: str = "claude-sonnet-4-6",
    concurrency: int = 5,
    max_searches_per_claim: int = 5,
    max_tokens: int = 4000,
    fallback_to_open_web: bool = True,
) -> list[dict]:
    if not claims:
        return []
    allowed_domains = [s["domain"] if isinstance(s, dict) else s for s in sources]
    return asyncio.run(
        _research_all_async(
            claims=claims,
            allowed_domains=allowed_domains,
            model=model,
            concurrency=concurrency,
            max_searches_per_claim=max_searches_per_claim,
            max_tokens=max_tokens,
            fallback_to_open_web=fallback_to_open_web,
        )
    )
