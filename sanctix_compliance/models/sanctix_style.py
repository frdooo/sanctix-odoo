"""Sanctix visual language for Odoo views.

Mirrors the dashboard's verdict tokens (sanctix-frontend/app/globals.css
`--verdict-*`, light mode) so risk status looks the same in Odoo as on
sanctix.com: a small semibold pill with tinted bg + border. Odoo list
decorations can only tint text, so form/report headers use a computed Html
field rendered by `risk_pill_html()` with inline styles (no external CSS
needed, works on 17/18/19).

Unknown/empty risk fails safe to the caution (amber) tone, same as the
dashboard (`verdict.tsx` VERDICT_TONE falls back to caution, never clear).
"""

from __future__ import annotations

import html

# fg / bg / border, copied from the frontend --verdict-*-* tokens (light).
_VERDICT_TONES = {
    "blocked": {"fg": "#991B1B", "bg": "#FDF0F0", "border": "#F3C9C9", "glyph": "⛔"},
    "review": {"fg": "#9A3412", "bg": "#FDF3EC", "border": "#F3D4BD", "glyph": "⚠"},
    "caution": {"fg": "#8A5300", "bg": "#FDF7E8", "border": "#EFDCAE", "glyph": "●"},
    "clear": {"fg": "#186A3B", "bg": "#EEF8F1", "border": "#C3E3CE", "glyph": "✓"},
    "pending": {"fg": "#3D4757", "bg": "#F1F3F7", "border": "#D8DDE5", "glyph": "○"},
}

# Odoo risk_level -> dashboard tone. "error" (failed call, not a verdict)
# uses review/amber: it needs action, and must never look clear or blocked
# (blocked would imply a confirmed sanctions hit).
_RISK_TO_TONE = {
    "match": "blocked",
    "high": "review",
    "medium": "review",
    "error": "review",
    "low": "caution",
    "clear": "clear",
    "unscreened": "pending",
}


def risk_pill_html(risk_level: str | None, label: str) -> str:
    """Render a Sanctix-style verdict pill as an inline-styled span."""
    tone = _VERDICT_TONES[_RISK_TO_TONE.get(risk_level or "", "caution")]
    safe_label = html.escape(label or "")
    return (
        '<span style="display:inline-flex;align-items:center;gap:6px;white-space:nowrap;'
        "border:1px solid %s;border-radius:3px;padding:2px 8px;"
        "font-size:11px;font-weight:600;"
        "background:%s;color:%s;"
        '">' '<span>%s</span><span>%s</span></span>'
    ) % (tone["border"], tone["bg"], tone["fg"], tone["glyph"], safe_label)


# Worst-first severity order for rolling several party verdicts up to one
# partner status (document screening screens N parties per call).
_RISK_SEVERITY = {"clear": 0, "low": 1, "medium": 2, "high": 3, "match": 4}


def worse_risk(first: str, second: str) -> str:
    """Return whichever risk_level is more severe."""
    if _RISK_SEVERITY.get(first, -1) >= _RISK_SEVERITY.get(second, -1):
        return first
    return second


def score_donut_svg(score: int | None, risk_level: str | None) -> str:
    """Small SVG score ring in the platform's style (recharts-like pie).

    Pure inline SVG so it renders inside Odoo Html fields with no JS/CSS
    asset. `score` 0-100; None renders an empty ring.
    """
    tone = _VERDICT_TONES[_RISK_TO_TONE.get(risk_level or "", "caution")]
    pct = max(0, min(100, int(score))) if isinstance(score, int) else 0
    # circle r=34, circumference ~213.6
    dash = pct * 213.6 / 100
    label = str(pct) if isinstance(score, int) else "—"
    return (
        '<svg width="76" height="76" viewBox="0 0 76 76">'
        '<circle cx="38" cy="38" r="34" fill="none" stroke="#E8EBF0" stroke-width="9"/>'
        '<circle cx="38" cy="38" r="34" fill="none" stroke="%s" stroke-width="9"'
        ' stroke-linecap="round" stroke-dasharray="%.1f 213.6" transform="rotate(-90 38 38)"/>'
        '<text x="38" y="44" text-anchor="middle" font-size="20" font-weight="700"'
        ' font-family="-apple-system,Segoe UI,Inter,Roboto,Arial,sans-serif" fill="%s">%s</text>'
        "</svg>" % (tone["fg"], dash, tone["fg"], label)
    )


def markdown_to_html(text: str | None) -> str:
    """Minimal, safe markdown renderer for Sanctix AI memos.

    Supports exactly: `#`/`##`/`###` headings, `- ` bullets, `**bold**`,
    `` `code` ``, and paragraphs. Everything is HTML-escaped FIRST, so only
    the tags below can ever reach the page — safe for t-raw/QWeb use.
    """
    import re

    if not text:
        return ""
    blocks: list[str] = []
    para: list[str] = []
    in_list = False

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`(.+?)`", r"<code style='background:#F1F5F9;padding:0 4px;border-radius:3px;'>\1</code>", s)
        return s

    def flush_para() -> None:
        if para:
            blocks.append("<p style='font-size:13px;line-height:1.6;color:#0B1220;margin:0 0 8px 0;'>%s</p>" % " ".join(para))
            para.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            blocks.append("</ul>")
            in_list = False

    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            flush_para()
            close_list()
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush_para()
            close_list()
            level = len(heading.group(1))
            size = {1: 15, 2: 13, 3: 12}[level]
            blocks.append(
                "<div style='font-size:%dpx;font-weight:700;color:#0B1220;margin:10px 0 4px 0;'>%s</div>"
                % (size, inline(heading.group(2)))
            )
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", line)
        if bullet:
            flush_para()
            if not in_list:
                blocks.append("<ul style='font-size:13px;line-height:1.6;color:#0B1220;margin:0 0 8px 0;padding-left:18px;'>")
                in_list = True
            blocks.append("<li>%s</li>" % inline(bullet.group(1)))
            continue
        close_list()
        para.append(inline(line))
    flush_para()
    close_list()
    return "".join(blocks)


def sanctix_logo_data_uri() -> str | None:
    """Sanctix shield mark (copied from the platform frontend) as an SVG data
    URI for dashboard/PDF headers. None when the asset is missing — callers
    fall back to the text wordmark. Decorative: never raises."""
    try:
        import base64

        from odoo.modules import get_module_resource

        path = get_module_resource("sanctix_compliance", "static", "src", "img", "sanctix_logo.svg")
        if not path:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
        if b"<svg" not in raw[:500]:
            return None
        return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:  # noqa: BLE001 — decorative only
        return None


FONT_STACK = "-apple-system,Segoe UI,Inter,Roboto,Arial,sans-serif"
