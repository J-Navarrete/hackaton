from anthropic import Anthropic


_SYSTEM = """Eres un asistente que produce 'priming prompts' para el modelo de transcripcion Whisper.

ADVERTENCIA CRITICA: Whisper alucina y regurgita el prompt como output cuando este viene en forma de prosa con oraciones completas. La forma correcta de un priming prompt es una LISTA DE VOCABULARIO separada por comas, sin oraciones, sin verbos conjugados, sin frases narrativas.

Dado el contexto de un video (titulo, canal, descripcion, tags), genera una lista de terminos separados por comas que incluya:
- Nombres propios (personas, cargos, instituciones, lugares)
- Siglas y acronimos esperados, expandidos cuando sea util (ej: "DIPRES (Direccion de Presupuestos)")
- Vocabulario tecnico o jerga del dominio
- Numeros, fechas o leyes clave si aparecen en el titulo

REGLAS ESTRICTAS:
- NO escribas oraciones. NO uses verbos conjugados como "aborda", "discute", "menciona", "incluye", "se refiere".
- NO uses frases tipo "El registro es formal...", "La discusion abarca...", "Vocabulario esperado:". Estos patrones causan alucinaciones.
- Separa los terminos UNICAMENTE con comas.
- Maximo 80 terminos, maximo 200 caracteres en total para no exceder el contexto de Whisper.
- En el mismo idioma del audio esperado.

Ejemplo de salida correcta para un video sobre presupuesto chileno:
Ministro de Hacienda, Nicolas Grau, Ley de Reajuste, sector publico, Camara de Diputadas y Diputados, Senado, tercer tramite, DIPRES, ANEF, CUT, IPC, indicacion, glosa presupuestaria, votacion, articulado

Ejemplo INCORRECTO (causa alucinacion):
El Ministro de Hacienda aborda la Ley de Reajuste. La discusion incluye terminos como sector publico, DIPRES, ANEF.

Devuelve UNICAMENTE la lista, sin preambulos, sin comillas, sin explicaciones."""


def generate_initial_prompt(
    title: str | None,
    channel: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    model: str | None = None,
    max_tokens: int = 400,
    provider: str = "claude",
) -> str:
    title = (title or "").strip()
    channel = (channel or "").strip()
    description = (description or "").strip()
    tags = tags or []

    if not title and not description and not channel and not tags:
        return ""

    lines: list[str] = []
    if title:
        lines.append(f"Titulo: {title}")
    if channel:
        lines.append(f"Canal: {channel}")
    if tags:
        lines.append(f"Tags: {', '.join(tags[:20])}")
    if description:
        lines.append(f"Descripcion: {description[:1500]}")

    user_content = "\n".join(lines)

    if provider == "minimax":
        from .minimax_client import DEFAULT_MODEL, chat, get_text
        msg = chat(
            messages=[{"role": "user", "content": user_content}],
            system=_SYSTEM,
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens + 500,
        )
        return get_text(msg)

    claude_model = model or "claude-haiku-4-5-20251001"
    client = Anthropic()
    response = client.messages.create(
        model=claude_model,
        max_tokens=max_tokens,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text.strip()
