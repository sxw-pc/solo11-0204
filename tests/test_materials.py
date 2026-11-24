from contextlib import closing
import unittest

from kiln_cars import isolation, loading, materials
from kiln_cars.db import Refused, connect, transaction


class MaterialFlows(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        with transaction(self.db):
            loading.firing(self.db,'F','oxidation',2,'log')
            loading.slot(self.db,'F','C1','top',1200)
            loading.slot(self.db,'F','C2','top',1200)
            materials.arrival(self.db,'BOX','lot-a',3,'source-doc')
            materials.pick(self.db,'P1','BOX','F','C1','top',2)
            materials.pick(self.db,'P2','BOX','F','C2','top',1)

    def test_locate_ties_real_stock_and_zones(self):
        initial = isolation.locate(self.db,'lot-a')
        self.assertEqual(initial['stock'],{'BOX':0})
        self.assertEqual([item['units'] for item in initial['zones'].values()],[2,1])
        with transaction(self.db):
            materials.give_back(self.db,'R','P1',2)
        revised = isolation.locate(self.db,'lot-a')
        self.assertEqual(revised['stock'],{'BOX':2})
        self.assertEqual(list(revised['zones']),['P2'])

    def test_overdraws_are_rejected(self):
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick(self.db,'P3','BOX','F','C1','top',1)
        with self.assertRaises(Refused), transaction(self.db):
            materials.give_back(self.db,'R','P2',2)
