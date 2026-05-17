import html
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


_VERDICT_SLUGS = {
    "Exacto": "exacto",
    "Parcialmente exacto": "parcial",
    "Inexacto": "inexacto",
    "Ridiculo": "ridiculo",
}

_MONTHS_ES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  line-height: 1.6;
  color: #1f2937;
  background: #f9fafb;
}
main { max-width: 820px; margin: 0 auto; padding: 2rem 1.5rem; }
.page-header { border-bottom: 2px solid #e5e7eb; padding-bottom: 1.5rem; margin-bottom: 2rem; }
.brand {
  font-size: 0.75rem; letter-spacing: 0; color: #6b7280;
  margin-bottom: 0.5rem; font-weight: 700;
}
.page-header h1 { font-size: 1.75rem; line-height: 1.3; margin-bottom: 0.75rem; color: #111827; }
.meta { display: flex; flex-wrap: wrap; gap: 1rem 1.5rem; font-size: 0.875rem; color: #4b5563; }
.meta strong { color: #1f2937; }
.video-link { color: #2563eb; text-decoration: none; }
.video-link:hover { text-decoration: underline; }

h2 { font-size: 1.25rem; margin-bottom: 1rem; color: #111827; }

.summary {
  background: white; border-radius: 8px; padding: 1.5rem;
  margin-bottom: 2rem; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}
.tally { display: flex; flex-wrap: wrap; gap: 0.5rem; }

.badge {
  display: inline-block; padding: 0.25rem 0.75rem; border-radius: 999px;
  font-size: 0.8125rem; font-weight: 600; border: 1px solid;
}
.badge-exacto    { background: #dcfce7; color: #15803d; border-color: #86efac; }
.badge-parcial   { background: #fef3c7; color: #b45309; border-color: #fcd34d; }
.badge-inexacto  { background: #fee2e2; color: #b91c1c; border-color: #fca5a5; }
.badge-ridiculo  { background: #7f1d1d; color: #fee2e2; border-color: #991b1b; }
.badge-skipped   { background: #f3f4f6; color: #4b5563; border-color: #d1d5db; }

.scope-tag {
  display: inline-block; padding: 0.05rem 0.45rem; border-radius: 4px;
  font-size: 0.7rem; font-weight: 600; letter-spacing: 0;
  text-transform: uppercase; vertical-align: middle; margin-left: 0.4rem;
  border: 1px solid;
}
.scope-nacional      { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
.scope-internacional { background: #f5f3ff; color: #6d28d9; border-color: #ddd6fe; }
.scope-otro          { background: #f3f4f6; color: #4b5563; border-color: #d1d5db; }

.verdict-card {
  background: white; border-radius: 8px; padding: 1.5rem;
  margin-bottom: 1.5rem; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  border-left: 4px solid #d1d5db;
}
.verdict-card.v-exacto    { border-left-color: #22c55e; }
.verdict-card.v-parcial   { border-left-color: #f59e0b; }
.verdict-card.v-inexacto  { border-left-color: #ef4444; }
.verdict-card.v-ridiculo  { border-left-color: #991b1b; }

.vh { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; margin-bottom: 1rem; }
.confidence { font-size: 0.8125rem; color: #6b7280; }
.timestamp {
  margin-left: auto; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.8125rem; background: #1f2937; color: white;
  padding: 0.25rem 0.6rem; border-radius: 4px; text-decoration: none;
}
.timestamp:hover { background: #111827; }

blockquote.claim {
  font-size: 1.0625rem; border-left: 3px solid #d1d5db;
  padding-left: 1rem; margin: 0.5rem 0; color: #1f2937; font-style: italic;
}
.attribution { font-size: 0.875rem; color: #6b7280; margin-bottom: 1rem; }

.correction {
  margin: 1rem 0; padding: 1rem; background: #f9fafb; border-radius: 6px;
}
.correction h3, .sources h3 {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0;
  color: #6b7280; margin-bottom: 0.5rem; font-weight: 700;
}
.correction p { font-size: 0.9375rem; color: #1f2937; }

.sources { margin-top: 1rem; }
.sources ol { list-style: none; padding: 0; }
.source { padding: 0.75rem 0; border-bottom: 1px solid #f3f4f6; }
.source:last-child { border-bottom: none; }
.source-title {
  display: block; color: #2563eb; font-size: 0.9375rem;
  text-decoration: none; margin-bottom: 0.25rem;
}
.source-title:hover { text-decoration: underline; }
.source-date { font-size: 0.75rem; color: #9ca3af; }
.excerpt {
  margin-top: 0.5rem; padding: 0.5rem 0.75rem; background: #f9fafb;
  border-left: 2px solid #d1d5db; font-size: 0.875rem;
  color: #4b5563; font-style: italic;
}

.empty-note {
  background: white; border-radius: 8px; padding: 1.5rem;
  color: #6b7280; font-size: 0.95rem; line-height: 1.6;
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}

.skipped-section { margin-top: 3rem; }
.skipped-section .note { font-size: 0.875rem; color: #6b7280; margin-bottom: 1rem; }
.skipped-card {
  background: white; border-radius: 8px; padding: 1rem 1.5rem;
  margin-bottom: 1rem; border-left: 4px solid #d1d5db;
}
.skipped-card details { margin-top: 0.75rem; font-size: 0.875rem; color: #4b5563; }
.skipped-card summary { cursor: pointer; color: #6b7280; font-size: 0.8125rem; padding: 0.25rem 0; }
.skipped-card details p { margin-top: 0.5rem; padding-left: 0.5rem; border-left: 2px solid #e5e7eb; }

footer {
  margin-top: 3rem; padding-top: 2rem; border-top: 1px solid #e5e7eb;
  font-size: 0.8125rem; color: #6b7280; text-align: center;
}

@media (max-width: 600px) {
  main { padding: 1rem; }
  .page-header h1 { font-size: 1.4rem; }
  .timestamp { margin-left: 0; }
  .vh { gap: 0.5rem; }
}
"""


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _seconds_to_mmss(s: float | int | None) -> str:
    if s is None:
        return "--:--"
    total = int(s)
    m, sec = divmod(total, 60)
    return f"{m:02d}:{sec:02d}"


def _fmt_upload_date(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        d = datetime.strptime(raw, "%Y%m%d")
        return f"{d.day} de {_MONTHS_ES[d.month - 1]} de {d.year}"
    except Exception:
        return raw


def _youtube_url_at(video_id: str, t_start: float | int | None) -> str:
    t = int(t_start or 0)
    return f"https://www.youtube.com/watch?v={video_id}&t={t}s"


def _domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host.lower()


def _build_sources_index(sources: list[dict] | None) -> dict[str, dict]:
    index: dict[str, dict] = {}
    if not sources:
        return index
    for s in sources:
        if not isinstance(s, dict):
            continue
        domain = (s.get("domain") or "").lower()
        if domain:
            index[domain] = s
    return index


def _lookup_source_meta(url: str, sources_index: dict[str, dict]) -> dict | None:
    host = _domain_of(url)
    if not host:
        return None
    if host in sources_index:
        return sources_index[host]
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        suffix = ".".join(parts[i:])
        if suffix in sources_index:
            return sources_index[suffix]
    return None


def _scope_tag(scope: str | None) -> str:
    if not scope:
        return ""
    slug = scope if scope in ("nacional", "internacional") else "otro"
    label = {"nacional": "Nacional", "internacional": "Internacional"}.get(scope, scope.capitalize())
    return f'<span class="scope-tag scope-{slug}">{_esc(label)}</span>'


def _render_source(source: dict, sources_index: dict[str, dict]) -> str:
    meta = _lookup_source_meta(source.get("url", ""), sources_index)
    scope = (meta or {}).get("scope")
    org_name = (meta or {}).get("name")
    org_line = f'<span class="source-date">{_esc(org_name)}</span>' if org_name else ""
    return f"""<li class="source">
  <a href="{_esc(source.get('url'))}" target="_blank" rel="noopener" class="source-title">{_esc(source.get('title'))}</a>{_scope_tag(scope)}
  {org_line}
  <span class="source-date">consultado {_esc(source.get('retrieved_date'))}</span>
  <blockquote class="excerpt">&ldquo;{_esc(source.get('excerpt'))}&rdquo;</blockquote>
</li>"""


def _render_verdict_card(v: dict, video_id: str, sources_index: dict[str, dict]) -> str:
    slug = _VERDICT_SLUGS.get(v.get("verdict") or "", "otro")
    t_start = v.get("t_start", 0)
    conf_pct = int(round((v.get("confidence") or 0) * 100))
    sources = [s for s in (v.get("sources") or []) if isinstance(s, dict)]
    sources_html = "".join(_render_source(s, sources_index) for s in sources)
    speaker = v.get("speaker") or "Hablante no identificado"
    return f"""<article class="verdict-card v-{slug}">
  <header class="vh">
    <span class="badge badge-{slug}">{_esc(v.get('verdict'))}</span>
    <span class="confidence" title="Confianza basada en calidad y cantidad de evidencia">Confianza: {conf_pct}%</span>
    <a class="timestamp" href="{_esc(_youtube_url_at(video_id, t_start))}" target="_blank" rel="noopener">{_esc(_seconds_to_mmss(t_start))}</a>
  </header>
  <blockquote class="claim">&ldquo;{_esc(v.get('claim'))}&rdquo;</blockquote>
  <p class="attribution">&mdash; {_esc(speaker)}</p>
  <div class="correction">
    <h3>An&aacute;lisis</h3>
    <p>{_esc(v.get('correction'))}</p>
  </div>
  <div class="sources">
    <h3>Fuentes ({len(sources)})</h3>
    <ol>{sources_html}</ol>
  </div>
</article>"""


def _render_skipped_card(s: dict, video_id: str) -> str:
    t_start = s.get("t_start", 0)
    return f"""<article class="skipped-card">
  <header class="vh">
    <span class="badge badge-skipped">Sin verificar</span>
    <a class="timestamp" href="{_esc(_youtube_url_at(video_id, t_start))}" target="_blank" rel="noopener">{_esc(_seconds_to_mmss(t_start))}</a>
  </header>
  <blockquote class="claim">&ldquo;{_esc(s.get('claim'))}&rdquo;</blockquote>
  <details>
    <summary>Detalle de la b&uacute;squeda intentada</summary>
    <p>{_esc(s.get('search_summary'))}</p>
  </details>
</article>"""


def render_report_html(verdicts_payload: dict, sources: list[dict] | None = None) -> str:
    video = verdicts_payload.get("video", {})
    verdicts = verdicts_payload.get("verdicts", [])
    skipped = verdicts_payload.get("skipped_claims", [])

    video_id = video.get("id", "")
    title = video.get("title", "Sin titulo")
    channel = video.get("channel") or video.get("uploader") or "Sin canal"
    upload_date = _fmt_upload_date(video.get("upload_date"))
    webpage_url = video.get("webpage_url", "")

    sources_index = _build_sources_index(sources)

    tally = {"Exacto": 0, "Parcialmente exacto": 0, "Inexacto": 0, "Ridiculo": 0}
    for v in verdicts:
        label = v.get("verdict")
        if label in tally:
            tally[label] += 1

    if verdicts:
        verdict_cards_html = "\n".join(
            _render_verdict_card(v, video_id, sources_index) for v in verdicts
        )
    elif skipped:
        verdict_cards_html = (
            '<p class="empty-note">Ninguna de las afirmaciones detectadas pudo verificarse con las fuentes oficiales del allowlist. '
            'Ver detalle en la secci&oacute;n siguiente.</p>'
        )
    else:
        verdict_cards_html = (
            '<p class="empty-note">No se identificaron afirmaciones verificables en este contenido. '
            'El video puede ser de naturaleza puramente opin&aacute;tica, ret&oacute;rica, o demasiado breve '
            'para emitir afirmaciones contrastables con datos objetivos.</p>'
        )

    skipped_html = ""
    if skipped:
        skipped_cards_html = "\n".join(_render_skipped_card(s, video_id) for s in skipped)
        skipped_html = f"""<section class="skipped-section">
  <h2>Afirmaciones sin verificar ({len(skipped)})</h2>
  <p class="note">Estas afirmaciones no pudieron verificarse con las fuentes oficiales del allowlist. Se incluyen por transparencia.</p>
  {skipped_cards_html}
</section>"""

    meta_parts = [f"<span><strong>Canal:</strong> {_esc(channel)}</span>"]
    if upload_date:
        meta_parts.append(f"<span><strong>Publicado:</strong> {_esc(upload_date)}</span>")
    if webpage_url:
        meta_parts.append(
            f'<a href="{_esc(webpage_url)}" target="_blank" rel="noopener" class="video-link">Ver video original &rarr;</a>'
        )
    meta_html = "\n".join(meta_parts)

    skipped_badge = (
        f'<span class="badge badge-skipped">{len(skipped)} Sin verificar</span>' if skipped else ""
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PolitiCheck &mdash; {_esc(title)}</title>
  <style>{_CSS}</style>
</head>
<body>
  <main>
    <header class="page-header">
      <p class="brand">POLITICHECK</p>
      <h1>{_esc(title)}</h1>
      <div class="meta">{meta_html}</div>
    </header>

    <section class="summary">
      <h2>Resumen</h2>
      <div class="tally">
        <span class="badge badge-exacto">{tally['Exacto']} Exacto</span>
        <span class="badge badge-parcial">{tally['Parcialmente exacto']} Parcialmente exacto</span>
        <span class="badge badge-inexacto">{tally['Inexacto']} Inexacto</span>
        <span class="badge badge-ridiculo">{tally['Ridiculo']} Rid&iacute;culo</span>
        {skipped_badge}
      </div>
    </section>

    <section class="verdicts-section">
      <h2>Afirmaciones verificadas ({len(verdicts)})</h2>
      {verdict_cards_html}
    </section>

    {skipped_html}

    <footer>
      <p>Generado por <strong>PolitiCheck</strong> &middot; pipeline automatizado de fact-checking de discursos pol&iacute;ticos.</p>
    </footer>
  </main>
</body>
</html>"""


def write_report(
    verdicts_payload: dict,
    output_path: str | Path,
    sources: list[dict] | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_content = render_report_html(verdicts_payload, sources=sources)
    output_path.write_text(html_content, encoding="utf-8")
    return output_path
