"""Repair only stale read adapters after a hot deployment, without I/O."""
from __future__ import annotations

import importlib
import sys

INVESTMENT_RUNTIME_VERSION = "home-review-v3"

def prepare_investment_runtime() -> tuple[str, ...]:
    refreshed = []
    changed = False
    for name, cls in (("src.journal", "TradingJournal"),
                      ("src.supabase_journal", "SupabaseTradingJournal")):
        module = sys.modules.get(name)
        if module is not None and any(not callable(getattr(getattr(module, cls, None), method, None))
                for method in ("get_latest_daily_score_report", "list_analysis_snapshots_for_sessions")):
            importlib.reload(module)
            refreshed.append(name)
            changed = True
    if changed and (module := sys.modules.get("src.storage")) is not None:
        importlib.reload(module)
        refreshed.append("src.storage")
    module = sys.modules.get("src.session_journal")
    if module is not None and not {"get_latest_daily_score_report", "list_analysis_snapshots_for_sessions"}.issubset(
            getattr(module, "_READ_METHODS", ())):
        importlib.reload(module)
        refreshed.append("src.session_journal")
    workspace = sys.modules.get("src.investment_workspace")
    ui = sys.modules.get("src.investment_workspace_ui")
    workspace_changed = workspace is not None and getattr(workspace, "WORKSPACE_VERSION", "") != "investment-workspace-v2"
    if workspace_changed:
        importlib.reload(workspace)
        refreshed.append("src.investment_workspace")
    if ui is not None and (workspace_changed or getattr(ui, "WORKSPACE_UI_VERSION", "") != "investment-workspace-ui-v3"):
        importlib.reload(ui)
        refreshed.append("src.investment_workspace_ui")
    return tuple(refreshed)
