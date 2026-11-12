"""Synthetic private owners only. No network, mailbox or production data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import sqlite3

import pytest

from src.external_analyst_store import ExternalAnalystConflict, ExternalAnalystStore


def message(**changes):
    value = {
        "message_id": "<synthetic-1@example.invalid>", "provider": "perito_inversor",
        "received_at": "2026-11-12T09:00:00+00:00", "observed_at": "2026-11-12T09:01:00+00:00",
        "announced_count": 3, "issues": ["Se anunciaron tres; solo una quedó documentada."],
        "calls": [{"source_id": "example-call-1", "ticker": "SYN", "currency": "USD",
                   "company_name": "Synthetic Company", "analyst": "Analista sintético",
                   "published_at": "2026-11-12T08:00:00+00:00", "action": "buy", "direction": "long",
                   "entry_rule": "limit", "entry": 10, "stop": 9, "target": 13,
                   "source_status": "pending", "source_url": "https://peritoinversor.com/synthetic",
                   "evidence": {"fields": ["Precio y niveles publicados"]}}],
    }
    value.update(changes)
    return value


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "synthetic.db"
    return tuple(ExternalAnalystStore(SimpleNamespace(owner=owner, database_path=path))
                 for owner in ("synthetic-alice", "synthetic-bob"))


def test_import_metadata_and_owner_isolation(stores):
    alice, bob = stores
    assert alice.import_message(message()) == {"inserted": 1, "revised": 0, "duplicates": 0, "message_duplicate": False}
    assert bob.list_calls() == bob.list_events() == bob.list_messages() == []
    assert bob.import_message(message())["inserted"] == 1
    assert alice.list_calls()[0]["ticker"] == "SYN"
    row = alice.list_messages()[0]
    assert row["announced_count"] == 3 and row["extracted_count"] == 1
    assert "example.invalid" not in str(row)
    assert "synthetic-1" not in str(row)
    assert len(row["message_key"]) == 64


def test_duplicates_and_new_revision_keep_new_observation_clock(stores):
    store, _ = stores
    first = message()
    store.import_message(first)
    call_id = store.list_calls()[0]["id"]
    repeated = deepcopy(first)
    repeated["observed_at"] = "2026-11-12T09:20:00Z"
    assert store.import_message(repeated)["message_duplicate"] is True
    repeated["message_id"] = "second-copy"
    assert store.import_message(repeated)["duplicates"] == 1
    assert len(store.list_revisions()) == 1
    amended = deepcopy(first)
    amended.update(message_id="revised-levels", received_at="2026-11-12T10:00:00Z", observed_at="2026-11-12T10:01:00Z")
    amended["calls"][0]["stop"] = 9.5
    assert store.import_message(amended)["revised"] == 1
    revisions = store.list_revisions(call_id)
    assert len(revisions) == 2
    assert revisions[0]["call"]["stop"] == 9
    latest = store.list_calls()[0]
    assert latest["stop"] == 9.5
    assert latest["observed_at"].startswith("2026-11-12T10:01")
    assert latest["first_observed_at"].startswith("2026-11-12T09:01")
    assert latest["published_at"].startswith("2026-11-12T08:00")


def test_conflicting_message_and_bad_batch_are_atomic(stores):
    store, _ = stores
    store.import_message(message())
    amended = message()
    amended["calls"][0]["entry"] = 11
    with pytest.raises(ExternalAnalystConflict):
        store.import_message(amended)
    invalid = message(message_id="bad-batch")
    invalid["calls"].append({"ticker": "NO_SOURCE"})
    with pytest.raises(ValueError):
        store.import_message(invalid)
    assert len(store.list_messages()) == len(store.list_revisions()) == len(store.list_calls()) == 1
    assert store.list_calls()[0]["entry"] == 10


def test_csv_original_receipt_is_preserved_but_observation_cannot_be_backdated(stores):
    store, _ = stores
    data = message()
    data["calls"][0].update(received_at="2026-11-10T08:00:00Z", observed_at="2026-11-10T08:00:01Z")
    store.import_message(data)
    imported = store.list_calls()[0]
    assert imported["received_at"].startswith("2026-11-10T08:00")
    assert imported["observed_at"].startswith("2026-11-12T09:01")


def test_storage_is_append_only_even_for_direct_database_writes(stores):
    store, _ = stores
    store.import_message(message())
    call_id = store.list_calls()[0]["id"]
    store.append_event(call_id, "event-1", {"status": "pending"}, "2026-11-12T09:02:00Z")
    store.save_simulation_run("sim-1", {"observed_at": "2026-11-12T10:00:00Z", "equity_eur": 100})
    with sqlite3.connect(store.path) as db:
        for table in ("messages", "calls", "revisions", "events", "simulation_runs"):
            for operation in ("DELETE FROM", "UPDATE"):
                sql = (f"{operation} external_analyst_{table}" if operation == "DELETE FROM" else
                       f"UPDATE external_analyst_{table} SET owner='synthetic-tamper'")
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    db.execute(sql)


def test_events_are_scoped_idempotent_and_cannot_predict_the_past(stores):
    store, bob = stores
    store.import_message(message())
    call_id = store.list_calls()[0]["id"]
    with pytest.raises(ValueError):
        store.append_event(call_id, "pre-known", {}, "2026-11-12T09:00:00.999999Z")
    with pytest.raises(ValueError):
        bob.append_event(call_id, "foreign", {}, "2026-11-12T10:00:00Z")
    assert store.append_event(call_id, "activation-1", {"status": "open"}, "2026-11-12T10:00:00Z")
    assert not store.append_event(call_id, "activation-1", {"status": "open"}, "2026-11-12T10:00:00Z")
    with pytest.raises(ExternalAnalystConflict):
        store.append_event(call_id, "activation-1", {"status": "stopped"}, "2026-11-12T10:00:00Z")
    assert store.list_calls()[0]["tracking_status"] == "open"


def test_late_arriving_history_cannot_replace_a_newer_revision_or_tracking_state(stores):
    store, _ = stores
    original = message()
    store.import_message(original)
    call_id = store.list_calls()[0]["id"]
    newest = deepcopy(original)
    newest.update(message_id="newest", observed_at="2026-11-12T11:00:00Z")
    newest["calls"][0]["stop"] = 9.5
    store.import_message(newest)
    late = deepcopy(original)
    late.update(message_id="late-arriving", observed_at="2026-11-12T10:00:00Z")
    late["calls"][0]["stop"] = 9.2
    store.import_message(late)
    assert len(store.list_revisions(call_id)) == 3
    assert store.list_calls()[0]["stop"] == 9.5
    store.append_event(call_id, "open", {"status": "open"}, "2026-11-12T12:00:00Z")
    store.append_event(call_id, "late-activation", {"status": "activated"}, "2026-11-12T11:30:00Z")
    assert len(store.list_events()) == 2
    assert store.list_calls()[0]["tracking_status"] == "open"


@pytest.mark.parametrize("payload", [{"password": "hidden"}, {"evidence": {"recipient": "hidden"}},
                                     {"content": "copy to user@example.invalid"}, {"raw_email": "mail"}])
def test_sensitive_or_raw_data_never_stored(stores, payload):
    store, _ = stores
    with pytest.raises(ValueError):
        store.claim_delivery("private-test", payload)
    assert store.list_deliveries() == []


def test_delivery_claim_is_atomic_and_terminal_outcomes_cannot_be_replayed(stores):
    store, bob = stores
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: store.claim_delivery("same-report", {"calls": 1}), range(16)))
    assert sum(outcomes) == 1
    assert bob.claim_delivery("same-report", {"calls": 1})
    store.finish_delivery("same-report", "uncertain")
    store.finish_delivery("same-report", "uncertain")
    assert not store.claim_delivery("same-report", {"calls": 1})
    with pytest.raises(ExternalAnalystConflict):
        store.finish_delivery("same-report", "sent")
    assert store.list_deliveries()[0]["status"] == "uncertain"
    assert bob.list_deliveries()[0]["status"] == "reserved"


def test_preferences_default_disabled_and_do_not_overwrite_cursor(stores):
    store, bob = stores
    assert store.get_preferences() == {"enabled": False}
    cursor = {"uidvalidity": "123", "last_uid": 4, "mailbox_hash": "a" * 64}
    store.set_mail_cursor(cursor)
    store.save_preferences({"enabled": True, "commission_eur": 1, "spread_bps": 5})
    assert store.get_mail_cursor() == cursor
    assert store.get_preferences()["enabled"] is True
    assert bob.get_preferences() == {"enabled": False}
    assert bob.get_mail_cursor() == {}
    for bad in ({"enabled": "yes"}, {"enabled": True, "recipient": "bad"}, {"spread_bps": -1}, {"spread_bps": True}):
        with pytest.raises(ValueError):
            store.save_preferences(bad)
    for bad in ({"last_uid": -1}, {"mailbox_hash": "raw-inbox"}, {"uidvalidity": True}):
        with pytest.raises(ValueError):
            store.set_mail_cursor(bad)


def test_simulation_runs_are_immutable_and_owner_scoped(stores):
    store, bob = stores
    assert store.get_simulation_state() is None
    first = {"observed_at": "2026-11-12T10:00:00Z", "equity_eur": 100, "transactions": []}
    assert store.save_simulation_run("run-1", first)
    assert not store.save_simulation_run("run-1", first)
    with pytest.raises(ExternalAnalystConflict):
        store.save_simulation_run("run-1", {**first, "equity_eur": 200})
    assert store.get_simulation_state() == first
    assert bob.list_simulation_runs() == []


class RemoteJournal:
    owner, url = "synthetic-alice", "https://synthetic.invalid"

    def __init__(self, response):
        self.response, self.requests = response, []

    def _request(self, method, **kwargs):
        self.requests.append((method, kwargs))
        return SimpleNamespace(json=lambda: self.response)


def test_remote_uses_owner_scoped_atomic_rpc_and_fails_closed():
    journal = RemoteJournal(True)
    store = ExternalAnalystStore(journal)
    assert store.claim_delivery("delivery-1", {"calls": 1})
    method, kwargs = journal.requests[-1]
    assert method == "POST"
    assert kwargs["endpoint"].endswith("/rest/v1/rpc/external_analyst_claim_delivery")
    assert kwargs["json"]["p_owner"] == "synthetic-alice"
    journal.response = "false"
    with pytest.raises(ValueError):
        store.claim_delivery("delivery-2", {})
    journal.response = [{"owner": "synthetic-bob", "call_json": {}}]
    with pytest.raises(ValueError, match="usuario"):
        store.list_calls()
    journal.response = [None]
    with pytest.raises(ValueError):
        store.list_calls()


@pytest.mark.parametrize("response", [None, False, {}, {"inserted": True, "revised": 0, "duplicates": 0, "message_duplicate": False},
    {"inserted": 0, "revised": 0, "duplicates": 0, "message_duplicate": False},
    {"inserted": 1, "revised": 0, "duplicates": 0, "message_duplicate": True}])
def test_remote_import_requires_an_explicit_consistent_confirmation(response):
    store = ExternalAnalystStore(RemoteJournal(response))
    with pytest.raises(ValueError, match="confirmó"):
        store.import_message(message())


def test_remote_import_confirmation_accepts_an_owner_scoped_atomic_result():
    result = {"inserted": 1, "revised": 0, "duplicates": 0, "message_duplicate": False}
    journal = RemoteJournal(result)
    assert ExternalAnalystStore(journal).import_message(message()) == result
    assert len(journal.requests) == 1
    assert journal.requests[0][1]["json"]["p_owner"] == "synthetic-alice"


@pytest.mark.parametrize("owner", [None, "", "anon", "grupo_compartido", "alice&owner=eq.bob"])
def test_rejects_shared_or_injected_owners(tmp_path, owner):
    with pytest.raises(ValueError):
        ExternalAnalystStore(SimpleNamespace(owner=owner, path=tmp_path / "never.db"))
