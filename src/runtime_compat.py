"""Refresh only incompatible, already-loaded analysis modules after deployment.

No network, storage, email, cache clearing or investment calculations happen
here. Ordinary Streamlit reruns retain current modules and session data.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from threading import RLock

RUNTIME_COMPAT_VERSION = "radar-runtime-v1"
JOURNAL_RESOURCE_VERSION = "radar-checkpoints-v1"

# Keep one lock for concurrent app reruns, including when this module itself
# was reloaded during a deployment.
if "_RADAR_RUNTIME_LOCK" not in globals():
    _RADAR_RUNTIME_LOCK = RLock()


ANALYSIS_MODULE_ORDER = (
    "src.data_sources", "src.data_loader", "src.analysis_data_quality",
    "src.analysis_policy", "src.growth_candidates", "src.daily_digest",
    "src.daily_digest_ui", "src.analysis_snapshots", "src.alert_runner",
)
_VERSIONS = {
    "src.data_sources": ("FUNDAMENTAL_PROVENANCE_VERSION", "fundamental_provenance_v2"),
    "src.data_loader": ("FUNDAMENTAL_SNAPSHOT_VERSION", "dated_statement_fallback_v2"),
    "src.analysis_data_quality": ("QUALITY_PRESENTATION_VERSION", "dated_quality_coverage_v2"),
    "src.daily_digest": ("DIGEST_PRESENTATION_VERSION", "compact-mobile-v3"),
    "src.analysis_snapshots": ("PRIMARY_PERSISTENCE_VERSION", "independent-primary-storage-v1"),
    "src.alert_runner": ("ALERT_RUNNER_VERSION", "home-preview-v1"),
}
_SIGNATURES = {
    "src.analysis_policy": ("build_policy_snapshot", "enrich_analysis_rows"),
    "src.growth_candidates": ("build_growth_candidate_rows",),
}
_FUNCTIONS = {
    "src.daily_digest": ("daily_score_reading", "daily_score_coverage", "daily_score_sort_key", "daily_score_overview"),
    "src.daily_digest_ui": ("daily_score_reading", "daily_score_coverage", "daily_score_sort_key", "daily_score_overview"),
}
_BINDINGS = (
    ("src.data_loader", "download_sec_fundamental_snapshot", "src.data_sources"),
    ("src.analysis_policy", "assess_analysis_data_quality", "src.analysis_data_quality"),
    ("src.growth_candidates", "assess_analysis_data_quality", "src.analysis_data_quality"),
    ("src.daily_digest_ui", "daily_score_reading", "src.daily_digest"),
    ("src.daily_digest_ui", "daily_score_overview", "src.daily_digest"),
    ("src.alert_runner", "download_fundamental_snapshot", "src.data_loader"),
    ("src.alert_runner", "build_daily_score_report", "src.daily_digest"),
    ("src.alert_runner", "render_daily_score_email", "src.daily_digest"),
)


def _incompatible_loaded_module() -> bool:
    for name, (marker, expected) in _VERSIONS.items():
        module = sys.modules.get(name)
        if module is not None and getattr(module, marker, None) != expected:
            return True
    for name, functions in _SIGNATURES.items():
        module = sys.modules.get(name)
        if module is None:
            continue
        for function_name in functions:
            try:
                if "fundamental_info" not in inspect.signature(getattr(module, function_name)).parameters:
                    return True
            except (AttributeError, TypeError, ValueError):
                return True
    for name, functions in _FUNCTIONS.items():
        module = sys.modules.get(name)
        if module is not None and any(not callable(getattr(module, key, None)) for key in functions):
            return True
    for consumer_name, symbol, source_name in _BINDINGS:
        consumer, source = sys.modules.get(consumer_name), sys.modules.get(source_name)
        if consumer is not None and source is not None and (
            getattr(consumer, symbol, None) is not getattr(source, symbol, None)
        ):
            return True
    return False


def prepare_analysis_runtime() -> tuple[str, ...]:
    """Repair the full dependency chain before app.py binds any of its APIs."""
    if not _incompatible_loaded_module():
        return ()
    refreshed = []
    # Preserve the existing company-name compatibility contract. A legacy
    # instruments module must be repaired before reloading its consumers.
    instruments = sys.modules.get("src.instruments")
    if instruments is not None and (
        getattr(instruments, "COMPANY_NAMES_VERSION", "") != "company-names-v1"
        or not callable(getattr(instruments, "resolve_company_name", None))
    ):
        importlib.reload(instruments)
        refreshed.append("src.instruments")
    for name in ANALYSIS_MODULE_ORDER:
        module = sys.modules.get(name)
        if module is None:
            importlib.import_module(name)
        else:
            importlib.reload(module)
        refreshed.append(name)
    return tuple(refreshed)


def _takes_keyword(function, name: str) -> bool:
    try:
        parameter = inspect.signature(function).parameters[name]
    except (KeyError, TypeError, ValueError):
        return False
    return parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)


def _radar_journal_contract(cls) -> bool:
    return (
        callable(getattr(cls, "get_radar_review_checkpoint", None))
        and _takes_keyword(getattr(cls, "save_radar_review_checkpoint", None), "expected_revision")
        and _takes_keyword(getattr(cls, "list_analysis_snapshots_for_sessions", None), "tickers")
    )


def prepare_radar_runtime() -> tuple[str, ...]:
    """Bind Radar to current APIs without touching I/O or unrelated modules.

    Streamlit can rerun the new entrypoint while old imports and backend
    instances remain in memory. Check only loaded contracts and repair their
    consumers in dependency order. The app's versioned resource key handles
    old backend instances; no global cache or session is cleared here.
    """
    refreshed = []

    def repair(name, needs_reload):
        module = sys.modules.get(name)
        if module is not None and needs_reload(module):
            importlib.reload(module)
            refreshed.append(name)

    with _RADAR_RUNTIME_LOCK:
        repair("src.journal", lambda m: not _radar_journal_contract(getattr(m, "TradingJournal", None)))
        repair("src.supabase_journal", lambda m: (
            not _radar_journal_contract(getattr(m, "SupabaseTradingJournal", None))
            or ("src.journal" in sys.modules and
                getattr(m, "radar_checkpoint_json", None) is not
                getattr(sys.modules["src.journal"], "radar_checkpoint_json", None))
        ))
        repair("src.storage", lambda m: any(
            source in sys.modules and getattr(m, symbol, None) is not
            getattr(sys.modules[source], symbol, None)
            for source, symbol in (("src.journal", "TradingJournal"),
                                   ("src.supabase_journal", "SupabaseTradingJournal"))
        ))
        repair("src.session_journal", lambda m: (
            "get_radar_review_checkpoint" not in getattr(m, "_UNCACHED_READ_METHODS", ())
        ))
        repair("src.hourly_market_schedule", lambda m: (
            getattr(m, "RADAR_CLOSE_CONTRACT_VERSION", "") != "radar-close-v1"
        ))
        repair("src.market_cache_ui", lambda m: (
            getattr(m, "MARKET_CACHE_UI_VERSION", "") != "radar-session-provenance-v1"
            or ("src.hourly_market_schedule" in sys.modules and
                getattr(m, "completed_daily_prices", None) is not
                getattr(sys.modules["src.hourly_market_schedule"], "completed_daily_prices", None))
        ))
        repair("src.analysis_snapshots", lambda m: not _takes_keyword(
            getattr(m, "persist_analysis_batch_snapshots", None), "existing_snapshots"
        ))
        repair("src.opportunity_catalog", lambda m: not _takes_keyword(
            getattr(m, "build_opportunity_catalog", None), "verified_review"
        ))
        repair("src.opportunity_status", lambda m: (
            getattr(m, "VERIFIED_RADAR_READING", None) != "Revisado y guardado"
        ))
    return tuple(refreshed)
