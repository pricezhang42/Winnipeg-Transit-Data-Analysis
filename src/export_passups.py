"""Export the City's pass-up CSV for a portable static dashboard (stdlib only)."""
import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

COLUMNS = ['Pass-Up ID', 'Pass-Up Type', 'Time', 'Route Number', 'Route Name',
           'Route Destination', 'Location']
POINT = re.compile(r'^POINT\s*\(\s*([-+\d.eE]+)\s+([-+\d.eE]+)\s*\)$')


def coordinates(value):
    match = POINT.fullmatch(value.strip())
    if match:
        try:
            lon, lat = map(float, match.groups())
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                return [lon, lat]
        except ValueError:
            pass
    return None


def export(source, output='dashboard/dist/passups'):
    source, output = Path(source), Path(output)
    months = defaultdict(list)
    types, routes, destinations = [], [], []
    dictionaries = [{}, {}, {}]
    missing_route = invalid_location = duplicate_ids = 0
    seen = set()

    def code(value, values, lookup):
        if value not in lookup:
            lookup[value] = len(values)
            values.append(value)
        return lookup[value]

    with source.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not set(COLUMNS) <= set(reader.fieldnames or []):
            raise ValueError('CSV must contain: ' + ', '.join(COLUMNS))
        for record_number, row in enumerate(reader, 1):
            try:
                timestamp = datetime.strptime(row['Time'], '%m/%d/%Y %I:%M:%S %p').isoformat()
            except (ValueError, TypeError) as exc:
                raise ValueError(f'Invalid Time in source record {record_number}; no rows silently dropped') from exc
            route = (row['Route Number'] or '').strip()
            point = coordinates(row['Location'] or '')
            missing_route += not route
            invalid_location += point is None
            duplicate_ids += row['Pass-Up ID'] in seen
            seen.add(row['Pass-Up ID'])
            months[timestamp[:7]].append([
                row['Pass-Up ID'], timestamp,
                code(route, routes, dictionaries[0]),
                code(row['Pass-Up Type'] or '', types, dictionaries[1]),
                code(row['Route Destination'] or '', destinations, dictionaries[2]),
                point, record_number,
            ])
    if not months:
        raise ValueError('CSV has no pass-up records')
    output.mkdir(parents=True, exist_ok=True)
    summaries = []
    for month, rows in sorted(months.items()):
        rows.sort(key=lambda row: (row[1], row[6]))
        (output / f'{month}.json').write_text(json.dumps(rows, separators=(',', ':'), allow_nan=False) + '\n')
        summaries.append({'month': month, 'records': len(rows), 'first': rows[0][1][:10], 'last': rows[-1][1][:10]})
    index = {
        'records': sum(m['records'] for m in summaries), 'months': summaries,
        'first': summaries[0]['first'], 'last': summaries[-1]['last'],
        'routes': routes, 'types': types, 'destinations': destinations,
        'columns': ['id', 'localTime', 'routeIndex', 'typeIndex', 'destinationIndex', 'coordinates', 'sourceRecord'],
        'timezone': 'America/Winnipeg', 'sourceFile': source.name,
        'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'exportedAt': datetime.now(timezone.utc).isoformat(),
        'missingRoute': missing_route, 'invalidLocation': invalid_location,
        'additionalDuplicateIds': duplicate_ids,
        'notes': 'Reported pass-up records, not passenger counts or pass-up rates. All rows retained, including missing routes and duplicate IDs. Times are source local wall-clock values; no UTC conversion.',
    }
    (output / 'index.json').write_text(json.dumps(index, separators=(',', ':'), allow_nan=False) + '\n')
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Downloaded Transit Pass-ups CSV')
    parser.add_argument('--output', default='dashboard/dist/passups')
    args = parser.parse_args()
    index = export(args.source, args.output)
    print(f"Exported {index['records']:,} records, {index['first']} through {index['last']}, to {args.output}")


if __name__ == '__main__':
    main()
