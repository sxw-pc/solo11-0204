import unittest
from contextlib import closing
from pathlib import Path

from kiln_cars.__main__ import run_fixture, apply
from kiln_cars.db import Refused, connect, transaction


class ShiftEnergy(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'energy.json'
        self.steps = {item['step']: item['result'] for item in run_fixture(self.db, fixture)}

    def test_energy_uses_rollover_and_requires_each_circuit(self):
        self.assertEqual(self.steps['before second circuit']['state'], 'INCOMPLETE')
        self.assertEqual(self.steps['before second circuit']['circuits']['heat']['watt_hours'], 8)
        self.assertEqual(self.steps['complete statement']['watt_hours'], 15)

    def test_independent_reads_at_same_minute_expose_conflict(self):
        with transaction(self.db):
            apply(self.db, 'energy-reading', dict(id='H-C', meter_id='M1', minute=20, count=6, document='later copy'))
            report = apply(self.db, 'energy-statement', dict(begin=10, end=20, circuits=['heat', 'fan']))
        self.assertEqual(report['circuits']['heat']['state'], 'CONFLICT')
        self.assertEqual(report['circuits']['fan']['watt_hours'], 7)

    def test_receipt_is_idempotent_and_payload_is_immutable(self):
        row = self.steps['H-end']
        with transaction(self.db):
            self.assertEqual(apply(self.db, 'energy-reading', row), row)
        with self.assertRaises(Refused), transaction(self.db):
            apply(self.db, 'energy-reading', {**row, 'count': 6})
