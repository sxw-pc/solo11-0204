from contextlib import closing
import unittest

from kiln_cars import isolation, loading, materials
from kiln_cars.db import Refused, connect, transaction


class MaterialFlows(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        with transaction(self.db):
            loading.firing(self.db, 'F', 'oxidation', 2, 'log')
            loading.slot(self.db, 'F', 'C1', 'top', 1200)
            loading.slot(self.db, 'F', 'C2', 'top', 1200)
            materials.arrival(self.db, 'BOX', 'lot-a', 3, 'source-doc')
            materials.pick(self.db, 'P1', 'BOX', 'F', 'C1', 'top', 2)
            materials.pick(self.db, 'P2', 'BOX', 'F', 'C2', 'top', 1)

    def test_locate_ties_real_stock_and_zones(self):
        initial = isolation.locate(self.db, 'lot-a')
        self.assertEqual(initial['stock'], {'BOX': 0})
        self.assertEqual([item['units'] for item in initial['zones'].values()], [2, 1])
        with transaction(self.db):
            materials.give_back(self.db, 'R', 'P1', 2)
        revised = isolation.locate(self.db, 'lot-a')
        self.assertEqual(revised['stock'], {'BOX': 2})
        self.assertEqual(list(revised['zones']), ['P2'])

    def test_overdraws_are_rejected(self):
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick(self.db, 'P3', 'BOX', 'F', 'C1', 'top', 1)
        with self.assertRaises(Refused), transaction(self.db):
            materials.give_back(self.db, 'R', 'P2', 2)


class MixedLotIsolation(unittest.TestCase):
    """One supplier carton, three physical units, two destination kiln zones.

    One unit carries an explicit document proving a clean lot; the other two
    are an unattributed mixed packing (one problem lot B, one normal lot N).
    All issue lines are scanned by carton number only.
    """

    PROBLEM = 'lot-bad'

    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        with transaction(self.db):
            loading.firing(self.db, 'F', 'oxidation', 4, 'log')
            for car in ('C1', 'C2', 'C3', 'C4'):
                loading.slot(self.db, 'F', car, 'top', 1200)
            isolation.quarantine_position(self.db, 'Q1', 1)
            isolation.quarantine_position(self.db, 'Q2', 1)
            materials.split_arrival(
                self.db, 'MIX', 3, 'supplier-box-MIX',
                [{'units': 1, 'lot': 'lot-good', 'document': 'cert-seg'},
                 {'units': 2, 'lot': None, 'document': 'mixed-packing'}])

    def _directive(self, lot=None):
        return isolation.directive(self.db, lot or self.PROBLEM)

    def _verdicts(self, lot=None):
        return {item['holder']: item['verdict'] for item in self._directive(lot)['holders']}

    def _trace_mixed_pair(self):
        """The shared reissue scenario: B/N pair leaves the carton for C1, C2.

        The documented clean unit stays in stock, is later issued and returned
        once, then consumed by its single reissue onto C4.
        """
        materials.pick(self.db, 'P1', 'MIX', 'F', 'C1', 'top', 1,
                       picked_at='2026-11-01T08:00:00+00:00')
        materials.pick(self.db, 'P2', 'MIX', 'F', 'C2', 'top', 1,
                       picked_at='2026-11-01T08:05:00+00:00')
        # The certified clean unit is issued from remaining stock, then returned.
        materials.pick(self.db, 'P0', 'MIX', 'F', 'C3', 'top', 1,
                       picked_at='2026-11-01T09:00:00+00:00')
        isolation.register_packing(self.db, 'cert-seg', 'MIX', 'P0', 'lot-good', 1)
        materials.give_back(self.db, 'R0', 'P0', 1)
        # Returned remainder: one later issue, whole balance, third destination.
        materials.pick(self.db, 'P3', 'MIX', 'F', 'C4', 'top', 1,
                       picked_at='2026-11-02T09:00:00+00:00', reissue=True)
        isolation.register_composition(self.db, 'supplier-trace', 'MIX',
                                       {self.PROBLEM: 1, 'lot-normal': 1})

    def test_carton_only_issue_lines_and_conservation(self):
        with transaction(self.db):
            materials.pick_line(self.db, 'L1', 'MIX', 1, picked_at='2026-11-01T08:00:00+00:00')
            materials.pick_line(self.db, 'L2', 'MIX', 1, picked_at='2026-11-01T08:05:00+00:00')
            materials.direct_line(self.db, 'L1', 'F', 'C1', 'top')
            materials.direct_line(self.db, 'L2', 'F', 'C2', 'top')
        # Every carton unit still balances: two on zones, one in stock.
        record = self._directive()['conservation'][0]
        self.assertEqual(record['received'], 3)
        self.assertEqual(record['held'], 3)
        # No packing proof locates a unit: both zones and the stock unit could carry B.
        verdicts = self._verdicts()
        self.assertEqual(verdicts['zone:F|C1|top'], 'SUSPECT')
        self.assertEqual(verdicts['zone:F|C2|top'], 'SUSPECT')
        self.assertEqual(verdicts['stock:MIX'], 'SUSPECT')
        # The same physical quantity must never be counted in two zones at once.
        self.assertEqual(sum(item['units'] for item in self._directive()['holders']), 3)

    def test_certified_lot_is_prioritised_and_returned_remainder_reissues_once(self):
        with transaction(self.db):
            self._trace_mixed_pair()
        verdicts = self._verdicts()
        # Documented clean unit followed through its return onto the reissue zone.
        self.assertEqual(verdicts['zone:F|C4|top'], 'CLEAR')
        # The two destination zones are the minimum safe scope for the B/N pair.
        self.assertEqual(verdicts['zone:F|C1|top'], 'SUSPECT')
        self.assertEqual(verdicts['zone:F|C2|top'], 'SUSPECT')
        self.assertNotIn('stock:MIX', verdicts)
        self.assertEqual(self._directive()['conservation'][0]['held'], 3)
        # The reissued remainder cannot be returned, split, or reissued again.
        with self.assertRaises(Refused), transaction(self.db):
            materials.give_back(self.db, 'R2', 'P3', 1)
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick(self.db, 'P4', 'MIX', 'F', 'C3', 'top', 1, reissue=True)
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick_line(self.db, 'L9', 'MIX', 1)
        # A partial reissue is refused: the whole returned balance has one later issue.
        with closing(connect(':memory:')) as other:
            with transaction(other):
                loading.firing(other, 'G', 'oxidation', 1, 'log')
                loading.slot(other, 'G', 'K1', 'top', 1200)
                materials.arrival(other, 'B2', 'lot-x', 2, 'd')
                materials.pick(other, 'X1', 'B2', 'G', 'K1', 'top', 2)
                materials.give_back(other, 'X2', 'X1', 2)
                with self.assertRaises(Refused):
                    materials.pick(other, 'X3', 'B2', 'G', 'K1', 'top', 1, reissue=True)

    def test_frozen_zones_and_cartons_refuse_new_issues_and_redistribution(self):
        with transaction(self.db):
            self._trace_mixed_pair()
            isolation.impose(self.db, self.PROBLEM)
        # Frozen zones reject fresh issues and returns; the carton rejects new lines.
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick(self.db, 'P5', 'MIX', 'F', 'C1', 'top', 1)
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick_line(self.db, 'L5', 'MIX', 1)
        with self.assertRaises(Refused), transaction(self.db):
            materials.give_back(self.db, 'R3', 'P2', 1)
        # The proven-clear reissue zone keeps issuing from a different, clean carton.
        with transaction(self.db):
            materials.arrival(self.db, 'SAFE', 'lot-other', 1, 'd')
            materials.pick(self.db, 'P6', 'SAFE', 'F', 'C4', 'top', 1)

    def test_late_original_record_isolates_and_releases_real_objects_only(self):
        with transaction(self.db):
            self._trace_mixed_pair()
            isolation.impose(self.db, self.PROBLEM)
        # A late original document locates the normal unit at C2: B is forced to C1.
        with transaction(self.db):
            self.assertEqual(
                isolation.register_packing(self.db, 'late-original', 'MIX', 'P2', 'lot-normal', 1),
                'late-original')
        verdicts = self._verdicts()
        self.assertEqual(verdicts['zone:F|C1|top'], 'ISOLATED')
        self.assertEqual(verdicts['zone:F|C2|top'], 'CLEAR')
        self.assertEqual(verdicts['zone:F|C4|top'], 'CLEAR')
        with transaction(self.db):
            reconciliation = isolation.reconcile(self.db, self.PROBLEM)
        self.assertEqual(len(reconciliation['released']), 1)
        # The isolated zone stays frozen; the released zone may receive clean material.
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick(self.db, 'P7', 'MIX', 'F', 'C1', 'top', 1)
        with transaction(self.db):
            materials.arrival(self.db, 'SAFE2', 'lot-x', 1, 'd')
            materials.pick(self.db, 'P8', 'SAFE2', 'F', 'C2', 'top', 1)
        # The late document clears only its own object; the C1 relation is still pending.
        self.assertEqual(self._verdicts()['zone:F|C1|top'], 'ISOLATED')

    def test_quarantine_transfer_verification_and_original_receipt_recheck(self):
        with transaction(self.db):
            self._trace_mixed_pair()
            isolation.impose(self.db, self.PROBLEM)
            isolation.register_packing(self.db, 'late-original', 'MIX', 'P2', 'lot-normal', 1)
            isolation.reconcile(self.db, self.PROBLEM)
        # The B-bearing receipt at C1 is moved as one complete physical group.
        with transaction(self.db):
            isolation.reserve_group(self.db, 'G1', 'Q1', 'MIX', 1, pick='P1')
        # A second group does not fit: capacity is reserved up front per arriving group.
        with self.assertRaises(Refused), transaction(self.db):
            isolation.reserve_group(self.db, 'G2', 'Q1', 'MIX', 1, pick='P2')
        with transaction(self.db):
            isolation.transfer_out(self.db, 'G1')
        # Re-posting the same transfer is refused rather than counted as two groups.
        with self.assertRaises(Refused), transaction(self.db):
            isolation.transfer_out(self.db, 'G1')
        # Origin zone stays frozen until the move is checked against the original receipt.
        self.assertTrue(self.db.execute(
            "SELECT 1 FROM material_hold WHERE state='FROZEN' AND car='C1' AND zone='top'").fetchone())
        with transaction(self.db):
            isolation.verify_transfer(self.db, 'G1', 'supplier-box-MIX')
        self.assertFalse(self.db.execute(
            "SELECT 1 FROM material_hold WHERE state='FROZEN' AND kind='zone'").fetchone())
        directive = self._directive()
        self.assertIn('quarantine:Q1', {item['holder'] for item in directive['holders']})
        # The quarantined physical unit still counts exactly once in carton conservation.
        self.assertEqual(directive['conservation'][0]['held'], 3)
        self.assertEqual(sum(item['units'] for item in directive['holders']), 3)

    def test_duplicate_evidence_is_idempotent_but_conflicting_reposts_refused(self):
        with transaction(self.db):
            materials.pick(self.db, 'P1', 'MIX', 'F', 'C1', 'top', 1)
            materials.pick(self.db, 'P2', 'MIX', 'F', 'C2', 'top', 1)
            isolation.register_packing(self.db, 'cert-seg', 'MIX', 'P1', 'lot-good', 1)
            # Same document, same content: idempotent.
            isolation.register_packing(self.db, 'cert-seg', 'MIX', 'P1', 'lot-good', 1)
            isolation.register_composition(self.db, 'supplier-trace', 'MIX',
                                           {self.PROBLEM: 1, 'lot-normal': 1})
            isolation.register_composition(self.db, 'supplier-trace', 'MIX',
                                           {self.PROBLEM: 1, 'lot-normal': 1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM material_lot_proof').fetchone()[0], 3)
        with self.assertRaises(Refused), transaction(self.db):
            isolation.register_packing(self.db, 'cert-seg', 'MIX', 'P1', 'lot-normal', 1)
        with self.assertRaises(Refused), transaction(self.db):
            isolation.register_composition(self.db, 'supplier-trace', 'MIX', {self.PROBLEM: 2})
        # Packing cannot claim more units than the original receipt line holds.
        with self.assertRaises(Refused), transaction(self.db):
            isolation.register_packing(self.db, 'overreach', 'MIX', 'P2', 'lot-normal', 2)
        # Infeasible multiset (two B where only one unattributed unit exists) is refused.
        with self.assertRaises(Refused), transaction(self.db):
            isolation.register_composition(self.db, 'bad-trace', 'MIX',
                                           {self.PROBLEM: 1, 'lot-normal': 2})

    def test_in_place_stop_waits_for_independent_packing_evidence(self):
        with transaction(self.db):
            materials.pick(self.db, 'P1', 'MIX', 'F', 'C1', 'top', 1)
            materials.pick(self.db, 'P2', 'MIX', 'F', 'C2', 'top', 1)
            isolation.register_composition(self.db, 'supplier-trace', 'MIX',
                                           {self.PROBLEM: 1, 'lot-normal': 1})
            isolation.impose(self.db, self.PROBLEM)
            # Units cannot be distinguished on the spot: stop them where they stand.
            isolation.hold_in_place(self.db, 'IP1', 'F', 'C2', 'top',
                                    'unidentifiable mixed packing')
        with self.assertRaises(Refused), transaction(self.db):
            materials.pick(self.db, 'P5', 'MIX', 'F', 'C2', 'top', 1)
        # Reconciliation must not lift an operational in-place hold, even after proof.
        with transaction(self.db):
            isolation.register_packing(self.db, 'late-original', 'MIX', 'P2', 'lot-normal', 1)
            isolation.reconcile(self.db, self.PROBLEM)
        self.assertTrue(self.db.execute(
            "SELECT 1 FROM material_hold WHERE state='FROZEN' AND group_id='INPLACE:IP1'").fetchone())
        with transaction(self.db):
            isolation.release_in_place(self.db, 'F', 'C2', 'top')
        self.assertFalse(self.db.execute(
            "SELECT 1 FROM material_hold WHERE state='FROZEN' AND group_id='INPLACE:IP1'").fetchone())

    def test_count_only_trace_cannot_hide_behind_most_likely_assignment(self):
        # A separate carton: three unattributed units across three zones, exactly one is B.
        with transaction(self.db):
            materials.split_arrival(
                self.db, 'MIX3', 3, 'supplier-box-MIX3',
                [{'units': 3, 'lot': None, 'document': 'mixed-packing-3'}])
            for pick, car in (('A1', 'C1'), ('A2', 'C2'), ('A3', 'C3')):
                materials.pick(self.db, pick, 'MIX3', 'F', car, 'top', 1)
            isolation.register_composition(self.db, 'trace3', 'MIX3',
                                           {self.PROBLEM: 1, 'lot-normal': 2})
        verdicts = {item['holder']: item['verdict']
                    for item in self._directive()['holders'] if item['carton'] == 'MIX3'}
        # Every zone could be the one; no "most likely" guess may drop a possible carrier.
        self.assertEqual(set(verdicts.values()), {'SUSPECT'})
        self.assertEqual(len(verdicts), 3)
        record = next(item for item in self._directive()['conservation']
                      if item['carton'] == 'MIX3')
        self.assertEqual(record['held'], 3)
        self.assertEqual(sum(record['ledger'].values()), 3)
