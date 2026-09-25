"""Read the source in chunks; preserve observations and make quality explicit."""

from collections import Counter
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

TIME_FORMAT = "%Y %b %d %I:%M:%S %p"
COLUMNS = {
    "Row ID": "row_id",
    "Stop Number": "stop_number",
    "Route Number": "route_number",
    "Route Name": "route_name",
    "Route Destination": "destination",
    "Day Type": "day_type",
    "Scheduled Time": "scheduled_time_raw",
    "Deviation": "deviation_raw",
    "Location": "location_wkt",
}
SOURCE_URL = "https://data.winnipeg.ca/Transit/Recent-Transit-On-Time-Performance-Data/gp3k-am4u"


def parse_deviation(raw):
    """Accept integer seconds with optional correctly grouped thousands commas."""
    text = raw.astype("string").str.strip()
    valid = text.str.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", na=False)
    return pd.to_numeric(
        text.where(valid).str.replace(",", "", regex=False), errors="coerce"
    ).astype("Int64")


def assign_splits(times, config):
    boundaries = [pd.Timestamp(config[k]) for k in (
        "train_start", "validation_start", "test_start", "test_end_exclusive"
    )]
    if not all(a < b for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError("Split boundaries must be strictly increasing.")
    split = pd.Series("outside", index=times.index, dtype="string")
    for label, start, end in zip(["train", "validation", "test"], boundaries, boundaries[1:]):
        split.loc[times.ge(start) & times.lt(end)] = label
    split.loc[times.ge(boundaries[-1])] = "later"
    split.loc[times.isna()] = "invalid"
    return split


def prepare_route(raw, config):
    """Keep every selected row, including flagged rows; never infer a trip ID."""
    frame = raw.rename(columns=COLUMNS).copy()
    frame["scheduled_time_local"] = pd.to_datetime(
        frame["scheduled_time_raw"], format=TIME_FORMAT, errors="coerce"
    )
    frame["deviation_seconds"] = parse_deviation(frame["deviation_raw"])
    frame["delay_seconds"] = -frame["deviation_seconds"]
    time = frame["scheduled_time_local"]
    frame["scheduled_date"] = time.dt.normalize()
    frame["day_of_week"] = time.dt.dayofweek.astype("Int8")
    frame["hour"] = time.dt.hour.astype("Int8")
    frame["minute"] = time.dt.minute.astype("Int8")
    frame["month"] = time.dt.month.astype("Int8")
    frame["is_weekend"] = frame["day_of_week"].ge(5).astype("boolean")
    coords = frame["location_wkt"].str.extract(
        r"^POINT \((-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)\)$"
    ).apply(pd.to_numeric, errors="coerce")
    frame["longitude"], frame["latitude"] = coords[0], coords[1]
    frame["flag_invalid_location"] = ~(
        frame["longitude"].between(-180, 180) & frame["latitude"].between(-90, 90)
    )
    required = ["row_id", "route_number", "stop_number", "scheduled_time_local", "delay_seconds"]
    frame["valid_required_fields"] = frame[required].notna().all(axis=1)
    frame["flag_missing_destination"] = frame["destination"].isna()
    frame["flag_missing_day_type"] = frame["day_type"].isna()
    frame["flag_extreme_delay"] = frame["delay_seconds"].abs().gt(
        config["extreme_delay_seconds"]
    ).fillna(False)
    # Row order in the source is preserved for deterministic duplicate provenance.
    signature = [c for c in COLUMNS.values() if c != "row_id"]
    frame["flag_duplicate_candidate"] = frame.duplicated(signature, keep="first")
    frame["duplicate_of_row_id"] = frame.groupby(signature, dropna=False)["row_id"].transform("first").where(
        frame["flag_duplicate_candidate"]
    )
    # Conflicting IDs are not silently repaired. Identical repeated IDs may be flagged.
    id_rows = frame.loc[frame["row_id"].duplicated(keep=False)]
    if not id_rows.empty and id_rows.groupby("row_id")[signature].nunique(dropna=False).gt(1).any().any():
        raise ValueError("Conflicting records share a Row ID; inspect the source before proceeding.")
    frame["baseline_eligible"] = (
        frame["valid_required_fields"]
        & ~frame["flag_extreme_delay"]
        & ~frame["flag_duplicate_candidate"]
        & ~frame["flag_missing_destination"]
        & ~frame["flag_missing_day_type"]
    )
    frame["split"] = assign_splits(time, config)
    return frame.sort_values(["scheduled_time_local", "row_id"], kind="stable").reset_index(drop=True)


def ingest(source: Path, config):
    """Audit the full CSV and collect only the selected route in memory."""
    rows = comma_values = invalid_times = invalid_deviations = 0
    missing, routes, days, daily = Counter(), Counter(), Counter(), Counter()
    stops, row_ids, parts = set(), [], []
    time_min = time_max = previous_time = None
    time_sorted = True
    for chunk in pd.read_csv(source, dtype="string", chunksize=config["chunksize"]):
        if set(chunk.columns) != set(COLUMNS):
            raise ValueError(f"Unexpected CSV columns: {list(chunk.columns)}")
        chunk = chunk.replace(r"^\s*$", pd.NA, regex=True)
        offset = rows
        rows += len(chunk)
        missing.update({k: int(v) for k, v in chunk.isna().sum().items()})
        routes.update(chunk["Route Number"].value_counts().to_dict())
        days.update(chunk["Day Type"].value_counts().to_dict())
        stops.update(chunk["Stop Number"].dropna().unique())
        # Hashes are used for a compact candidate check only, not to drop rows.
        row_ids.append(pd.util.hash_pandas_object(chunk["Row ID"], index=False).to_numpy())
        times = pd.to_datetime(chunk["Scheduled Time"], format=TIME_FORMAT, errors="coerce")
        invalid_times += int(times.isna().sum())
        daily.update(times.dt.strftime("%Y-%m-%d").value_counts().to_dict())
        if not times.is_monotonic_increasing or (previous_time is not None and times.iloc[0] < previous_time):
            time_sorted = False
        previous_time = times.iloc[-1]
        lo, hi = times.min(), times.max()
        if pd.notna(lo):
            time_min = lo if time_min is None else min(time_min, lo)
            time_max = hi if time_max is None else max(time_max, hi)
        comma_values += int(chunk["Deviation"].str.contains(",", regex=False, na=False).sum())
        invalid_deviations += int(parse_deviation(chunk["Deviation"]).isna().sum())
        selected = chunk["Route Number"].eq(config["route"]).fillna(False)
        part = chunk.loc[selected].copy()
        part["source_record_number"] = np.flatnonzero(selected.to_numpy()) + offset + 1
        parts.append(part)
        print(f"Read {rows:,} source records", flush=True)
    if rows == 0 or not parts or not any(len(part) for part in parts):
        raise ValueError(f"No observations found for route {config['route']}.")
    route = prepare_route(pd.concat(parts, ignore_index=True), config)
    all_ids = np.concatenate(row_ids)
    unique_hashes = len(np.unique(all_ids))
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    calendar = pd.date_range(time_min.normalize(), time_max.normalize(), freq="D")
    source_profile = {
        "source_file": source.name, "source_url": SOURCE_URL,
        "source_bytes": source.stat().st_size, "source_sha256": digest,
        "source_rows": rows, "route_counts": dict(routes.most_common()),
        "unique_routes": len(routes), "unique_stops": len(stops),
        "missing_fields": dict(missing), "invalid_scheduled_times": invalid_times,
        "invalid_deviations_after_comma_parsing": invalid_deviations,
        "deviations_with_thousands_commas": comma_values,
        "row_id_duplicate_hash_candidates": rows - unique_hashes,
        "date_min": str(time_min), "date_max": str(time_max),
        "source_sorted_by_time": time_sorted, "daily_source_rows": dict(sorted(daily.items())),
        "missing_calendar_dates": [d.strftime("%Y-%m-%d") for d in calendar if d.strftime("%Y-%m-%d") not in daily],
        "day_types": dict(days),
    }
    return route, source_profile
