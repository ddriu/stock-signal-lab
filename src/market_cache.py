"""Owner-scoped transport cache; never a source of trading decisions.

Reads do not initialise a database, fetch market data, or touch saved analyses.
Price payloads contain JSON only (no pickle), with bounded gzip decompression.
"""
from __future__ import annotations

import base64
import binascii
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any
import zlib

import numpy as np
import pandas as pd

from src.data_loader import REQUIRED_COLUMNS


TABLE = "market_data_cache"
PRICE_ENCODING = "ohlcv-json-gzip-base64-v1"
MAX_ROWS = 500
MAX_READ_MANY_SYMBOLS = 200
READ_BATCH_SIZE = 50
MAX_PAYLOAD_BYTES = 512 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
KINDS = frozenset({"prices", "fundamentals", "fx"})
_OWNER = re.compile(r"[A-Za-z0-9_@.+:\-]{1,128}\Z")
_SYMBOL = re.compile(r"[A-Za-z0-9_^.=:/\-]{1,64}\Z")


class MarketCacheError(ValueError):
    """Invalid/incomplete cache data or an unsuccessful storage operation."""


def _identifier(value: Any, *, owner: bool = False) -> str:
    if not isinstance(value, str) or not (_OWNER if owner else _SYMBOL).fullmatch(value):
        raise MarketCacheError("Invalid cache owner or symbol.")
    return value


def _kind(value: Any) -> str:
    if not isinstance(value, str) or value not in KINDS:
        raise MarketCacheError("Invalid market-cache kind.")
    return value


def _timestamp(value: Any) -> str:
    try:
        if isinstance(value, str) and (len(value) > 128 or any(ord(c) < 32 for c in value)):
            raise ValueError("Invalid timestamp text")
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("An aware timestamp is required")
        return stamp.tz_convert("UTC").isoformat(timespec="nanoseconds")
    except (ValueError, TypeError, OverflowError) as exc:
        raise MarketCacheError("Cache observed_at must be a valid timezone-aware timestamp.") from exc


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise MarketCacheError("Cache JSON is nested too deeply.")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise MarketCacheError("Non-finite cache JSON number.")
        return
    if isinstance(value, str):
        if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise MarketCacheError("Control character in cache JSON.")
        return
    if isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _check_json(key, depth + 1)
            _check_json(item, depth + 1)
        return
    raise MarketCacheError("Cache values must be ordinary JSON values.")


def _json_bytes(value: Any, *, limit: int) -> bytes:
    _check_json(value)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False,
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MarketCacheError("Invalid cache JSON.") from exc
    if len(encoded) > limit:
        raise MarketCacheError("Cache JSON exceeds its size limit.")
    return encoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MarketCacheError("Duplicate JSON key in cache payload.")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise MarketCacheError("Non-finite cache JSON number.")


def _load_json(value: bytes | str) -> Any:
    try:
        result = json.loads(value, object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
        _check_json(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise MarketCacheError("Invalid cache JSON.") from exc


def _validate_prices(frame: pd.DataFrame) -> None:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise MarketCacheError("Price history must be a non-empty DataFrame.")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.hasnans:
        raise MarketCacheError("Price history requires valid datetime observations.")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise MarketCacheError("Price dates must be unique and strictly increasing.")
    if not frame.columns.is_unique or not set(REQUIRED_COLUMNS).issubset(frame.columns):
        raise MarketCacheError("Missing or duplicate OHLCV columns.")
    for column in REQUIRED_COLUMNS:
        if not pd.api.types.is_numeric_dtype(frame[column]) or pd.api.types.is_bool_dtype(frame[column]):
            raise MarketCacheError("OHLCV values must be numeric.")
    try:
        data = frame.loc[:, list(REQUIRED_COLUMNS)].to_numpy(dtype=float)
    except (ValueError, TypeError, OverflowError) as exc:
        raise MarketCacheError("Invalid OHLCV values.") from exc
    if not np.isfinite(data).all() or (data[:, :4] <= 0).any() or (data[:, 4] < 0).any():
        raise MarketCacheError("Prices must be positive and volume non-negative and finite.")
    opened, high, low, close = (data[:, i] for i in range(4))
    if (high < np.maximum(opened, close)).any() or (low > np.minimum(opened, close)).any() or (high < low).any():
        raise MarketCacheError("Inconsistent OHLC price range.")


def encode_prices(frame: pd.DataFrame) -> dict:
    """Encode raw OHLCV and JSON attrs, preserving units and datetime timezone."""
    _validate_prices(frame)
    columns = list(REQUIRED_COLUMNS)
    selected = frame.loc[:, columns]
    # No indicators/score columns are persisted as if they were source quotes.
    document = {
        "version": 1, "columns": columns,
        "index": [stamp.isoformat() for stamp in frame.index],
        "index_name": frame.index.name,
        "timezone": str(frame.index.tz) if frame.index.tz is not None else None,
        "frequency": frame.index.freqstr,
        "dtypes": {column: str(selected[column].dtype) for column in columns},
        "data": [[value.item() if isinstance(value, np.generic) else value
                  for value in row] for row in selected.itertuples(index=False, name=None)],
        "attrs": dict(frame.attrs),
    }
    raw = _json_bytes(document, limit=MAX_UNCOMPRESSED_BYTES)
    result = {
        "encoding": PRICE_ENCODING,
        "data": base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii"),
        "sha256": sha256(raw).hexdigest(), "uncompressed_bytes": len(raw),
    }
    _json_bytes(result, limit=MAX_PAYLOAD_BYTES)
    return result


def decode_prices(payload: dict) -> pd.DataFrame:
    """Decode a bounded, integrity-checked price payload; reject malformed data."""
    if not isinstance(payload, dict):
        raise MarketCacheError("Invalid encoded price payload.")
    _json_bytes(payload, limit=MAX_PAYLOAD_BYTES)
    if payload.get("encoding") != PRICE_ENCODING:
        raise MarketCacheError("Unknown price-cache encoding.")
    size = payload.get("uncompressed_bytes")
    if type(size) is not int or not 0 < size <= MAX_UNCOMPRESSED_BYTES:
        raise MarketCacheError("Invalid uncompressed price size.")
    try:
        compressed = base64.b64decode(payload["data"], validate=True)
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(compressed, MAX_UNCOMPRESSED_BYTES + 1)
        if len(raw) > MAX_UNCOMPRESSED_BYTES or decoder.unconsumed_tail:
            raise MarketCacheError("Price payload exceeds decompression limit.")
        raw += decoder.flush(MAX_UNCOMPRESSED_BYTES + 1 - len(raw))
        if not decoder.eof or decoder.unused_data or len(raw) != size:
            raise MarketCacheError("Truncated/concatenated price payload or invalid size.")
    except (KeyError, ValueError, TypeError, binascii.Error, zlib.error) as exc:
        raise MarketCacheError("Corrupt compressed price payload.") from exc
    if sha256(raw).hexdigest() != payload.get("sha256"):
        raise MarketCacheError("Price payload integrity check failed.")
    doc = _load_json(raw)
    try:
        if not isinstance(doc, dict) or doc.get("version") != 1 or doc.get("columns") != list(REQUIRED_COLUMNS):
            raise ValueError("Unknown price document")
        if not isinstance(doc["attrs"], dict) or not isinstance(doc["index"], list) or not isinstance(doc["data"], list):
            raise ValueError("Invalid price document")
        if len(doc["index"]) != len(doc["data"]):
            raise ValueError("Mismatched price dates")
        if not all(isinstance(stamp, str) for stamp in doc["index"]):
            raise ValueError("Invalid price dates")
        if not all(isinstance(row, list) and len(row) == len(REQUIRED_COLUMNS)
                   and all(type(cell) in (int, float) for cell in row) for row in doc["data"]):
            raise ValueError("Invalid numeric price observations")
        timezone = doc["timezone"]
        index = pd.DatetimeIndex(pd.to_datetime(doc["index"], utc=timezone is not None, format="ISO8601"),
                                 name=doc["index_name"])
        if timezone is not None:
            index = index.tz_convert(timezone)
        elif index.tz is not None:
            raise ValueError("Undeclared timezone")
        if doc.get("frequency") is not None:
            index = pd.DatetimeIndex(index, freq=doc["frequency"])
        frame = pd.DataFrame(doc["data"], columns=list(REQUIRED_COLUMNS), index=index)
        _validate_prices(frame)
        dtypes = doc["dtypes"]
        if not isinstance(dtypes, dict) or set(dtypes) != set(REQUIRED_COLUMNS):
            raise ValueError("Invalid price dtypes")
        for column, dtype in dtypes.items():
            if not isinstance(dtype, str) or not re.fullmatch(r"(?:[uU]?[iI]nt(?:8|16|32|64)|[fF]loat(?:32|64))", dtype):
                raise ValueError("Invalid numeric dtype")
            cast = frame[column].astype(dtype)
            if not np.array_equal(cast.to_numpy(dtype=float), frame[column].to_numpy(dtype=float)):
                raise ValueError("Lossy dtype conversion")
            frame[column] = cast
        frame.attrs = doc["attrs"]
        _validate_prices(frame)
        return frame
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise MarketCacheError("Invalid OHLCV document.") from exc


class MarketCacheStore:
    """One owner only, using the journal's configured SQLite or REST backend."""

    def __init__(self, journal: object):
        self.journal = journal
        self.owner = _identifier(getattr(journal, "owner", None), owner=True)
        self.remote = callable(getattr(journal, "_request", None)) and bool(getattr(journal, "url", ""))
        if self.remote:
            url = str(journal.url).rstrip("/")
            if not url.startswith("https://") or any(c in url for c in "?#\r\n"):
                raise MarketCacheError("Invalid cache backend URL.")
            self.endpoint = f"{url}/rest/v1/{TABLE}"
            self.rpc_endpoint = f"{url}/rest/v1/rpc/upsert_market_data_cache"
        else:
            path = getattr(journal, "database_path", None)
            if path is None:
                raise MarketCacheError("Unsupported market-cache backend.")
            self.path = Path(path)

    def _guard(self) -> None:
        if getattr(self.journal, "owner", None) != self.owner:
            raise MarketCacheError("Cache journal owner changed.")

    def _row(self, row: Any, *, full: bool) -> dict:
        if not isinstance(row, dict) or row.get("owner") != self.owner:
            raise MarketCacheError("Cache response crossed its owner boundary.")
        result = {"kind": _kind(row.get("kind")), "symbol": _identifier(row.get("symbol")),
                  "observed_at": _timestamp(row.get("observed_at")), "metadata": row.get("metadata")}
        if not isinstance(result["metadata"], dict):
            raise MarketCacheError("Invalid cache metadata.")
        _json_bytes(result["metadata"], limit=MAX_METADATA_BYTES)
        if full:
            result["owner"] = self.owner
            result["payload"] = row.get("payload")
            if not isinstance(result["payload"], dict):
                raise MarketCacheError("Invalid cache payload.")
            _json_bytes(result["payload"], limit=MAX_PAYLOAD_BYTES)
        return result

    def _get_remote(self, params: dict[str, str], *, full: bool) -> list[dict]:
        try:
            headers = dict(getattr(self.journal, "headers", {}))
            response = self.journal._request("GET", endpoint=self.endpoint,
                params={"owner": f"eq.{self.owner}", **params},
                headers={**headers, "Prefer": "count=exact"})
            rows = response.json()
            if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                raise MarketCacheError("Invalid or oversized cache response.")
            total = str(getattr(response, "headers", {}).get("Content-Range", "")).rsplit("/", 1)[-1]
            if total.isdigit() and int(total) != len(rows):
                raise MarketCacheError("Incomplete cache response.")
            return [self._row(row, full=full) for row in rows]
        except MarketCacheError:
            raise
        except Exception as exc:
            raise MarketCacheError("Could not read market cache.") from exc

    def _get_local(self, where: str, args: list[str], *, full: bool) -> list[dict]:
        if not self.path.exists():
            return []
        columns = "owner, kind, symbol, observed_at, metadata" + (", payload" if full else "")
        try:
            with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(f"SELECT {columns} FROM {TABLE} WHERE owner=?{where} ORDER BY kind,symbol LIMIT ?",
                                          [self.owner, *args, MAX_ROWS + 1]).fetchall()
            if len(rows) > MAX_ROWS:
                raise MarketCacheError("Oversized cache response.")
            result = []
            for record in rows:
                row = dict(record)
                for field, limit in (("metadata", MAX_METADATA_BYTES), ("payload", MAX_PAYLOAD_BYTES)):
                    if field in row and (not isinstance(row[field], str) or len(row[field].encode("utf-8")) > limit):
                        raise MarketCacheError("Invalid or oversized stored cache JSON.")
                row["metadata"] = _load_json(row["metadata"])
                if full:
                    row["payload"] = _load_json(row["payload"])
                result.append(self._row(row, full=full))
            return result
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return []
            raise MarketCacheError("Could not read market cache.") from exc
        except sqlite3.Error as exc:
            raise MarketCacheError("Could not read market cache.") from exc

    def heads(self, symbols=None, kind=None) -> list[dict]:
        """Read only metadata; never SELECT or decompress history payloads."""
        self._guard()
        if kind is not None:
            _kind(kind)
        wanted = None
        if symbols is not None:
            if isinstance(symbols, (str, bytes)):
                raise MarketCacheError("symbols must be a bounded collection.")
            wanted = []
            for symbol in symbols:
                wanted.append(_identifier(symbol))
                if len(wanted) > MAX_ROWS:
                    raise MarketCacheError("Too many cache symbols.")
            wanted = sorted(set(wanted))
            if not wanted:
                return []
        if self.remote:
            params = {"select": "owner,kind,symbol,observed_at,metadata", "order": "kind.asc,symbol.asc", "limit": str(MAX_ROWS + 1)}
            if kind is not None:
                params["kind"] = f"eq.{kind}"
            if wanted is not None:
                params["symbol"] = "in.(" + ",".join('"' + symbol + '"' for symbol in wanted) + ")"
            rows = self._get_remote(params, full=False)
        else:
            where, args = "", []
            if kind is not None:
                where += " AND kind=?"
                args.append(kind)
            if wanted is not None:
                where += " AND symbol IN (" + ",".join("?" for _ in wanted) + ")"
                args.extend(wanted)
            rows = self._get_local(where, args, full=False)
        if any((kind is not None and row["kind"] != kind) or (wanted is not None and row["symbol"] not in wanted) for row in rows):
            raise MarketCacheError("Cache response ignored its filters.")
        identities = [(row["kind"], row["symbol"]) for row in rows]
        if len(set(identities)) != len(identities):
            raise MarketCacheError("Duplicate cache heads.")
        return rows

    def read(self, kind, symbol) -> dict | None:
        self._guard()
        _kind(kind)
        _identifier(symbol)
        if self.remote:
            rows = self._get_remote({"select": "owner,kind,symbol,observed_at,metadata,payload",
                "kind": f"eq.{kind}", "symbol": f"eq.{symbol}", "limit": "2"}, full=True)
        else:
            rows = self._get_local(" AND kind=? AND symbol=?", [kind, symbol], full=True)
        if len(rows) > 1 or any(row["kind"] != kind or row["symbol"] != symbol for row in rows):
            raise MarketCacheError("Duplicate or mismatched cache result.")
        return rows[0] if rows else None

    def read_many(self, kind, symbols) -> list[dict]:
        """Fetch at most 200 requested keys, in batches of at most 50.

        Absent cache keys are omitted, like ``read`` returning None. A failed,
        truncated or invalid batch aborts the whole call, with no partial return.
        Every returned row carries its own observation time, not a fictitious
        atomic multi-symbol market timestamp.
        """
        self._guard()
        _kind(kind)
        if isinstance(symbols, (str, bytes)):
            raise MarketCacheError("symbols must be a bounded collection.")
        try:
            iterator = iter(symbols)
        except TypeError as exc:
            raise MarketCacheError("symbols must be a bounded collection.") from exc
        wanted = []
        for symbol in iterator:
            wanted.append(_identifier(symbol))
            if len(wanted) > MAX_READ_MANY_SYMBOLS:
                raise MarketCacheError("Too many cache payload symbols.")
        wanted = sorted(set(wanted))
        result = []
        for offset in range(0, len(wanted), READ_BATCH_SIZE):
            batch = wanted[offset:offset + READ_BATCH_SIZE]
            if self.remote:
                rows = self._get_remote({
                    "select": "owner,kind,symbol,observed_at,metadata,payload",
                    "kind": f"eq.{kind}",
                    "symbol": "in.(" + ",".join('"' + symbol + '"' for symbol in batch) + ")",
                    "order": "symbol.asc", "limit": str(len(batch) + 1),
                }, full=True)
            else:
                where = " AND kind=? AND symbol IN (" + ",".join("?" for _ in batch) + ")"
                rows = self._get_local(where, [kind, *batch], full=True)
            symbols_returned = [row["symbol"] for row in rows]
            if (len(rows) > len(batch) or len(set(symbols_returned)) != len(rows)
                or any(row["kind"] != kind or row["symbol"] not in batch for row in rows)):
                raise MarketCacheError("Duplicate or unrequested batch cache result.")
            result.extend(rows)
        return sorted(result, key=lambda row: row["symbol"])

    def put(self, kind, symbol, payload, metadata, *, observed_at) -> bool:
        """Upsert one complete observation; an older writer cannot replace it.

        The remote RPC must return a JSON boolean: true accepted, false stale.
        Exceptions, including ambiguous writes, are not retried here.
        """
        self._guard()
        _kind(kind)
        _identifier(symbol)
        stamp = _timestamp(observed_at)
        if not isinstance(payload, dict) or not isinstance(metadata, dict):
            raise MarketCacheError("Cache payload and metadata must be JSON objects.")
        payload_json = _json_bytes(payload, limit=MAX_PAYLOAD_BYTES).decode("utf-8")
        metadata_json = _json_bytes(metadata, limit=MAX_METADATA_BYTES).decode("utf-8")
        if kind == "prices":
            decode_prices(payload)
        if self.remote:
            try:
                response = self.journal._request("POST", endpoint=self.rpc_endpoint, json={
                    "p_owner": self.owner, "p_kind": kind, "p_symbol": symbol,
                    "p_observed_at": stamp, "p_metadata": metadata, "p_payload": payload})
                result = response.json()
                if type(result) is not bool:
                    raise MarketCacheError("Market-cache upsert did not confirm its outcome.")
                return result
            except MarketCacheError:
                raise
            except Exception as exc:
                raise MarketCacheError("Could not confirm market-cache write.") from exc
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (owner TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL, metadata TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(owner,kind,symbol))")
                old = connection.execute(f"SELECT observed_at FROM {TABLE} WHERE owner=? AND kind=? AND symbol=?",
                                         (self.owner, kind, symbol)).fetchone()
                if old is not None and pd.Timestamp(stamp) < pd.Timestamp(_timestamp(old[0])):
                    return False
                connection.execute(f"INSERT INTO {TABLE} (owner,kind,symbol,observed_at,metadata,payload) VALUES (?,?,?,?,?,?) ON CONFLICT(owner,kind,symbol) DO UPDATE SET observed_at=excluded.observed_at,metadata=excluded.metadata,payload=excluded.payload",
                                   (self.owner, kind, symbol, stamp, metadata_json, payload_json))
            return True
        except (sqlite3.Error, OSError) as exc:
            raise MarketCacheError("Could not confirm market-cache write.") from exc
