"""Synthetic local/REST cache tests: no provider, hosted DB, mail or engine."""
import base64
from copy import deepcopy
import gzip
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.market_cache import (
    MAX_METADATA_BYTES, MAX_PAYLOAD_BYTES, MAX_UNCOMPRESSED_BYTES,
    MarketCacheError, MarketCacheStore, decode_prices, encode_prices,
)


NOW = "2026-10-30T22:00:00Z"


def prices(*, timezone=None):
    frame = pd.DataFrame({"open": [100., 105.], "high": [110., 112.],
        "low": [95., 101.], "close": [105., 108.], "volume": [1000, 2000]},
        index=pd.date_range("2026-10-29", periods=2, name="Date", tz=timezone))
    frame.attrs = {"ticker": "TEST.L", "provider": "Synthetic",
        "quote_currency": "GBp", "source_currency": "GBp",
        "display_currency": "GBP", "price_scale": .01,
        "quote_units_normalized": True, "nested": {"items": [1, "é"]}}
    return frame


def local(tmp_path, owner="alice"):
    return MarketCacheStore(SimpleNamespace(owner=owner, database_path=tmp_path / "cache.sqlite"))


def row(owner="alice", kind="fundamentals", symbol="TEST", **kwargs):
    return {"owner": owner, "kind": kind, "symbol": symbol, "observed_at": NOW,
            "metadata": {"source": "Synthetic"}, "payload": {"value": 42}, **kwargs}


class Response:
    def __init__(self, value, content_range=""):
        self.value = value
        self.headers = {"Content-Range": content_range} if content_range else {}

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return deepcopy(self.value)


class Remote:
    def __init__(self, responses, owner="alice"):
        self.owner, self.url = owner, "https://synthetic.invalid"
        self.headers = {"apikey": "synthetic-test-only"}
        self.responses, self.calls = list(responses), []

    def _request(self, method, **kwargs):
        self.calls.append((method, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("timezone", [None, "UTC", "Europe/London"])
def test_price_roundtrip_preserves_data_dates_dtypes_attrs_units(timezone):
    frame = prices(timezone=timezone)
    encoded = encode_prices(frame)
    assert encode_prices(frame) == encoded
    decoded = decode_prices(encoded)
    pd.testing.assert_frame_equal(decoded, frame)
    assert decoded.attrs == frame.attrs
    decoded.attrs["nested"]["items"].append("changed")
    assert frame.attrs["nested"]["items"] == [1, "é"]


def test_roundtrip_does_not_normalize_subunits_a_second_time():
    frame = prices()
    assert decode_prices(encode_prices(frame))["close"].tolist() == [105., 108.]


def test_mixed_iso_precision_roundtrip_without_losing_nanoseconds():
    frame = prices()
    frame.index = pd.DatetimeIndex([pd.Timestamp("2026-10-29"), pd.Timestamp("2026-10-30T00:00:00.000000001")])
    pd.testing.assert_frame_equal(decode_prices(encode_prices(frame)), frame)


def test_encoding_does_not_persist_computed_engine_columns():
    frame = prices()
    frame["score"] = 99
    assert list(decode_prices(encode_prices(frame))) == ["open", "high", "low", "close", "volume"]


@pytest.mark.parametrize("change", [
    lambda f: f.assign(close=[0., 108.]),
    lambda f: f.assign(volume=[-1, 2]),
    lambda f: f.assign(open=[np.nan, 105.]),
    lambda f: f.assign(close=[np.inf, 108.]),
    lambda f: f.assign(high=[99., 112.]),
    lambda f: f.assign(low=[106., 101.]),
    lambda f: f.assign(volume=[True, False]),
    lambda f: f.iloc[::-1],
    lambda f: f.set_axis([f.index[0], f.index[0]]),
    lambda f: f.set_axis([pd.NaT, f.index[1]]),
    lambda f: f.set_axis(["2026-10-29", "2026-10-30"]),
    lambda f: f.drop(columns="close"),
    lambda f: f.iloc[:0],
])
def test_reject_invalid_prices_before_encoding(change):
    with pytest.raises(MarketCacheError):
        encode_prices(change(prices()))


def repack(encoded, mutate):
    result = deepcopy(encoded)
    doc = json.loads(gzip.decompress(base64.b64decode(result["data"])))
    mutate(doc)
    raw = json.dumps(doc).encode()
    result.update(data=base64.b64encode(gzip.compress(raw)).decode(),
                  sha256=sha256(raw).hexdigest(), uncompressed_bytes=len(raw))
    return result


@pytest.mark.parametrize("mutate", [
    lambda doc: doc["index"].__setitem__(1, doc["index"][0]),
    lambda doc: doc["data"][0].__setitem__(3, 0),
    lambda doc: doc["dtypes"].__setitem__("close", "object"),
    lambda doc: doc["data"][0].__setitem__(3, True),
    lambda doc: doc.__setitem__("columns", ["close"]),
    lambda doc: doc.__setitem__("attrs", "not-an-object"),
    lambda doc: doc.__setitem__("version", 2),
])
def test_reject_authenticated_but_invalid_price_document(mutate):
    with pytest.raises(MarketCacheError):
        decode_prices(repack(encode_prices(prices()), mutate))


@pytest.mark.parametrize("update", [
    {"encoding": "pickle"}, {"data": "bad+base64!!"}, {"sha256": "0" * 64},
    {"uncompressed_bytes": 1}, {"uncompressed_bytes": MAX_UNCOMPRESSED_BYTES + 1},
])
def test_reject_corrupt_encoded_prices(update):
    payload = encode_prices(prices())
    payload.update(update)
    with pytest.raises(MarketCacheError):
        decode_prices(payload)


def test_reject_concatenated_or_truncated_gzip():
    payload = encode_prices(prices())
    for content in (base64.b64decode(payload["data"])[:-5],
                    base64.b64decode(payload["data"]) + gzip.compress(b"ignored")):
        changed = {**payload, "data": base64.b64encode(content).decode()}
        with pytest.raises(MarketCacheError):
            decode_prices(changed)


def test_bounded_decompression_rejects_zip_bomb(monkeypatch):
    import src.market_cache as module
    raw = b"x" * 3000
    payload = {"encoding": module.PRICE_ENCODING,
        "data": base64.b64encode(gzip.compress(raw)).decode(),
        "uncompressed_bytes": 1000, "sha256": sha256(raw).hexdigest()}
    monkeypatch.setattr(module, "MAX_UNCOMPRESSED_BYTES", 1024)
    with pytest.raises(MarketCacheError):
        decode_prices(payload)


def test_reads_do_not_create_database_or_table(tmp_path):
    store = local(tmp_path)
    assert store.heads() == [] and store.read("prices", "TEST") is None
    assert not store.path.exists()
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE untouched (id INTEGER)")
    assert store.heads() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("untouched",)]


def test_sqlite_owner_isolation_metadata_heads_and_full_read(tmp_path):
    first, second = local(tmp_path), local(tmp_path, "bob")
    assert first.put("fundamentals", "TEST", {"value": 1}, {"source": "A"}, observed_at=NOW)
    assert second.put("fundamentals", "TEST", {"value": 2}, {"source": "B"}, observed_at=NOW)
    head = first.heads(["TEST"], "fundamentals")[0]
    assert set(head) == {"kind", "symbol", "observed_at", "metadata"}
    assert head["metadata"] == {"source": "A"}
    assert first.read("fundamentals", "TEST")["payload"] == {"value": 1}
    assert second.read("fundamentals", "TEST")["payload"] == {"value": 2}
    assert first.heads([]) == []
    assert first.heads(["OTHER"]) == []
    assert first.heads(kind="fx") == []


def test_sqlite_reject_late_write_at_nanosecond_precision_and_preserve_original(tmp_path):
    store = local(tmp_path)
    later = "2026-10-30T22:00:00.000000002Z"
    assert store.put("fx", "EUR", {"rate": 1}, {}, observed_at=later)
    assert not store.put("fx", "EUR", {"rate": 2}, {}, observed_at="2026-10-30T22:00:00.000000001Z")
    assert store.read("fx", "EUR")["payload"] == {"rate": 1}
    assert store.put("fx", "EUR", {"rate": 3}, {}, observed_at=later)


def test_prices_store_roundtrip(tmp_path):
    store = local(tmp_path)
    assert store.put("prices", "TEST.L", encode_prices(prices()),
        {"auto_adjust": True, "currency": "GBP"}, observed_at=NOW)
    pd.testing.assert_frame_equal(decode_prices(store.read("prices", "TEST.L")["payload"]), prices())


def test_failed_sqlite_write_rolls_back_without_replacing_old_value(tmp_path):
    store = local(tmp_path)
    store.put("fx", "EUR", {"rate": 1}, {}, observed_at=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON market_data_cache BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(MarketCacheError):
        store.put("fx", "EUR", {"rate": 2}, {}, observed_at="2026-10-30T23:00:00Z")
    assert store.read("fx", "EUR")["payload"] == {"rate": 1}


def test_heads_never_parse_or_return_payload(tmp_path):
    store = local(tmp_path)
    store.put("fx", "EUR", {}, {}, observed_at=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE market_data_cache SET payload='corrupt json'")
    assert len(store.heads()) == 1
    with pytest.raises(MarketCacheError):
        store.read("fx", "EUR")


def test_remote_heads_explicitly_project_no_payload_and_filter_owner():
    backend = Remote([Response([row()], "0-0/1")])
    heads = MarketCacheStore(backend).heads(["TEST"], "fundamentals")
    method, kwargs = backend.calls[0]
    assert method == "GET"
    assert kwargs["endpoint"] == "https://synthetic.invalid/rest/v1/market_data_cache"
    assert "payload" not in kwargs["params"]["select"]
    assert kwargs["params"]["owner"] == "eq.alice"
    assert kwargs["params"]["kind"] == "eq.fundamentals"
    assert kwargs["params"]["symbol"] == 'in.("TEST")'
    assert "payload" not in heads[0]


def test_remote_read_and_boolean_rpc_confirmed_without_retry():
    backend = Remote([Response([row()]), Response(True), Response(False)])
    store = MarketCacheStore(backend)
    assert store.read("fundamentals", "TEST")["payload"] == {"value": 42}
    assert store.put("fx", "EUR", {"rate": 1}, {}, observed_at=NOW)
    assert not store.put("fx", "EUR", {"rate": 0}, {}, observed_at=NOW)
    method, kwargs = backend.calls[1]
    assert method == "POST" and kwargs["endpoint"].endswith("/rpc/upsert_market_data_cache")
    assert kwargs["json"]["p_owner"] == "alice"
    assert set(kwargs["json"]) == {"p_owner", "p_kind", "p_symbol", "p_observed_at", "p_metadata", "p_payload"}
    assert len(backend.calls) == 3


def test_read_many_projects_full_payload_only_for_requested_keys_in_batches_of_50():
    symbols = [f"T{i:03d}" for i in range(120)]
    responses = [Response([row(symbol=symbol) for symbol in symbols[offset:offset + 50]])
                 for offset in range(0, 120, 50)]
    backend = Remote(responses)
    result = MarketCacheStore(backend).read_many("fundamentals", symbols)
    assert len(result) == 120 and len(backend.calls) == 3
    assert [item["symbol"] for item in result] == symbols
    assert all(item["payload"] == {"value": 42} for item in result)
    for offset, (method, kwargs) in enumerate(backend.calls):
        batch = symbols[offset * 50:(offset + 1) * 50]
        assert method == "GET" and "payload" in kwargs["params"]["select"]
        assert kwargs["params"]["owner"] == "eq.alice"
        assert kwargs["params"]["kind"] == "eq.fundamentals"
        assert kwargs["params"]["symbol"] == "in.(" + ",".join('"' + symbol + '"' for symbol in batch) + ")"
        assert int(kwargs["params"]["limit"]) <= 51


def test_read_many_real_missing_keys_omitted_and_empty_request_has_no_io():
    backend = Remote([Response([row()], "0-0/1")])
    store = MarketCacheStore(backend)
    assert store.read_many("fundamentals", []) == [] and backend.calls == []
    assert [item["symbol"] for item in store.read_many("fundamentals", ["MISSING", "TEST", "TEST"])] == ["TEST"]
    assert len(backend.calls) == 1


@pytest.mark.parametrize("failure", [RuntimeError("second batch failed"),
    Response([row(symbol="T050")], "0-0/10"), Response([row(owner="bob", symbol="T050")]),
    Response([row(symbol="T000")]), Response([row(symbol="T050"), row(symbol="T050")]),
    Response([row(kind="fx", symbol="T050")])])
def test_read_many_second_batch_failure_never_returns_first_batch_results(failure):
    symbols = [f"T{i:03d}" for i in range(60)]
    backend = Remote([Response([row(symbol=symbol) for symbol in symbols[:50]]), failure])
    with pytest.raises(MarketCacheError):
        MarketCacheStore(backend).read_many("fundamentals", symbols)
    assert len(backend.calls) == 2


@pytest.mark.parametrize("invalid", [[f"T{i}" for i in range(201)], "TEST", None, ["TEST&owner=eq.bob"]])
def test_read_many_bounded_before_any_io(invalid):
    backend = Remote([])
    with pytest.raises(MarketCacheError):
        MarketCacheStore(backend).read_many("fundamentals", invalid)
    assert backend.calls == []


def test_read_many_sqlite_isolation_and_no_table_on_read(tmp_path):
    store, other = local(tmp_path), local(tmp_path, "bob")
    assert store.read_many("fx", ["EUR", "USD"]) == []
    assert not store.path.exists()
    store.put("fx", "EUR", {"rate": 1}, {}, observed_at=NOW)
    other.put("fx", "EUR", {"rate": 2}, {}, observed_at=NOW)
    assert store.read_many("fx", ["EUR", "USD"])[0]["payload"] == {"rate": 1}
    assert other.read_many("fx", ["EUR", "USD"])[0]["payload"] == {"rate": 2}


@pytest.mark.parametrize("rows", [[row(), row()], [row(symbol="OTHER")], [row(kind="fx")]])
def test_remote_heads_reject_duplicate_or_ignored_filters(rows):
    with pytest.raises(MarketCacheError):
        MarketCacheStore(Remote([Response(rows)])).heads(["TEST"], "fundamentals")


@pytest.mark.parametrize("response", [
    Response([row(owner="bob")]), Response([row(), row()]),
    Response([row(symbol="OTHER")]), Response([row()], "0-0/2"),
    Response({"error": "failure"}), Response([row(metadata={"bad": float("inf")})]),
    Response([row(observed_at="not-a-date")]), Response(RuntimeError("invalid JSON")),
    RuntimeError("request failed"),
])
def test_remote_read_rejects_partial_wrong_owner_malformed_or_failed_response(response):
    backend = Remote([response])
    with pytest.raises(MarketCacheError):
        MarketCacheStore(backend).read("fundamentals", "TEST")
    assert len(backend.calls) == 1


@pytest.mark.parametrize("response", [Response(None), Response({"ok": True}), RuntimeError("ambiguous write")])
def test_remote_write_requires_unambiguous_boolean_and_never_retries(response):
    backend = Remote([response])
    with pytest.raises(MarketCacheError):
        MarketCacheStore(backend).put("fx", "EUR", {}, {}, observed_at=NOW)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("invalid", ["", "alice\x00", "alice\n", "alice&owner=eq.bob", 'alice"', "alice,or(foo)"])
def test_owner_control_and_query_injection_rejected(tmp_path, invalid):
    with pytest.raises(MarketCacheError):
        local(tmp_path, invalid)


@pytest.mark.parametrize("symbol", ["", "TEST\x00", "TEST\r\n", "TEST&owner=eq.bob", 'TEST"', "TEST),or(owner.eq.bob)"])
def test_symbol_control_and_query_injection_rejected(tmp_path, symbol):
    store = local(tmp_path)
    with pytest.raises(MarketCacheError):
        store.read("prices", symbol)


def test_mutated_journal_owner_is_rejected(tmp_path):
    store = local(tmp_path)
    store.journal.owner = "bob"
    for call in (lambda: store.heads(), lambda: store.read("fx", "EUR"),
                 lambda: store.put("fx", "EUR", {}, {}, observed_at=NOW)):
        with pytest.raises(MarketCacheError):
            call()


@pytest.mark.parametrize("timestamp", ["2026-10-30", "2026-10-30T22:00:00", None, "NaT"])
def test_observed_at_must_be_aware(tmp_path, timestamp):
    with pytest.raises(MarketCacheError):
        local(tmp_path).put("fx", "EUR", {}, {}, observed_at=timestamp)
    assert not (tmp_path / "cache.sqlite").exists()


@pytest.mark.parametrize("payload,metadata", [
    ({"value": float("nan")}, {}), ({"value": float("inf")}, {}),
    ({"object": object()}, {}), ({"control": "\x00"}, {}),
    ({"big": "x" * MAX_PAYLOAD_BYTES}, {}), ({}, {"big": "x" * MAX_METADATA_BYTES}),
    ([], {}), ({}, []),
])
def test_invalid_or_oversize_json_does_not_write(tmp_path, payload, metadata):
    with pytest.raises(MarketCacheError):
        local(tmp_path).put("fundamentals", "TEST", payload, metadata, observed_at=NOW)
    assert not (tmp_path / "cache.sqlite").exists()


def test_symbol_and_response_count_bounds(tmp_path):
    with pytest.raises(MarketCacheError):
        local(tmp_path).heads([f"TEST{i}" for i in range(501)])
    backend = Remote([Response([row(symbol=f"TEST{i}") for i in range(501)])])
    with pytest.raises(MarketCacheError):
        MarketCacheStore(backend).heads()


def test_local_corrupt_database_is_not_reported_as_empty(tmp_path):
    path = tmp_path / "cache.sqlite"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(MarketCacheError):
        local(tmp_path).heads()
