import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from kiln_cars import audit, evidence, loading, release, settlement
from kiln_cars.__main__ import run_fixture
from kiln_cars.db import Refused, connect, transaction


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'shared.json'


class SharedFiringTest(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        self.result = {row['step']: row['result'] for row in run_fixture(self.db, FIXTURE)}

    def test_admission_has_three_different_denials(self):
        for label, reason in [('airborne-color-conflict', 'CROSS_LOAD_COLOR_CONFLICT'),
                              ('atmosphere-conflict', 'ATMOSPHERE_MISMATCH'),
                              ('body-window-conflict', 'SLOT_OUTSIDE_BODY_WINDOW')]:
            self.assertEqual(self.result[label]['refused'], reason)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM placement').fetchone()[0], 4)
        self.assertIn('OPEN', self.result['locked-unload-refused']['refused'])

    def test_open_occupancy_can_be_reassigned(self):
        with transaction(self.db):
            loading.firing(self.db, 'OPEN-BATCH', 'oxidation', 2, 'operator')
            loading.slot(self.db, 'OPEN-BATCH', 'C4', 'middle', 1200)
            self.assertEqual(loading.place(self.db, 'P5', 'OPEN-BATCH', 'C4', 'middle'), 'COMPATIBLE')
            self.assertEqual(loading.unload(self.db, 'P5'), 'UNLOADED')
            with self.assertRaises(Refused):
                loading.lock(self.db, 'OPEN-BATCH')

    def test_independent_gap_scope_and_late_evidence(self):
        self.assertEqual(self.result['neighbor-cannot-cover-gap'], 'EVIDENCE_GAP')
        self.assertIn('independent', self.result['same-team-coupon']['refused'])
        self.assertIn('conflicting', self.result['same-tick-conflict']['refused'])
        self.assertEqual(self.result['uncovered-interval']['decision'], 'HOLD')
        self.assertEqual(self.result['consented-downgrade']['decision'], 'DOWNGRADED')
        self.assertEqual([(item['piece_id'], item['cents']) for item in self.result['partial-consignment']['items']],
                         [('P1', 1250), ('P2', 800)])
        self.assertEqual(self.result['late-current-zone-rejected'], 'OUTSIDE_WINDOW')
        self.assertEqual(evidence.zone_status(self.db, 'P1'), 'LOGGER_COMPLETE')

    def test_rework_entitlement_and_capacity(self):
        self.assertEqual(self.result['no-consent-repair']['decision'], 'REWORK_QUEUE')
        self.assertIn('no compatible free', self.result['capacity-contended']['refused'])
        self.assertEqual(self.result['first-claim'], self.result['capacity-reallocated'])
        states = [r[0] for r in self.db.execute('SELECT state FROM rework_claim ORDER BY seq')]
        self.assertEqual(states, ['USED', 'RELEASED'])
        self.assertEqual(self.result['repaired-and-reinspected']['decision'], 'STANDARD')

    def test_mixed_dispatch_rolls_back_and_retry_is_exact(self):
        self.assertIsNone(self.db.execute("SELECT 1 FROM dispatch WHERE id='D-MIX'").fetchone())
        with transaction(self.db):
            self.assertEqual(settlement.dispatch(self.db, 'D-A1', 'O-A', ['P2', 'P1']),
                             self.result['partial-consignment'])
            with self.assertRaisesRegex(Refused, 'another manifest'):
                settlement.dispatch(self.db, 'D-A1', 'O-A', ['P1'])
        with self.assertRaises(sqlite3.IntegrityError), transaction(self.db):
            settlement.dispatch(self.db, 'D-REPOST', 'O-A', ['P1'])
        self.assertEqual(self.result['partial-order-balance']['unshipped'], ['P3'])
        self.assertEqual(settlement.balance(self.db, 'O-A')['settled_cents'], 3300)

    def test_audit_replays_old_acceptance_and_flags_late_excursion(self):
        result = audit.recompute(self.db)
        self.assertEqual(result['status'], 'AUDIT_OK')
        self.assertEqual(result['late_evidence'], [{'piece': 'P2', 'current_thermal': 'OUTSIDE_WINDOW'}])
        self.assertEqual([r['total_cents'] for r in result['manifests']], [2050, 1250])
        self.db.execute("UPDATE settlement SET cents=1 WHERE piece_id='P2'")
        self.assertEqual(audit.recompute(self.db)['status'], 'AUDIT_MISMATCH')

    def test_late_zone_evidence_blocks_only_fresh_affected_dispatch(self):
        with transaction(self.db):
            with self.assertRaisesRegex(Refused, 'repair work must be recorded'):
                release.assess(self.db, 'P4', 'clean')
            release.assess(self.db, 'P4', 'repairable')
            release.reserve(self.db, 'P4')
            release.finish(self.db, 'P4', True)
            release.assess(self.db, 'P4', 'clean')
            evidence.capture_witness(self.db, 'F1', 'C2', 'bottom', 'W4', 0, 0, 1189, 1204, 'Inspector C')
        with self.assertRaisesRegex(Refused, 'stale'), transaction(self.db):
            settlement.dispatch(self.db, 'D-B1', 'O-B', ['P4'])
        with transaction(self.db):
            release.assess(self.db, 'P4', 'clean')
            self.assertEqual(settlement.dispatch(self.db, 'D-B1', 'O-B', ['P4'])['total_cents'], 1250)

    def test_stale_rework_requires_release_and_thermal_reassessment(self):
        with transaction(self.db):
            release.assess(self.db, 'P4', 'repairable')
            release.reserve(self.db, 'P4')
            evidence.capture_witness(self.db, 'F1', 'C2', 'bottom', 'W4', 0, 1, 1190, 1204, 'Inspector C')
            with self.assertRaisesRegex(Refused, 'new evidence'):
                release.finish(self.db, 'P4', True)
            self.assertEqual(release.finish(self.db, 'P4', False), 'RELEASED')
            self.assertEqual(release.assess(self.db, 'P4', 'repairable')['decision'], 'REWORK_QUEUE')
            self.assertEqual(release.reserve(self.db, 'P4'), 'RW-S1')


