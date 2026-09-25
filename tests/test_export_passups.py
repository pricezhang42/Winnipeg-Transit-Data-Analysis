import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.export_passups import COLUMNS, coordinates, export


class PassupExportTests(unittest.TestCase):
    def test_preserves_rows_unknown_routes_duplicates_and_month_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.csv'
            with source.open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(COLUMNS)
                writer.writerows([
                    ['1', 'Full Bus Pass-Up', '01/31/2026 11:59:59 PM', 'BLUE', 'Blue', 'South', 'POINT (-97.1 49.8)'],
                    ['1', 'Wheelchair User Pass-Up', '02/01/2026 12:00:00 AM', '', '', '', 'bad'],
                ])
            output = Path(tmp) / 'out'
            index = export(source, output)
            self.assertEqual(index['records'], 2)
            self.assertEqual(index['missingRoute'], 1)
            self.assertEqual(index['invalidLocation'], 1)
            self.assertEqual(index['additionalDuplicateIds'], 1)
            self.assertEqual([m['month'] for m in index['months']], ['2026-01', '2026-02'])
            second = json.loads((output / '2026-02.json').read_text())[0]
            self.assertEqual(second[1], '2026-02-01T00:00:00')
            self.assertEqual(second[6], 2)
            self.assertEqual(index['routes'][second[2]], '')
            self.assertIsNone(second[5])

    def test_invalid_time_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.csv'
            with source.open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(COLUMNS)
                writer.writerow(['1', 'type', 'bad timestamp', '', '', '', ''])
            output = Path(tmp) / 'out'
            with self.assertRaisesRegex(ValueError, 'source record 1'):
                export(source, output)
            self.assertFalse(output.exists())

    def test_coordinate_validation(self):
        self.assertEqual(coordinates('POINT (-97.1 49.8)'), [-97.1, 49.8])
        for value in ['', 'POINT (181 50)', 'POINT (0 91)', 'POINT (1e999 0)']:
            self.assertIsNone(coordinates(value))


if __name__ == '__main__':
    unittest.main()
