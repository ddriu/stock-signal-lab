"""Selección del almacenamiento del diario según el entorno."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re

import streamlit as st

from src.journal import TradingJournal, default_database_path
from src.supabase_journal import JournalStorageError, SupabaseTradingJournal


GROUP_PORTFOLIO_OWNER = "grupo_compartido"


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    secret_key: str
    table: str = "operations"
    favorites_table: str = "favorites"


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
    favorites_table = str(
        section.get("favorites_table")
        or os.getenv("SUPABASE_FAVORITES_TABLE", "favorites")
    ).strip()
    if not url and not secret_key:
        return None
    if not url or not secret_key:
        raise JournalStorageError(
            "La configuración de Supabase está incompleta: se necesitan URL y clave secreta."
        )
    return SupabaseConfig(
        url=url,
        secret_key=secret_key,
        table=table or "operations",
        favorites_table=favorites_table or "favorites",
    )


def create_journal(owner: str) -> TradingJournal | SupabaseTradingJournal:
    """Usa Supabase cuando está configurado; mantiene SQLite en instalaciones locales."""

    config = load_supabase_config()
    if config is None:
        safe_owner = re.sub(r"[^a-zA-Z0-9_-]+", "_", owner.strip().lower()).strip("_")
        if not safe_owner:
            raise JournalStorageError("El nombre de usuario no permite crear un diario local.")
        base_path = default_database_path()
        user_path = base_path.with_name(f"{base_path.stem}_{safe_owner}{base_path.suffix}")
        return TradingJournal(user_path)
    return SupabaseTradingJournal(
        config.url,
        config.secret_key,
        owner,
        table=config.table,
        favorites_table=config.favorites_table,
    )
