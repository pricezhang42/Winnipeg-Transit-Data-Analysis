"""Credential-safe HTTP snapshots and conservative Winnipeg Transit v4 normalization."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

API_BASE = "https://api.winnipegtransit.com/v4"
LOCAL_ZONE = ZoneInfo("America/Winnipeg")


def utc_now():
    return datetime.now(timezone.utc)


def sanitize(value, api_key=""):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if re.sub(r"[-_]", "", key.lower()) in {"apikey", "authorization"}
                else sanitize(item, api_key) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, api_key) for item in value]
    if isinstance(value, str):
        for secret in {api_key, quote(api_key, safe=""), quote_plus(api_key)} - {""}:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)(api[-_]key=)[^&\s\"<>]+", r"\1[REDACTED]", value)
    return value


class AuthenticationError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiClient:
    def __init__(self, api_key, config, opener=None, monotonic=time.monotonic, sleep=time.sleep, now=utc_now):
        self.api_key, self.config = api_key, config
        self.opener = opener or build_opener(NoRedirect()).open
        self.monotonic, self.sleep, self.now = monotonic, sleep, now
        self.next_request = 0.0

    def _pace(self):
        delay = self.next_request - self.monotonic()
        if delay > 0:
            self.sleep(delay)
        self.next_request = self.monotonic() + 60 / self.config["requests_per_minute"]

    def attempts(self, path, parameters):
        if not re.fullmatch(r"(?:stops/\d+/schedule|trips/[A-Za-z0-9_-]+)\.json", path):
            raise ValueError("Unsupported API path.")
        if "api-key" in parameters:
            raise ValueError("Credentials must not appear in stored request parameters.")
        url = API_BASE + "/" + path + "?" + urlencode({**parameters, "api-key": self.api_key, "json-camel-case": "false"})
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "WTDA-research-collector/1.0"})
        for number in range(self.config["max_retries"] + 1):
            self._pace()
            started = self.now()
            status, body, headers, error = None, b"", {}, None
            try:
                with self.opener(request, timeout=self.config["timeout_seconds"]) as response:
                    status, headers = response.status, response.headers
                    body = response.read(5_000_001)
            except HTTPError as exc:
                status, headers = exc.code, exc.headers or {}
                body = exc.read(5_000_001)
                error = f"http_{status}"
                exc.close()
            except (URLError, TimeoutError, socket.timeout, OSError):
                # Do not stringify transport exceptions: they can contain the full credentialed URL.
                error = "transport_error"
            received = self.now()
            payload = None
            if len(body) > 5_000_000:
                error = "response_too_large"
            elif body:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    error = error or "invalid_json"
                    payload = {"unparsed_body_excerpt": body[:4000].decode("utf-8", errors="replace")}
            elif status == 200:
                error = "empty_body"
            retry_after = None
            if headers.get("Retry-After"):
                try:
                    retry_after = max(0, float(headers["Retry-After"]))
                except ValueError:
                    try:
                        retry_after = max(0, (parsedate_to_datetime(headers["Retry-After"]) - received).total_seconds())
                    except (TypeError, ValueError):
                        pass
            yield {
                "request_started_at_utc": started.isoformat(), "received_at_utc": received.isoformat(),
                "endpoint": path, "parameters": parameters, "attempt_number": number + 1,
                "http_status": status, "error_kind": error,
                "server_date": headers.get("Date"), "retry_after_seconds": retry_after,
                "payload": sanitize(payload, self.api_key),
            }
            if status in (401, 403):
                raise AuthenticationError(f"Winnipeg Transit rejected the credential (HTTP {status}).")
            retryable = status == 429 or (status is not None and status >= 500) or error == "transport_error"
            if status == 200 or not retryable:
                return
            # Honor server cooldown even when retry budget is exhausted; subsequent requests use it too.
            delay = retry_after if retry_after is not None else min(60, 2 ** (number + 1))
            self.next_request = max(self.next_request, self.monotonic() + delay)
            if number >= self.config["max_retries"]:
                return


def identity(value):
    if isinstance(value, dict):
        value = value.get("key")
    return None if value is None else str(value)


def api_time(value):
    """Return UTC only when an API time is parseable and DST interpretation is unambiguous."""
    if not isinstance(value, str) or not value:
        return None, "missing"
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}(?:\.\d+)?", value):
        return None, "date_missing"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            first, second = dt.replace(tzinfo=LOCAL_ZONE, fold=0), dt.replace(tzinfo=LOCAL_ZONE, fold=1)
            if first.utcoffset() != second.utcoffset():
                return None, "ambiguous_or_nonexistent_local_time"
            dt = first
        return dt.astimezone(timezone.utc), None
    except (ValueError, TypeError):
        return None, "invalid_time"


def normalize(payload, resource_type, resource_id, received_at, requested_route="BLUE"):
    if not isinstance(payload, dict):
        raise ValueError("Expected an API response object.")
    result = []
    if resource_type == "stop":
        root = payload.get("stop-schedule")
        if not isinstance(root, dict) or not isinstance(root.get("route-schedules"), list):
            raise ValueError("Expected stop-schedule.route-schedules list.")
        blocks = []
        for schedule in root["route-schedules"]:
            route = schedule.get("route") or {}
            number = str(route.get("number", route.get("key", requested_route)))
            if number != requested_route:
                continue
            blocks.append((schedule.get("scheduled-stops"), root.get("stop") or {"key": resource_id}, route, {}))
    elif resource_type == "trip":
        root = payload.get("trip", payload.get("trip-schedule"))
        if not isinstance(root, dict):
            raise ValueError("Expected trip or trip-schedule object.")
        blocks = [(root.get("scheduled-stops"), {}, root.get("route") or {}, root)]
    else:
        raise ValueError("Unknown resource type.")
    collected = datetime.fromisoformat(received_at)
    for events, shared_stop, route, trip in blocks:
        if not isinstance(events, list):
            raise ValueError("Expected scheduled-stops list.")
        for position, event in enumerate(events):
            if not isinstance(event, dict):
                raise ValueError("Expected a scheduled-stop object.")
            stop = event.get("stop") or shared_stop
            times = event.get("times") or {}
            departure = times.get("departure") or {}
            arrival = times.get("arrival") or {}
            scheduled, scheduled_issue = api_time(departure.get("scheduled"))
            estimated, estimated_issue = api_time(departure.get("estimated"))
            variant = event.get("variant") or trip.get("variant") or {}
            destination = event.get("destination") or (variant.get("destination") if isinstance(variant, dict) else None)
            if isinstance(destination, dict):
                destination = destination.get("name") or destination.get("key")
            cancelled = event.get("cancelled")
            if not isinstance(cancelled, bool):
                cancelled = {"true": True, "false": False, "1": True, "0": False}.get(str(cancelled).lower())
            result.append({
                "scheduled_stop_key": identity(event.get("key")),
                "trip_key": identity(event.get("trip-key") or trip.get("key") or (resource_id if resource_type == "trip" else None)),
                "bus_key": identity(event.get("bus") or trip.get("bus")),
                "stop_key": identity(stop),
                "stop_number": str(stop["number"]) if isinstance(stop, dict) and stop.get("number") is not None else None,
                "route_key": identity(route), "route_number": str(route.get("number", requested_route)),
                "variant_key": identity(variant), "destination": destination,
                "variant_name": variant.get("name") if isinstance(variant, dict) else None,
                "previous_trip_key": identity(trip.get("previous-trip-key")),
                "next_trip_key": identity(trip.get("next-trip-key")),
                "effective_from": trip.get("effective-from"), "effective_to": trip.get("effective-to"),
                "schedule_type": trip.get("schedule-type"), "cancelled": cancelled,
                "response_stop_index": position if resource_type == "trip" else None,
                "scheduled_departure_raw": departure.get("scheduled"), "estimated_departure_raw": departure.get("estimated"),
                "scheduled_arrival_raw": arrival.get("scheduled"), "estimated_arrival_raw": arrival.get("estimated"),
                "scheduled_departure_utc": scheduled.isoformat() if scheduled else None,
                "estimated_departure_utc": estimated.isoformat() if estimated else None,
                "estimated_delay_seconds": (estimated - scheduled).total_seconds() if scheduled and estimated else None,
                "scheduled_lead_seconds": (scheduled - collected).total_seconds() if scheduled else None,
                "time_issues": {k: v for k, v in {"scheduled": scheduled_issue, "estimated": estimated_issue}.items() if v},
                "measurement_kind": "api_estimate",
            })
    return result
