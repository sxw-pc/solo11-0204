from contextlib import closing
import sqlite3
from unittest import TestCase

from kiln_cars.__main__ import apply
from kiln_cars.db import connect, transaction


class YardTests(TestCase):
    def setUp(self):
        self.db=self.enterContext(closing(connect(':memory:')))
        self.call('yard-track',id='AB',source='A',target='B',length=4)
        self.call('yard-cart',id='C1',node='A',length=2)

    def call(self, action, **fields):
        with transaction(self.db):
            return apply(self.db, action, fields)

    def test_actual_movement_uses_track_endpoint(self):
        self.assertEqual(self.call('yard-move',id='M',cart='C1',track='AB',document='x')['node'],'B')

    def test_occupied_destination_refuses_movement(self):
        self.call('yard-cart',id='C2',node='B',length=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.call('yard-move',id='M',cart='C1',track='AB',document='x')
        self.assertEqual(self.call('yard-status',cart='C1')['node'],'A')
        self.assertEqual(self.db.execute('SELECT count(*) FROM yard_move').fetchone()[0],0)
