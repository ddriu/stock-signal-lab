"""Identidad visual ligera de Stock Signal Lab.

Los iconos son SVG propios e incrustados. No dependen de una fuente externa,
se ven nítidos en cualquier pantalla y no añaden peticiones de red.
"""

from __future__ import annotations


BRAND_MARK_SVG = """
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg"
     fill="none" aria-hidden="true" focusable="false">
  <path d="M13 44L25.5 31.5L35.5 38L50.5 20.5"
        stroke="currentColor" stroke-width="5" stroke-linecap="round"
        stroke-linejoin="round"/>
  <path d="M40.5 20.5H50.5V30.5" stroke="currentColor" stroke-width="5"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="13" cy="44" r="3.5" fill="currentColor"/>
  <circle cx="25.5" cy="31.5" r="3.5" fill="currentColor"/>
  <circle cx="35.5" cy="38" r="3.5" fill="currentColor"/>
</svg>
""".strip()


BRAND_FAVICON_SVG = """
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" fill="none">
  <defs>
    <linearGradient id="ssl-g" x1="8" y1="6" x2="58" y2="60"
                    gradientUnits="userSpaceOnUse">
      <stop stop-color="#0F3D3A"/>
      <stop offset="1" stop-color="#0A8F68"/>
    </linearGradient>
  </defs>
  <rect x="3" y="3" width="58" height="58" rx="18" fill="url(#ssl-g)"/>
  <path d="M13 44L25.5 31.5L35.5 38L50.5 20.5" stroke="white"
        stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M40.5 20.5H50.5V30.5" stroke="white" stroke-width="5"
        stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="13" cy="44" r="3.3" fill="white"/>
  <circle cx="25.5" cy="31.5" r="3.3" fill="white"/>
  <circle cx="35.5" cy="38" r="3.3" fill="white"/>
</svg>
""".strip()


_ICON_PATHS = {
    "home": (
        '<path d="M3.5 10.5L12 3.8l8.5 6.7v8.2a1.8 1.8 0 0 1-1.8 1.8H5.3a1.8 1.8 0 0 1-1.8-1.8z"/>'
        '<path d="M8.2 20.5v-6.2h7.6v6.2"/>'
    ),
    "analyze": (
        '<circle cx="10.3" cy="10.3" r="6.3"/>'
        '<path d="M15 15l5 5M7.2 12.2l2.1-2.1 1.8 1.3 2.6-3.2"/>'
    ),
    "favorite": (
        '<path d="M12 20.3S4 15.8 4 9.3A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 8 2.3c0 6.5-8 11-8 11z"/>'
        '<path d="M7.2 12.8l2.2-2.1 2 1.4 3.4-3.6"/>'
    ),
    "portfolio": (
        '<rect x="3.5" y="5" width="17" height="14.5" rx="3"/>'
        '<path d="M3.8 9h16.4M8 15v-2.2M12 15v-4M16 15v-6"/>'
    ),
    "alerts": (
        '<path d="M6.2 16.5h11.6l-1.5-2.2V9.7a4.3 4.3 0 0 0-8.6 0v4.6z"/>'
        '<path d="M10 19.2a2.2 2.2 0 0 0 4 0"/>'
        '<circle cx="18.3" cy="5.3" r="2.1" class="ssl-icon-dot"/>'
    ),
    "growth": (
        '<path d="M4 18L9.2 12.8l3.7 2.8L20 7"/>'
        '<path d="M14.5 7H20v5.5"/>'
    ),
    "more": (
        '<circle cx="5" cy="12" r="1.5" class="ssl-icon-dot"/>'
        '<circle cx="12" cy="12" r="1.5" class="ssl-icon-dot"/>'
        '<circle cx="19" cy="12" r="1.5" class="ssl-icon-dot"/>'
    ),
}


def brand_mark_html(class_name: str = "ssl-logo") -> str:
    """Devuelve el símbolo principal preparado para incrustarlo en HTML."""

    return f'<div class="{class_name}" aria-hidden="true">{BRAND_MARK_SVG}</div>'


def icon_html(name: str, class_name: str = "ssl-section-icon") -> str:
    """Icono lineal de sección con una alternativa de marca segura."""

    paths = _ICON_PATHS.get(name, _ICON_PATHS["growth"])
    return (
        f'<span class="{class_name}" aria-hidden="true">'
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" '
        'fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        f"{paths}</svg></span>"
    )


def contextual_icon(eyebrow: str, title: str = "") -> str:
    """Elige un icono coherente conservando texto accesible en la cabecera."""

    text = f"{eyebrow} {title}".casefold()
    if "inicio" in text or "hoy" in text:
        return "home"
    if "favorit" in text:
        return "favorite"
    if "cartera" in text or "capital" in text:
        return "portfolio"
    if "alert" in text:
        return "alerts"
    if "crecimiento" in text or "30+" in text:
        return "growth"
    if "más" in text or "guía" in text:
        return "more"
    return "analyze"
