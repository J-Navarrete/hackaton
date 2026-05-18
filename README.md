# DatoContraRelato

Pipeline automatizado de fact-checking de discursos políticos en video. Recibe una URL (YouTube, YouTube Shorts, TikTok, Instagram Reels) y produce un reporte HTML autocontenido con cada afirmación factual del video contrastada contra fuentes oficiales chilenas e internacionales.

## Cómo funciona

```
URL  →  audio.mp3  →  transcripción + timestamps  →  claims verificables (JSON)
                                                              ↓
        reporte HTML  ←  veredictos por claim  ←  research (web_search en allowlist)
```

7 pasos, ~1.5–2 min por video corto, ~USD 0.50 de tokens por video:

1. **Descarga** del audio (`yt-dlp`)
2. **Priming prompt dinámico** para Whisper (Claude Sonnet, lista de vocabulario)
3. **Transcripción** local con `faster-whisper` (large-v3, GPU si está disponible)
4. **Extracción** de claims verificables (Claude Sonnet, tool use → JSON estructurado)
5. **Investigación** paralela por claim (Claude + `web_search` restringido por `allowed_domains` a fuentes oficiales)
6. **Veredicto** por claim según taxonomía: *Exacto / Parcialmente exacto / Inexacto / Ridículo* (Claude Sonnet)
7. **Reporte HTML** autocontenido con timestamps clickables al video original

## Stack

- Python 3.11+
- `yt-dlp` (multi-plataforma: YouTube, Shorts, TikTok, Instagram Reels, etc.)
- `faster-whisper` (transcripción local, GPU opcional vía CUDA)
- `anthropic` (Claude Sonnet 4.6 para extracción, investigación, veredictos)
- `python-dotenv` (manejo de credenciales)

## Setup

### 1. Requisitos del sistema

- **Python 3.11+**
- **FFmpeg** (necesario para que yt-dlp convierta audio):
  - Windows: `winget install Gyan.FFmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `apt install ffmpeg`
- **(Opcional, GPU)** NVIDIA con drivers actualizados para aceleración de Whisper

### 2. Instalación

```bash
git clone https://github.com/USUARIO/politicheck.git
cd politicheck
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Aceleración GPU (opcional, recomendado)

`faster-whisper` corre por CPU por defecto. Para usar GPU NVIDIA:

```bash
pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
```

El pipeline auto-detecta CUDA al ejecutar y cae a CPU si algo falla. Para forzar un backend: `--device cuda` o `--device cpu`.

### 4. Credenciales

```bash
cp .env.example .env
# editar .env y poner ANTHROPIC_API_KEY=sk-ant-api03-...
```

Obtener una API key en https://console.anthropic.com/settings/keys.

## Uso

```bash
python main.py "https://www.youtube.com/watch?v=..."

# Forzar idioma (mejora calidad de transcripción si lo sabés):
python main.py "URL" --language es

# Cambiar tamaño de modelo Whisper (default: large-v3):
python main.py "URL" --model medium

# Override del priming prompt:
python main.py "URL" --initial-prompt "Ministerio de Hacienda, DIPRES, IPC, reajuste"

# Forzar device:
python main.py "URL" --device cpu     # forzar CPU
python main.py "URL" --device cuda    # forzar GPU
```

### Estructura de outputs

Cada corrida genera artefactos en `outputs/` (no versionados):

```
outputs/
├── audio/<video_id>.mp3
├── transcripts/<video_id>.json    # texto + segmentos con timestamps
├── claims/<video_id>.json         # claims verificables extraídos
├── research/<video_id>.json       # evidencia recopilada por claim
├── verdicts/<video_id>.json       # veredictos finales + correcciones
└── reports/<video_id>.html        # reporte para compartir
```

Cada paso guarda su propio JSON, así que si algo falla podés inspeccionar en qué paso y por qué.

## Fuentes oficiales

El archivo `sources.json` define el allowlist al que se restringe la búsqueda. Por defecto incluye:

- **40 fuentes nacionales** chilenas (Congreso, ministerios, Banco Central, INE, Servel, Consejo para la Transparencia, etc.)
- **7 fuentes internacionales** (CEPAL, FMI, Banco Mundial, OCDE, OMS, UNODC, PAHO)

Cada entrada tiene `domain`, `name`, `category` y `scope` (`"nacional"` o `"internacional"`). Editable directamente sin tocar código.

## Plataformas soportadas

- YouTube (videos y Shorts)
- TikTok
- Instagram Reels (cuentas públicas)
- Facebook, X/Twitter, Vimeo, y demás extractores de yt-dlp

## Diseño y decisiones

- **El texto se preserva literal** en la transcripción y los claims. No hay simplificación editorial, para evitar sesgo.
- **Un job de investigación por claim**, en paralelo via `asyncio`. Esto reduce la latencia total ~5x respecto a serial.
- **`web_search` restringido por `allowed_domains`**: el modelo no puede salir del allowlist, las citas vienen siempre de fuentes oficiales.
- **El reporte HTML es autocontenido** (CSS inline, sin JS, sin dependencias externas). Pesa ~25–40 KB.

## Limitaciones conocidas

- Whisper API local: el modelo `large-v3` necesita ~3 GB de descarga la primera vez (queda en `~/.cache/huggingface`).
- VRAM mínimo recomendado para GPU: 4 GB (usa `int8_float16` para fitear).
- Instagram con cuentas privadas requiere cookies (no implementado).
- Claims muy específicos sobre historia política sin huella documental en fuentes oficiales pueden quedar como "Sin verificar" (esto es deliberado: preferimos honestidad sobre alucinación).

## Licencia

MIT
