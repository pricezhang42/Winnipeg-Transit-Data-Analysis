"""Tests for helpers retained after removing the legacy reliability exporters."""
import tempfile
import unittest
from pathlib import Path
from history_utils import destination, digest, distance, point


class HistoryUtilsTests(unittest.TestCase):
    def test_destination_normalization(self):
        self.assertEqual(destination('  To Downtown  '), 'downtown')
        self.assertEqual(destination('UNIVERSITY OF MANITOBA'), 'university of manitoba')
        self.assertEqual(destination(None), '')

    def test_coordinate_validation(self):
        self.assertEqual(point('POINT (-97.1 49.8)'), (-97.1, 49.8))
        for invalid in ['POINT(nan 49)', 'POINT(1e309 49)', 'POINT(-181 49)', 'POINT(-97 91)', '', None]:
            self.assertIsNone(point(invalid))

    def test_distance_in_metres(self):
        self.assertEqual(distance((-97, 49), (-97, 49)), 0)
        self.assertAlmostEqual(distance((0, 0), (0, 1)), 111194.93, places=2)

    def test_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input'
            path.write_bytes(b'abc')
            self.assertEqual(digest(path), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')


if __name__ == '__main__':
    unittest.main()
