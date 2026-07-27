"""Selección del almacenamiento del diario según el entorno."""

from __future__ import annotations

from dataclasses import dataclass
import os

import streamlit as st

from src.journal import TradingJournal
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    secret_key: str
    table: str = "operations"


def _secret_section(name: str) -> dict[str, object]:
    try:
        section = st.secrets.get(name, {})
    except (FileNotFoundError, RuntimeError):
        return {}
    return dict(section) if section else {}


def load_supabase_config() -> SupabaseConfig | None:
    """Lee la conexión sin exponerla al cliente ni almacenarla en Git."""

    section = _secret_section("supabase")
    url = str(section.get("url") or os.getenv("SUPABASE_URL", "")).strip()
    secret_key = str(
        section.get("secret_key")
        or section.get("service_role_key")
        or os.getenv("SUPABASE_SECRET_KEY", "")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    ).strip()
    table = str(section.get("table") or os.getenv("SUPABASE_TABLE", "operations")).strip()
    if not url and not secret_key:
        return None
    if not url or not secret_key:
        raise JournalStorageError(
            "La configuración de Supabase está incompleta: se necesitan URL y clave secreta."
        )
    return SupabaseConfig(url=url, secret_key=secret_key, table=table or "operations")


def create_journal(owner: str) -> TradingJournal | SupabaseTradingJournal:
    """Usa Supabase cuando está configurado; mantiene SQLite en instalaciones locales."""

    config = load_supabase_config()
    if config is None:
        return TradingJournal()
    return SupabaseTradingJournal(
        config.url,
        config.secret_key,
        owner,
        table=config.table,
    )
