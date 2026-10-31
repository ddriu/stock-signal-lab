"""Refresh only incompatible, already-loaded analysis modules after deployment.

No network, storage, email, cache clearing or investment calculations happen
here. Ordinary Streamlit reruns retain current modules and session data.
"""
from __future__ import annotations

import importlib
import inspect
import sys


ANALYSIS_MODULE_ORDER = (
    "src.data_sources", "src.data_loader", "src.analysis_data_quality",
    "src.analysis_policy", "src.growth_candidates", "src.daily_digest",
    "src.daily_digest_ui", "src.alert_runner",
)
_VERSIONS = {
    "src.data_sources": ("FUNDAMENTAL_PROVENANCE_VERSION", "fundamental_provenance_v2"),
    "src.data_loader": ("FUNDAMENTAL_SNAPSHOT_VERSION", "dated_statement_fallback_v2"),
    "src.analysis_data_quality": ("QUALITY_PRESENTATION_VERSION", "dated_quality_coverage_v2"),
    "src.daily_digest": ("DIGEST_PRESENTATION_VERSION", "virtual-summary-v1"),
}
_SIGNATURES = {
    "src.analysis_policy": ("build_policy_snapshot", "enrich_analysis_rows"),
    "src.growth_candidates": ("build_growth_candidate_rows",),
}
_FUNCTIONS = {
    "src.daily_digest": ("daily_score_reading", "daily_score_coverage", "daily_score_sort_key"),
    "src.daily_digest_ui": ("daily_score_reading", "daily_score_coverage", "daily_score_sort_key"),
}
_BINDINGS = (
    ("src.data_loader", "download_sec_fundamental_snapshot", "src.data_sources"),
    ("src.analysis_policy", "assess_analysis_data_quality", "src.analysis_data_quality"),
    ("src.growth_candidates", "assess_analysis_data_quality", "src.analysis_data_quality"),
    ("src.daily_digest_ui", "daily_score_reading", "src.daily_digest"),
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
