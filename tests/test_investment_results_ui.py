"""Private result landing: synthetic sealed ledger, no DB, providers or SMTP."""
from streamlit.testing.v1 import AppTest


SCRIPT = '''
from unittest.mock import patch
import streamlit as st
import src.investment_results_ui as ui

scenario = SCENARIO
owner = "private-owner@example.invalid"
if st.checkbox("Cambiar propietario de prueba"):
    owner = "other-owner@example.invalid"

def count(name):
    st.session_state[name] = st.session_state.get(name, 0) + 1

class Journal:
    def __init__(self):
        self.owner = owner
    def __getattr__(self, name):
        raise AssertionError("No portfolio, operation, source or mail access: " + name)

class Store:
    def __init__(self):
        self.owner = owner
    def list_seasons(self):
        count("season_reads")
        if scenario == "empty":
            return []
        if scenario == "failure":
            raise RuntimeError("private-owner@example.invalid SECRET")
        rows = []
        for experiment, source, cohort in (("experiment-a", "technical", "raw"),
                                            ("experiment-b", "growth", "verified")):
            for mode in ("budget", "theoretical"):
                for strategy in "ABCD":
                    manifest = dict(experiment_id=experiment, source=source, cohort=cohort,
                        mode=mode, strategy_key=strategy, engine_version="frozen-test",
                        activation_at="2026-10-20T21:00:00Z")
                    row = dict(owner=owner, season_id=experiment+"/"+mode+"/"+strategy,
                               manifest=manifest, manifest_hash=ui.content_hash(manifest))
                    rows.append(row)
        if scenario == "wrong-owner":
            rows[0]["owner"] = "third-owner@example.invalid"
        if scenario == "bad-manifest":
            rows[0]["manifest"]["cohort"] = "changed after sealing"
        return rows
    def select(self, kind, *, season_id=None):
        assert kind == "snapshots" and season_id
        count("snapshot_reads")
        st.session_state["selected_seasons"] = st.session_state.get("selected_seasons", []) + [season_id]
        source = "technical" if season_id.startswith("experiment-a") else "growth"
        cohort = "raw" if source == "technical" else "verified"
        data = dict(available_at="2026-10-23T22:00:00Z", snapshot_kind="market_collection",
                    coverage_complete=True, source=source, cohort=cohort, engine_version="frozen-test",
                    metrics=dict(net_return_pct=12.5, max_drawdown_daily_approx_pct=-4.25,
                                 coverage_complete=True))
        if scenario == "initial":
            data.update(snapshot_kind="activation", coverage_complete=False)
        if scenario == "incomplete":
            data["coverage_complete"] = False
        if scenario == "bad-metrics":
            data["metrics"].update(net_return_pct=float("inf"), max_drawdown_daily_approx_pct=True)
            # Synthetic malformed metrics cannot be sealed by the real store;
            # use a JSON-safe non-number to exercise the renderer as well.
            data["metrics"]["net_return_pct"] = "not a number"
        if scenario == "different-source":
            data["source"] = "growth" if source == "technical" else "technical"
        row = dict(owner=owner, season_id=season_id, snapshot=data,
                   available_at=data["available_at"], commit_sequence=1,
                   snapshot_hash=ui.content_hash(data))
        if scenario == "cross-snapshot":
            row["owner"] = "third-owner@example.invalid"
        if scenario == "bad-snapshot":
            data["metrics"]["net_return_pct"] = 99
        if scenario == "latest-incomplete":
            new_data = dict(data, available_at="2026-10-26T22:00:00Z", coverage_complete=False)
            return [dict(row, snapshot=new_data, available_at=new_data["available_at"],
                         snapshot_hash=ui.content_hash(new_data), commit_sequence=2), row]
        return [row]
    def __getattr__(self, name):
        raise AssertionError("No activation, append, events or all-history read: " + name)

def factory(journal):
    count("store_factories")
    return Store()

def action(name, ticker):
    st.session_state["actions"] = st.session_state.get("actions", []) + [(name, ticker)]

with patch.object(ui, "create_model_results_store", side_effect=factory):
    ui.render_investment_results(Journal(), on_action=action)
'''


def page(scenario="complete"):
    return AppTest.from_string(SCRIPT.replace("SCENARIO", repr(scenario)), default_timeout=30).run()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def visible(app):
    return " ".join(str(item.value) for kind in
        ("markdown", "caption", "warning", "info", "error", "success", "dataframe")
        for item in getattr(app, kind))


def consult(app):
    return button(app, "Consultar seguimiento guardado").click().run()


def test_opening_and_navigating_results_is_inert_and_does_not_expose_private_owner():
    app = page()
    assert not app.exception
    assert "store_factories" not in app.session_state
    assert "private-owner@example.invalid" not in visible(app)
    button(app, "Ver mi rentabilidad real").click().run()
    assert app.session_state["actions"] == [("real_returns", None)]
    button(app, "Ver índices, operaciones y auditoría del modelo").click().run()
    assert app.session_state["actions"][-1] == ("model_details", None)
    assert "store_factories" not in app.session_state


def test_explicit_consult_reads_only_four_selected_seasons_and_reruns_are_inert():
    app = consult(page())
    assert not app.exception and not app.error
    assert app.session_state["store_factories"] == app.session_state["season_reads"] == 1
    assert app.session_state["snapshot_reads"] == 4
    assert len(set(app.session_state["selected_seasons"])) == 4
    frame = app.dataframe[0].value
    assert frame["Estrategia"].tolist() == list("ABCD")
    assert frame["Rentabilidad neta"].tolist() == ["+12.50%"] * 4
    assert frame["Caída máxima aprox."].tolist() == ["-4.25%"] * 4
    assert "No se declara un ganador" in visible(app)
    assert "private-owner@example.invalid" not in visible(app)
    app.run()
    assert app.session_state["store_factories"] == 1 and app.session_state["snapshot_reads"] == 4


def test_mode_or_experiment_changes_do_not_read_or_reuse_another_cohort():
    app = consult(page())
    app.selectbox[1].select("theoretical").run()
    assert not app.exception
    assert app.session_state["store_factories"] == 1 and app.session_state["snapshot_reads"] == 4
    assert set(app.dataframe[0].value["Rentabilidad neta"]) == {"N/D"}
    consult(app)
    assert app.session_state["store_factories"] == 2 and app.session_state["snapshot_reads"] == 8
    assert all("/theoretical/" in value for value in app.session_state["selected_seasons"][-4:])
    new_group = next(value for value in app.selectbox[0].options if "Crecimiento + momentum" in value)
    # Streamlit's options are formatted labels; choose by label index.
    app.selectbox[0].select_index(app.selectbox[0].options.index(new_group)).run()
    assert app.session_state["store_factories"] == 2 and app.session_state["snapshot_reads"] == 8
    assert set(app.dataframe[0].value["Rentabilidad neta"]) == {"N/D"}


def test_activation_incomplete_latest_and_bad_metrics_never_become_measured_returns():
    for scenario in ("initial", "incomplete", "latest-incomplete", "bad-metrics"):
        app = consult(page(scenario))
        assert not app.exception and not app.error, scenario
        assert set(app.dataframe[0].value["Rentabilidad neta"]) == {"N/D"}, scenario
        assert set(app.dataframe[0].value["Caída máxima aprox."]) == {"N/D"}, scenario
    app = consult(page("latest-incomplete"))
    assert all(value.startswith("2026-10-26") for value in app.dataframe[0].value["Última recogida UTC"])


def test_empty_missing_or_unverified_store_is_clear_and_does_not_leak_errors():
    for scenario in ("empty", "failure", "wrong-owner", "bad-manifest"):
        app = consult(page(scenario))
        assert not app.exception, scenario
        assert "snapshot_reads" not in app.session_state, scenario
        assert "private-owner@example.invalid" not in visible(app), scenario
        assert "SECRET" not in visible(app), scenario


def test_cross_owner_or_tampered_snapshot_does_not_display_plausible_returns():
    for scenario in ("cross-snapshot", "bad-snapshot", "different-source"):
        app = consult(page(scenario))
        assert not app.exception, scenario
        assert app.error, scenario
        assert set(app.dataframe[0].value["Rentabilidad neta"]) == {"N/D"}, scenario
        assert "private-owner@example.invalid" not in visible(app), scenario


def test_owner_switch_does_not_show_or_read_previous_owners_results():
    app = consult(page())
    assert app.dataframe
    app.checkbox[0].check().run()
    assert not app.exception and not app.dataframe
    assert app.session_state["store_factories"] == 1 and app.session_state["snapshot_reads"] == 4
    assert "other-owner@example.invalid" not in visible(app)


def test_actual_sqlite_tracking_records_render_without_changing_the_ledger(tmp_path):
    """Use production activation/replay/store contracts, not a shaped fake."""
    from src.journal import TradingJournal
    from src.model_results_store import ModelResultsStore
    from src.model_results_tracking import advance_model_experiment
    from test_model_results_tracking import activate, signals, quotes

    path = tmp_path / "tracking.sqlite"
    journal = TradingJournal(path, owner="sqlite-test-owner")
    journal.add_operation("REAL", "Compra", 1, 100, 1, "2026-10-01")
    experiment = activate(journal)
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        advance_model_experiment(journal, experiment, signals(), quotes(day), now=day + "T21:00:00Z")
    store = ModelResultsStore(journal)
    before = {kind: store.select(kind) for kind in ("seasons", "snapshots", "events", "batches")}
    operations = journal.list_operations().to_dict("records")
    assert any(row["snapshot"].get("snapshot_kind") == "market_collection"
               and row["snapshot"].get("coverage_complete") is True for row in before["snapshots"])
    script = '''
from types import SimpleNamespace
from unittest.mock import patch
import streamlit as st
import src.investment_results_ui as ui

journal = SimpleNamespace(database_path=PATH, owner="sqlite-test-owner")
factory = ui.create_model_results_store
def create(journal):
    st.session_state["store_factories"] = st.session_state.get("store_factories", 0) + 1
    return factory(journal)
with patch.object(ui, "create_model_results_store", side_effect=create), \\
     patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")), \\
     patch("smtplib.SMTP", side_effect=AssertionError("SMTP forbidden")):
    ui.render_investment_results(journal)
'''.replace("PATH", repr(str(path)))
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception and "store_factories" not in app.session_state
    consult(app)
    assert not app.exception and not app.error
    assert app.session_state["store_factories"] == 1
    frame = app.dataframe[0].value
    assert set(frame["Cobertura"]) == {"Completa según el registro"}
    assert any(value != "N/D" for value in frame["Rentabilidad neta"])
    app.run()
    assert app.session_state["store_factories"] == 1
    assert {kind: store.select(kind) for kind in before} == before
    assert journal.list_operations().to_dict("records") == operations
