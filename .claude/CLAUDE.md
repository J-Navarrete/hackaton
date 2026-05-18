# PolitiCheck
Pipeline automatizado de fact-checking de discursos políticos en video: descarga audio, transcribe con Whisper, extrae afirmaciones verificables y las contrasta contra fuentes oficiales chilenas usando Claude, generando un reporte HTML interactivo.

## Stack
- **Python 3.13** (importante: no usar 3.14, wheels de pydantic-core incompatibles)
- **faster-whisper** (large-v3 por defecto, local, GPU opcional vía CUDA)
- **anthropic SDK** (Claude Sonnet 4.6 para extracción/investigación/veredictos, Haiku 4.5 para prompter/editor)
- **yt-dlp + FFmpeg** (descarga multi-plataforma)
- **FastAPI + uvicorn** (web app con WebSocket para streaming de eventos)
- **SQLAlchemy 2.0 + SQLite** (`outputs/politicheck.db`)
- **Jinja2** (templates HTML)
- **MiniMax** (proveedor LLM alternativo opcional vía `--llm-provider minimax`)

## Estructura del proyecto

```
politicheck/
├── pipeline.py          # Orquestador async principal (7 pasos, emite eventos)
├── main.py              # CLI entry point (argparse → run_pipeline)
├── sources.json         # Allowlist de ~47 fuentes oficiales (editable sin tocar código)
├── steps/
│   ├── downloader.py    # yt-dlp: descarga audio + metadata del video
│   ├── prompter.py      # Genera vocabulary list para priming de Whisper (Claude/MiniMax)
│   ├── transcriber.py   # faster-whisper: audio → segmentos con timestamps
│   ├── transcript_editor.py  # Corrige errores fonéticos de Whisper (Claude/MiniMax)
│   ├── extractor.py     # Extrae claims verificables via tool_use (Claude/MiniMax)
│   ├── researcher.py    # Investiga cada claim con web_search (solo Claude)
│   ├── verdicts.py      # Emite veredicto por claim (Claude/MiniMax)
│   ├── reporter.py      # Genera HTML autocontenido (sin dependencias externas)
│   └── minimax_client.py  # Wrapper HTTP para MiniMax API (stdlib urllib)
├── web/
│   ├── app.py           # FastAPI app factory, middleware, routers
│   ├── db.py            # SQLAlchemy models: Video, Claim, Verdict, Vote
│   ├── jobs.py          # Job registry en memoria + run_job() (WebSocket fanout)
│   ├── persist.py       # Helpers DB: upsert_video, insert_claim_with_verdict, etc.
│   └── routes/
│       ├── pages.py     # GET /, /analyze, /v/{video_id}
│       ├── api.py       # POST /api/analyze, GET /api/jobs/{id}, POST /api/vote/{id}
│       └── ws.py        # WS /ws/job/{job_id} (streaming de eventos del pipeline)
├── web/templates/       # Jinja2: base.html, home.html, analyze.html, video.html
├── web/static/          # styles.css (~22 KB)
├── outputs/             # Artefactos generados (audio, transcripts, claims, reports)
│   └── politicheck.db   # SQLite con historial de análisis
├── backfill_db.py       # Script one-off: importa JSONs existentes a SQLite
└── refresh_embeds.py    # Script: regenera embed_html para todos los videos
```

## Contexto importante

**Pipeline de 7 pasos (orden crítico):**
1. `download` → audio.mp3 + metadata
2. `prompter` → vocabulary list para Whisper (Claude Haiku)
3. `transcribe` → faster-whisper, segmentos con timestamps
4. `transcript_edit` → corrección fonética opcional (Claude Haiku; default: activo en web, skippable en CLI)
5. `extract_claims` → tool_use forzado a `submit_claims` (Claude Sonnet)
6. `research_and_judge` → **paralelo**, 2 tiers: strict allowlist → fallback open web; web_search siempre usa Claude
7. `report` → HTML autocontenido

**Flujo de eventos:** `pipeline.py` es un async generator que emite dicts `{type, ...}`. La web app los consume vía `web/jobs.py` (fanout a WebSocket subscribers). El CLI los consume en `run_pipeline()` y los imprime.

**Dos tiers de investigación:**
- Strict: `allowed_domains` de `sources.json` (oficial chileno + internacional)
- Fallback (open web): si strict no encuentra evidencia; marcado con `source_tier: "open_web"` y `fallback_used: True`

**Multi-provider LLM:**
- `--llm-provider claude` (default): todos los pasos usan Claude
- `--llm-provider minimax`: pasos 2,4,5,7 usan MiniMax; paso 6 (research) SIEMPRE usa Claude (web_search es exclusivo de Anthropic)
- Convertidor de tool schemas: `minimax_client.to_openai_tool()` traduce Anthropic format → OpenAI format

**Speaker attribution:** Whisper no identifica speakers. El extractor pone `speaker: null` en debates/paneles. El juez de veredictos evalúa solo el hecho factual, no la atribución.

**Claim text literal:** Los claims preservan el texto exacto del transcript (incluyendo errores de Whisper). No parafrasear. Esto es intencional para evitar sesgo editorial.

**Windows / Python version:** El proyecto requiere Python 3.13 en este entorno. Python 3.14 es el default del sistema pero `pydantic_core` no tiene wheel compatible. Siempre usar `.\.venv\Scripts\python.exe` / `.\.venv\Scripts\uvicorn.exe` explícitamente, no `python` / `uvicorn` solos.

**Job registry:** En memoria (`web/jobs.py`). Los jobs se pierden si el servidor reinicia, pero los artefactos (DB, JSON, HTML en `outputs/`) persisten.

**DB schema:** `videos (1) → (N) claims (1) → (1) verdict` y `claims (1) → (N) votes`. Los votes son anónimos (session UID = 16-char hex), upsertables.

## Reglas

- **No modificar `sources.json` sin confirmar** — define qué fuentes son "oficiales"; cambios afectan todos los análisis futuros
- **No modificar el schema de `web/db.py`** sin crear migración (SQLAlchemy no auto-migra; usar `Base.metadata.create_all` solo crea tablas nuevas, no altera existentes)
- **No cambiar el contrato de eventos de `pipeline.py`** sin actualizar `web/jobs.py` y el frontend JS simultáneamente — los eventos son el contrato entre pipeline y web app
- **El texto de los claims debe ser literal** — no parafrasear, no limpiar errores en el campo `claim`
- **research siempre usa Claude** — no redirigir `_research_one_tiered` a MiniMax; depende de `web_search_20250305`
- Mantener los outputs en `outputs/` (no versionado); `outputs/.gitkeep` existe para mantener el directorio
- Cuando agregues un step nuevo al pipeline, actualiza `TOTAL_STEPS` y los eventos de `_print_event` en `pipeline.py`

## Comandos útiles

```powershell
# Levantar web app (SIEMPRE con ruta explícita por issue de Python 3.14 en el sistema)
.\.venv\Scripts\uvicorn.exe web.app:app --port 8000 --reload

# Correr CLI sobre un video
.\.venv\Scripts\python.exe main.py "URL" --language es

# Con MiniMax como LLM alternativo (pasos de texto)
.\.venv\Scripts\python.exe main.py "URL" --llm-provider minimax

# Opciones útiles del CLI
--no-transcript-edit      # Salta corrección fonética (ahorra ~$0.03)
--no-fallback-search      # Solo fuentes del allowlist estricto
--model medium            # Whisper más rápido (menos preciso)
--device cpu              # Forzar CPU aunque haya GPU

# Recrear venv (SIEMPRE usar ruta completa — py -3.13 copia el launcher de Windows que resuelve a 3.14)
Remove-Item -Recurse -Force .venv
& "C:\Users\matia\AppData\Local\Programs\Python\Python313\python.exe" -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# Importar JSONs existentes a SQLite
.\.venv\Scripts\python.exe backfill_db.py

# Regenerar embeds de YouTube en DB
.\.venv\Scripts\python.exe refresh_embeds.py

# Verificar que el app importa correctamente
.\.venv\Scripts\python.exe -c "from web.app import app; print('OK')"
```
