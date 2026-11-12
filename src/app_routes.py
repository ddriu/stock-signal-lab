"""Stable user routes, separate from Streamlit widget and analysis state."""
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Route:
    key: str
    label: str
    group: str
    section: str
    selection: tuple[tuple[str, str], ...] = ()
    admin: bool = False

    @property
    def path(self) -> str:
        return "" if self.key == "hoy" else self.key


def analysis(key, label, group, view, detail=None):
    selections = [("analysis_navigation", view)]
    if detail:
        child = {
            "Empresa": "analysis_company_navigation",
            "Estrategias": "analysis_strategy_navigation",
            "Validar": "analysis_validation_navigation",
        }[view]
        selections.append((child, detail))
    return Route(key, label, group, "Analizar", tuple(selections))


PRIMARY_ROUTES = ("hoy", "cartera", "radar", "favoritas", "mas")
ROUTES = {
    route.key: route for route in (
        Route("hoy", "Hoy", "hoy", "Inicio"),
        Route("cartera", "Cartera", "cartera", "Carteras", (("portfolio_navigation", "Privada"),)),
        analysis("radar", "Radar", "radar", "Radar"),
        Route("favoritas", "Favoritas", "favoritas", "Favoritos", (("favorite_view", "Mis listas"),)),
        Route("mas", "Más", "mas", "Más", (("more_navigation", "Guía y riesgos"),)),
        Route("invertir", "Por horizonte", "hoy", "Invertir"),
        analysis("cinco-sesiones", "Cinco sesiones", "hoy", "Resumen diario"),
        analysis("entradas", "Entradas hoy", "radar", "Oportunidades"),
        analysis("empresa", "Empresa", "radar", "Empresa", "Análisis individual"),
        analysis("comparar", "Comparar", "radar", "Empresa", "Comparar empresas"),
        Route("analistas", "Analistas externos", "radar", "Analistas externos"),
        Route("favoritas-anadir", "Añadir empresa", "favoritas", "Favoritos", (("favorite_view", "Añadir empresa"),)),
        Route("cartera-grupo", "Grupo", "cartera", "Carteras", (("portfolio_navigation", "Grupo"),)),
        Route("cartera-plan", "Plan por posición", "cartera", "Carteras", (("portfolio_navigation", "Plan por posición"),)),
        Route("cartera-capital", "Plan de capital", "cartera", "Carteras", (("portfolio_navigation", "Plan de capital"),)),
        Route("resultados", "Resultados", "mas", "Resultados"),
        Route("laboratorio", "Laboratorio", "mas", "Analizar",
              (("analysis_navigation", "Panel anterior"), ("home_view_navigation", "Laboratorio"))),
        Route("alertas", "Alertas por correo", "mas", "Más", (("more_navigation", "Alertas por correo"),)),
        Route("administracion", "Administración", "mas", "Más",
              (("more_navigation", "Administración"),), admin=True),
        analysis("crecimiento", "Crecimiento y momentum", "mas", "Estrategias", "Crecimiento y momentum"),
        analysis("fundamentales", "Calidad fundamental", "mas", "Estrategias", "Calidad fundamental"),
        analysis("ventaja-relativa", "Ventaja relativa", "mas", "Estrategias", "Ventaja relativa"),
        analysis("especulativas", "Especulativas", "mas", "Estrategias", "Especulativas"),
        analysis("modelo", "Resultados del modelo", "mas", "Validar", "Resultados del modelo"),
        analysis("evolucion", "Evolución del análisis", "mas", "Validar", "Evolución del análisis"),
        analysis("resultado-posterior", "Resultado posterior", "mas", "Validar", "Resultado posterior"),
        analysis("backtest", "Backtest técnico", "mas", "Validar", "Backtest técnico"),
        analysis("auditoria", "Auditoría de estrategias", "mas", "Validar", "Auditoría de estrategias"),
    )
}

LOCAL_ROUTES = {
    "hoy": ("hoy", "invertir", "cinco-sesiones"),
    "cartera": ("cartera", "cartera-plan", "cartera-grupo", "cartera-capital"),
    "radar": ("radar", "entradas", "empresa", "comparar", "analistas"),
    "favoritas": ("favoritas", "favoritas-anadir"),
}
TOOLS_GROUPS = (
    ("Seguimiento", ("resultados", "laboratorio", "analistas", "alertas")),
    ("Métodos de análisis", ("crecimiento", "fundamentales", "ventaja-relativa", "especulativas")),
    ("Validar el método", ("modelo", "evolucion", "resultado-posterior", "backtest", "auditoria")),
)


def route_for_state(state: Mapping, *, admin: bool = False) -> Route:
    """Translate existing action callbacks once, before any widget is built."""
    section = state.get("main_navigation")
    for route in ROUTES.values():
        if route.section != section or (route.admin and not admin):
            continue
        if all(state.get(key) == value for key, value in route.selection
               if key != "home_view_navigation"):
            return route
    return ROUTES[{
        "Analizar": "radar", "Carteras": "cartera", "Favoritos": "favoritas", "Más": "mas",
    }.get(section, "hoy")]
