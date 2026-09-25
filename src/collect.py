"""Bounded or continuous local collection of timestamped Winnipeg Transit API estimates."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

from .transit_api import ApiClient, AuthenticationError, LOCAL_ZONE, normalize, sanitize, utc_now


def validate_config(config):
    if config["route"] != "BLUE" or not config["seed_stops"]:
        raise ValueError("This collector requires BLUE and at least one seed stop.")
    if any(not re.fullmatch(r"\d+", str(stop)) for stop in config["seed_stops"]):
        raise ValueError("Seed stops must be numeric identifiers.")
    if len(set(config["seed_stops"])) != len(config["seed_stops"]):
        raise ValueError("Seed stops must be distinct.")
    if not 1 <= config["requests_per_minute"] <= 80:
        raise ValueError("Set a local budget between 1 and 80 requests per minute.")
    for name in ["poll_interval_seconds", "timeout_seconds", "lookahead_minutes", "trip_retention_minutes"]:
        if config[name] <= 0:
            raise ValueError(f"{name} must be positive.")
    for name in ["max_retries", "max_trip_requests_per_cycle", "lookback_minutes"]:
        if config[name] < 0:
            raise ValueError(f"{name} must not be negative.")


def read_api_key(root, variable):
    key = os.environ.get(variable, "").strip()
    if not key:
        path = root / ".env"
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == variable:
                    key = value.strip().strip("\"'")
                    break
    if not key or key in {"your_key", "YOUR_API_KEY", "replace_me"}:
        raise ValueError(f"Set {variable} in the environment or local .env before live collection.")
    return key


@contextmanager
def collector_lock(database):
    database.parent.mkdir(parents=True, exist_ok=True)
    with database.with_suffix(database.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A collector already holds this database's lock.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Store:
    def __init__(self, database):
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("Unsupported collector database schema.")
        self.connection.executescript("""
          CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, origin TEXT NOT NULL,
            resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
            request_started_at_utc TEXT NOT NULL, received_at_utc TEXT NOT NULL,
            endpoint TEXT NOT NULL, parameters_json TEXT NOT NULL, attempt_number INTEGER,
            http_status INTEGER, error_kind TEXT, server_date TEXT, retry_after_seconds REAL,
            payload_sha256 TEXT, payload_json TEXT, observation_count INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS observations (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id), row_number INTEGER NOT NULL,
            received_at_utc TEXT NOT NULL, scheduled_stop_key TEXT, trip_key TEXT, bus_key TEXT,
            stop_key TEXT, route_number TEXT, variant_key TEXT,
            scheduled_departure_utc TEXT, estimated_departure_utc TEXT,
            estimated_delay_seconds REAL, scheduled_lead_seconds REAL, cancelled INTEGER,
            measurement_kind TEXT NOT NULL CHECK(measurement_kind='api_estimate'), fields_json TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,row_number));
          CREATE INDEX IF NOT EXISTS observations_event ON observations(trip_key,scheduled_stop_key,received_at_utc);
          CREATE INDEX IF NOT EXISTS observations_stop_time ON observations(stop_key,scheduled_departure_utc);
          CREATE TABLE IF NOT EXISTS tracked_trips (
            trip_key TEXT PRIMARY KEY, expires_at_utc TEXT NOT NULL, last_attempt_at_utc TEXT);
          CREATE TABLE IF NOT EXISTS cycles (
            cycle_id TEXT PRIMARY KEY, started_at_utc TEXT NOT NULL, finished_at_utc TEXT,
            status TEXT NOT NULL, summary_json TEXT);
          PRAGMA user_version=1;
        """)

    def close(self):
        self.connection.close()

    def record(self, cycle_id, origin, resource_type, resource_id, attempt, api_key=""):
        safe = sanitize(attempt, api_key)
        rows = []
        if safe["http_status"] == 200 and not safe["error_kind"]:
            try:
                rows = normalize(safe["payload"], resource_type, resource_id, safe["received_at_utc"])
            except (ValueError, TypeError, AttributeError, KeyError):
                safe["error_kind"] = "schema_error"
        snapshot_id = uuid.uuid4().hex
        payload = json.dumps(safe["payload"], separators=(",", ":"), ensure_ascii=False)
        envelope = {"snapshot_id": snapshot_id, "cycle_id": cycle_id, "origin": origin,
                    "resource_type": resource_type, "resource_id": resource_id,
                    **{name: safe.get(name) for name in ["request_started_at_utc", "received_at_utc", "endpoint", "attempt_number", "http_status", "error_kind", "server_date", "retry_after_seconds"]},
                    "parameters_json": json.dumps(safe["parameters"]), "payload_json": payload,
                    "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(), "observation_count": len(rows)}
        columns = list(envelope)
        with self.connection:
            self.connection.execute(f"INSERT INTO snapshots ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(envelope.values()))
            for number, row in enumerate(rows):
                values = {"snapshot_id": snapshot_id, "row_number": number, "received_at_utc": safe["received_at_utc"],
                          **{name: row.get(name) for name in ["scheduled_stop_key", "trip_key", "bus_key", "stop_key", "route_number", "variant_key",
                                  "scheduled_departure_utc", "estimated_departure_utc", "estimated_delay_seconds", "scheduled_lead_seconds", "cancelled", "measurement_kind"]},
                          "fields_json": json.dumps(row, separators=(",", ":"))}
                cols = list(values)
                self.connection.execute(f"INSERT INTO observations ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", list(values.values()))
        return rows, safe["error_kind"]

    def start_cycle(self, cycle_id, started):
        with self.connection:
            self.connection.execute("INSERT INTO cycles VALUES (?, ?, NULL, 'running', NULL)", (cycle_id, started))

    def end_cycle(self, cycle_id, status, summary):
        with self.connection:
            self.connection.execute("UPDATE cycles SET finished_at_utc=?,status=?,summary_json=? WHERE cycle_id=?",
                                    (utc_now().isoformat(), status, json.dumps(summary), cycle_id))

    def track(self, records, now, retention_minutes):
        expires = (now + timedelta(minutes=retention_minutes)).isoformat()
        with self.connection:
            for key in {row["trip_key"] for row in records if row.get("trip_key")}:
                if re.fullmatch(r"[A-Za-z0-9_-]+", key):
                    self.connection.execute("""INSERT INTO tracked_trips VALUES (?, ?, NULL)
                         ON CONFLICT(trip_key) DO UPDATE SET expires_at_utc=MAX(expires_at_utc, excluded.expires_at_utc)""", (key, expires))

    def queued_trips(self, now, limit):
        with self.connection:
            self.connection.execute("DELETE FROM tracked_trips WHERE expires_at_utc<=?", (now.isoformat(),))
        return [r[0] for r in self.connection.execute("SELECT trip_key FROM tracked_trips ORDER BY COALESCE(last_attempt_at_utc,''),trip_key LIMIT ?", (limit,))]

    def mark_trip(self, key, now, retired=False):
        with self.connection:
            if retired:
                self.connection.execute("DELETE FROM tracked_trips WHERE trip_key=?", (key,))
            else:
                self.connection.execute("UPDATE tracked_trips SET last_attempt_at_utc=? WHERE trip_key=?", (now.isoformat(), key))


def collect_cycle(store, client, config):
    cycle_id, started = uuid.uuid4().hex, utc_now()
    store.start_cycle(cycle_id, started.isoformat())
    summary = {"cycle_id": cycle_id, "requests": 0, "successful_responses": 0, "observations": 0, "errors": 0, "trip_requests": 0}

    def collect(resource_type, resource_id, parameters):
        path = f"stops/{resource_id}/schedule.json" if resource_type == "stop" else f"trips/{resource_id}.json"
        last_status = None
        for attempt in client.attempts(path, parameters):
            rows, error = store.record(cycle_id, "live", resource_type, resource_id, attempt, client.api_key)
            last_status = attempt["http_status"]
            summary["requests"] += 1
            summary["observations"] += len(rows)
            summary["errors"] += int(error is not None)
            summary["successful_responses"] += int(last_status == 200 and error is None)
            if resource_type == "stop" and rows:
                store.track(rows, datetime.fromisoformat(attempt["received_at_utc"]), config["trip_retention_minutes"])
        return last_status

    try:
        for stop in config["seed_stops"]:
            local_now = utc_now().astimezone(LOCAL_ZONE)
            parameters = {"route": config["route"],
                          "start": (local_now - timedelta(minutes=config["lookback_minutes"])).strftime("%Y-%m-%dT%H:%M:%S"),
                          "end": (local_now + timedelta(minutes=config["lookahead_minutes"])).strftime("%Y-%m-%dT%H:%M:%S")}
            collect("stop", str(stop), parameters)
        for trip in store.queued_trips(utc_now(), config["max_trip_requests_per_cycle"]):
            status = collect("trip", trip, {})
            store.mark_trip(trip, utc_now(), retired=status == 404)
            summary["trip_requests"] += 1
        store.end_cycle(cycle_id, "complete" if not summary["errors"] else "completed_with_errors", summary)
        return summary
    except BaseException:
        store.end_cycle(cycle_id, "interrupted_or_failed", summary)
        raise


def replay(store, fixture):
    bundle = json.loads(Path(fixture).read_text())
    if bundle.get("origin") != "synthetic_fixture":
        raise ValueError("Offline replay requires an explicitly labelled synthetic fixture.")
    cycle_id = uuid.uuid4().hex
    store.start_cycle(cycle_id, utc_now().isoformat())
    observations = errors = 0
    for item in bundle["responses"]:
        timestamp = item["received_at_utc"]
        attempt = {"request_started_at_utc": timestamp, "received_at_utc": timestamp,
                   "endpoint": item["endpoint"], "parameters": {}, "attempt_number": 1,
                   "http_status": 200, "error_kind": None, "payload": item["payload"]}
        rows, error = store.record(cycle_id, "synthetic_fixture", item["resource_type"], item["resource_id"], attempt)
        observations += len(rows)
        errors += int(error is not None)
    result = {"origin": "synthetic_fixture", "responses": len(bundle["responses"]), "observations": observations, "errors": errors}
    store.end_cycle(cycle_id, "offline_replay", result)
    return result


def status(database):
    if not database.is_file():
        return {"database_exists": False, "live_collection_verified": False}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        by_origin = {origin: {"responses": count, "last_received_at_utc": last} for origin, count, last in connection.execute(
            "SELECT origin,COUNT(*),MAX(received_at_utc) FROM snapshots GROUP BY origin")}
        errors = {name: count for name, count in connection.execute("SELECT error_kind,COUNT(*) FROM snapshots WHERE error_kind IS NOT NULL GROUP BY error_kind")}
        live_ok = connection.execute("SELECT COUNT(*) FROM snapshots WHERE origin='live' AND http_status=200 AND error_kind IS NULL").fetchone()[0]
        return {"database_exists": True, "by_origin": by_origin, "observations": connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                "errors": errors, "live_collection_verified": live_ok > 0,
                "note": "These are stored capture statistics, not a claim that a collector process is currently running."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="collector_config.json")
    parser.add_argument("--database", help="Optional output database override")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--replay", help="Explicitly synthetic offline fixture")
    mode.add_argument("--continuous", action="store_true")
    parser.add_argument("--cycles", type=int, default=1, help="Finite live cycles (default 1); --continuous runs until interrupted")
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    root, config = config_path.parent, json.loads(config_path.read_text())
    validate_config(config)
    if args.cycles < 1:
        raise ValueError("--cycles must be positive.")
    database = Path(args.database).resolve() if args.database else root / config["database"]
    if args.status:
        print(json.dumps(status(database), indent=2))
        return 0
    if args.dry_run:
        print(json.dumps({"network_requests": 0, "database_writes": 0, "route": config["route"], "seed_stops": config["seed_stops"],
                          "max_trip_requests_per_cycle": config["max_trip_requests_per_cycle"], "requests_per_minute": config["requests_per_minute"],
                          "poll_interval_seconds": config["poll_interval_seconds"], "database": str(database)}, indent=2))
        return 0
    if args.replay and database == (root / config["database"]).resolve():
        raise ValueError("Use --database with a separate test path for synthetic replay.")
    key = None if args.replay else read_api_key(root, config["api_key_env"])
    with collector_lock(database):
        store = Store(database)
        try:
            if args.replay:
                result = replay(store, args.replay)
                print(json.dumps(result, indent=2))
                return int(result["errors"] > 0)
            client = ApiClient(key, config)
            completed = 0
            failure = False
            while args.continuous or completed < args.cycles:
                started = time.monotonic()
                result = collect_cycle(store, client, config)
                completed += 1
                failure |= bool(result["errors"])
                print(json.dumps(result), flush=True)
                if args.continuous or completed < args.cycles:
                    time.sleep(max(0, config["poll_interval_seconds"] - (time.monotonic() - started)))
            return int(failure)
        finally:
            store.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Collector stopped; completed responses remain stored.", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, AuthenticationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
