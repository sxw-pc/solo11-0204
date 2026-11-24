import unittest

from kiln_cars.__main__ import apply
from kiln_cars.db import Refused, connect, transaction


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
        self.addCleanup(self.db.close)
        self.call('bench-scale', id='G', cell='cold-end', tolerance=1)

    def call(self, action, **fields):
        with transaction(self.db):
            return apply(self.db, action, fields)

    def test_ticket_refs_original_check_after_scale_stops(self):
        self.call('bench-check', id='C1', scale='G', minute=2, standard=10, display=11, assessor='op')
        result = self.call('bench-weigh', id='W', scale='G', minute=3, item='setter', tare=5, gross=12)
        self.assertEqual((result['net'], result['check_id']), (7, 'C1'))
        self.call('bench-check', id='C2', scale='G', minute=6, standard=10, display=13, assessor='op')
        self.assertEqual(self.call('bench-ticket', id='W'), result)
        with self.assertRaises(Refused):
            self.call('bench-weigh', id='N', scale='G', minute=7, item='setter', tare=5, gross=12)

    def test_future_check_does_not_clear_past_weighing(self):
        self.call('bench-check', id='C1', scale='G', minute=9, standard=10, display=10, assessor='op')
        self.assertEqual(self.call('bench-status', scale='G', minute=4)['state'], 'UNCHECKED')
