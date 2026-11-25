import sqlite3
import unittest
from contextlib import closing

from kiln_cars import audit, evidence, loading, release, settlement
from kiln_cars.db import Refused, connect, transaction


WINDOW = dict(low=1180, high=1220)


class RefireTest(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        with transaction(self.db):
            self._build_baseline()

    def _build_baseline(self):
        loading.order(self.db, 'O-A', 'Studio A', 1250, 800, True)
        loading.recipe(self.db, 'R-matte', body='stoneware', color='matte-ochre',
                       atmosphere='oxidation', emission='', sensitive_to='', **WINDOW)
        loading.recipe(self.db, 'R-black', body='stoneware', color='carbon-black',
                       atmosphere='reduction', emission='', sensitive_to='', **WINDOW)
        loading.piece(self.db, 'P1', 'O-A', 'R-matte')
        loading.piece(self.db, 'P2', 'O-A', 'R-matte')
        loading.piece(self.db, 'P3', 'O-A', 'R-matte')
        loading.piece(self.db, 'P4', 'O-A', 'R-matte')
        # first firing, one tick per minute
        loading.firing(self.db, 'F1', 'oxidation', 4, 'Logger Team')
        loading.slot(self.db, 'F1', 'C1', 'top', 1200)
        loading.place(self.db, 'P1', 'F1', 'C1', 'top')
        loading.slot(self.db, 'F1', 'C1', 'bottom', 1190)
        loading.place(self.db, 'P2', 'F1', 'C1', 'bottom')
        loading.slot(self.db, 'F1', 'C2', 'top', 1200)
        loading.place(self.db, 'P4', 'F1', 'C2', 'top')
        loading.lock(self.db, 'F1')
        # P1 passes on an independent witness coupon covering the missing minutes
        evidence.capture_trace(self.db, 'F1', 'C1', 'top', 0, 1200, 'top-log')
        evidence.capture_trace(self.db, 'F1', 'C1', 'top', 3, 1200, 'top-log')
        evidence.capture_witness(self.db, 'F1', 'C1', 'top', 'W1', 1, 2, 1190, 1210, 'QA')
        release.assess(self.db, 'P1', 'cosmetic')  # DOWNGRADED via consent
        # P2 also passes thermally on an independent witness coupon covering its
        # missing minutes, then goes to the rework queue on surface condition
        evidence.capture_trace(self.db, 'F1', 'C1', 'bottom', 0, 1200, 'bottom-log')
        evidence.capture_trace(self.db, 'F1', 'C1', 'bottom', 3, 1200, 'bottom-log')
        evidence.capture_witness(self.db, 'F1', 'C1', 'bottom', 'W3', 1, 2, 1190, 1210, 'QA')
        self.assertEqual(evidence.zone_status(self.db, 'P2'), 'WITNESSED')
        release.assess(self.db, 'P2', 'repairable')
        # P4 mirrors P2: coupon-covered first firing, then surface rework
        evidence.capture_trace(self.db, 'F1', 'C2', 'top', 0, 1200, 'c2-log')
        evidence.capture_trace(self.db, 'F1', 'C2', 'top', 3, 1200, 'c2-log')
        evidence.capture_witness(self.db, 'F1', 'C2', 'top', 'W4', 1, 2, 1190, 1210, 'QA')
        release.assess(self.db, 'P4', 'repairable')
        release.add_rework_slot(self.db, 'RW-S1', 'stoneware')
        release.add_rework_slot(self.db, 'RW-S2', 'porcelain')
        # the re-firing batch, still open
        loading.firing(self.db, 'F2', 'oxidation', 4, 'Logger Team')
        loading.slot(self.db, 'F2', 'C9', 'top', 1205)
        loading.slot(self.db, 'F2', 'C9', 'cold', 1100)
        loading.slot(self.db, 'F2', 'C7', 'top', 1205)

    # ---- admission rules -------------------------------------------------

    def test_refire_requires_a_held_compatible_reservation(self):
        with transaction(self.db):
            with self.assertRaisesRegex(Refused, 'held rework reservation'):
                loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            self.assertEqual(release.reserve(self.db, 'P2'), 'RW-S1')
            # porcelain-only slots cannot serve a stoneware body
            self.db.execute("UPDATE rework_slot SET body='porcelain' WHERE id='RW-S1'")
            with self.assertRaisesRegex(Refused, 'body no longer matches'):
                loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            self.db.execute("UPDATE rework_slot SET body='stoneware' WHERE id='RW-S1'")
            result = loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            self.assertEqual(result['state'], 'PLACED')

    def test_only_one_open_refire_per_piece_and_destination_checks_apply(self):
        with transaction(self.db):
            release.reserve(self.db, 'P2')
            # same-load, slot and vacancy checks still run
            loading.slot(self.db, 'F2', 'C8', 'top', 1205)
            loading.place(self.db, 'P3', 'F2', 'C8', 'top')
            with self.assertRaisesRegex(Refused, 'occupied'):
                loading.refire(self.db, 'P2', 'F2', 'C8', 'top')
            with self.assertRaisesRegex(Refused, 'SLOT_OUTSIDE_BODY_WINDOW'):
                loading.refire(self.db, 'P2', 'F2', 'C9', 'cold')
            loading.firing(self.db, 'F3', 'reduction', 4, 'Logger Team')
            loading.slot(self.db, 'F3', 'C1', 'top', 1205)
            with self.assertRaisesRegex(Refused, 'ATMOSPHERE_MISMATCH'):
                loading.refire(self.db, 'P2', 'F3', 'C1', 'top')
            result = loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            self.assertEqual(result['state'], 'PLACED')
            with self.assertRaisesRegex(Refused, 'unfinished refire'):
                loading.refire(self.db, 'P2', 'F2', 'C9', 'cold')

    def test_refire_attempt_before_lock_can_only_be_withdrawn_and_is_not_heat(self):
        with transaction(self.db):
            release.reserve(self.db, 'P2')
            record = loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            with self.assertRaisesRegex(Refused, 'destination batch is locked'):
                loading.finish_refire(self.db, 'P2')
            with self.assertRaisesRegex(Refused, 'unfinished refire'):
                release.assess(self.db, 'P2', 'clean')
            withdrawn = loading.withdraw_refire(self.db, 'P2')
            self.assertEqual(withdrawn['state'], 'WITHDRAWN')
        # the piece is back on its first-firing position, the claim is released
        self.assertEqual(evidence.location_key(evidence.location(self.db, 'P2')),
                         ('F1', 'C1', 'bottom'))
        claim = self.db.execute('SELECT state FROM rework_claim ORDER BY seq DESC').fetchone()
        self.assertEqual(claim['state'], 'RELEASED')
        # the withdrawn attempt never became a heat history
        self.assertEqual([e['firing_id'] for e in release.heat_exposures(self.db, 'P2')], ['F1'])
        with transaction(self.db):
            # and a fresh attempt is now allowed
            self.assertEqual(release.reserve(self.db, 'P2'), 'RW-S1')
            self.assertEqual(loading.refire(self.db, 'P2', 'F2', 'C9', 'top')['state'], 'PLACED')
        loading.lock(self.db, 'F2')
        with transaction(self.db):
            with self.assertRaisesRegex(Refused, 'must be OPEN'):
                loading.withdraw_refire(self.db, 'P2')
            # once locked, the refire can actually finish and establish the new heat
            self.assertEqual(loading.finish_refire(self.db, 'P2')['state'], 'FIRED')

    # ---- end-to-end acceptance and dispatch gates ------------------------

    def test_fired_refire_without_evidence_releases_the_slot_but_blocks_delivery(self):
        with transaction(self.db):
            release.reserve(self.db, 'P2')
            loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            loading.lock(self.db, 'F2')
            fired = loading.finish_refire(self.db, 'P2')
        self.assertEqual(fired['state'], 'FIRED')
        self.assertEqual(fired['thermal'], 'EVIDENCE_GAP')
        # the rework position is released even though the new evidence is incomplete
        self.assertEqual(self.db.execute(
            "SELECT state FROM rework_claim WHERE seq=?", (fired['refire'],)).fetchone()[0], 'USED')
        self.assertEqual(evidence.location_key(evidence.location(self.db, 'P2')),
                         ('F2', 'C9', 'top'))
        # the inspector can trace every heat exposure and the evidence behind it
        history = release.heat_exposures(self.db, 'P2')
        self.assertEqual([(e['heat_no'], e['firing_id'], e['thermal']) for e in history],
                         [(1, 'F1', 'WITNESSED'), (2, 'F2', 'EVIDENCE_GAP')])
        with transaction(self.db):
            self.assertEqual(release.assess(self.db, 'P2', 'clean')['decision'], 'HOLD')
            with self.assertRaisesRegex(Refused, 'piece is not deliverable'):
                settlement.dispatch(self.db, 'D-A1', 'O-A', ['P2'])

    def test_complete_refire_evidence_allows_delivery_and_first_firing_cannot_fill_gaps(self):
        self._send_through_refire()
        with transaction(self.db):
            # evidence at the first firing never covers the new kiln position
            evidence.capture_trace(self.db, 'F2', 'C9', 'top', 0, 1200, 'new-log')
            evidence.capture_witness(self.db, 'F2', 'C9', 'top', 'W2', 1, 2, 1190, 1210, 'QA')
            # one minute still open -> still not deliverable
            self.assertEqual(evidence.zone_status(self.db, 'P2'), 'EVIDENCE_GAP')
            evidence.capture_trace(self.db, 'F2', 'C9', 'top', 3, 1200, 'new-log')
            self.assertEqual(evidence.zone_status(self.db, 'P2'), 'WITNESSED')
            self.assertEqual(release.assess(self.db, 'P2', 'clean')['decision'], 'STANDARD')
            manifest = settlement.dispatch(self.db, 'D-A1', 'O-A', ['P2'])
        self.assertEqual(manifest['total_cents'], 1250)
        self.assertEqual(settlement.balance(self.db, 'O-A')['unshipped'], ['P1', 'P3'])

    def test_late_first_firing_excursion_blocks_posting_before_and_after_new_evidence(self):
        # before any excursion, both pieces can actually post under their current verdicts
        with transaction(self.db):
            settlement.dispatch(self.db, 'D-OK1', 'O-A', ['P1'])
        self._send_through_refire()
        with transaction(self.db):
            for tick in range(4):
                evidence.capture_trace(self.db, 'F2', 'C9', 'top', tick, 1200, 'new-log')
            self.assertEqual(release.assess(self.db, 'P2', 'clean')['decision'], 'STANDARD')
            settlement.dispatch(self.db, 'D-OK2', 'O-A', ['P2'])
        # the missing first-firing minute now arrives with an out-of-window measurement
        with transaction(self.db):
            evidence.capture_trace(self.db, 'F1', 'C1', 'top', 2, 1240, 'recovered-logger')
            history = release.heat_exposures(self.db, 'P1')
            self.assertEqual([e['thermal'] for e in history], ['OUTSIDE_WINDOW'])
            # a delivered piece keeps its frozen decision; reassessment is refused ...
            with self.assertRaisesRegex(Refused, 'frozen decision'):
                release.assess(self.db, 'P1', 'clean')
        # ... but any fresh posting is blocked: the late evidence first makes the
        # delivered assessment stale, exactly as under the legacy rules
        with self.assertRaisesRegex(Refused, 'stale'), transaction(self.db):
            settlement.dispatch(self.db, 'D-RISK1', 'O-A', ['P1'])
        # P1 was never refired: its own position now fails outright
        self.assertEqual(evidence.zone_status(self.db, 'P1'), 'OUTSIDE_WINDOW')
        with self.assertRaisesRegex(Refused, 'stale'), transaction(self.db):
            settlement.dispatch(self.db, 'D-RISK2', 'O-A', ['P1'])
        # the same irreversible gate applies to an undelivered refired piece whose earlier
        # heat overshot, even though its refire position still passes the temperature window
        with transaction(self.db):
            self.assertEqual(release.reserve(self.db, 'P4'), 'RW-S1')
        with transaction(self.db):
            # RW-S1 was released by P2's finished refire, but the claim row still references
            # the slot; a second slot keeps both reservations independent
            pass
        with transaction(self.db):
            loading.refire(self.db, 'P4', 'F2', 'C7', 'top')
            loading.finish_refire(self.db, 'P4')
            for tick in range(4):
                evidence.capture_trace(self.db, 'F2', 'C7', 'top', tick, 1200, 'p4-new-log')
            self.assertEqual(evidence.zone_status(self.db, 'P4'), 'LOGGER_COMPLETE')
            evidence.capture_trace(self.db, 'F1', 'C2', 'top', 2, 1240, 'recovered-logger')
            self.assertEqual([e['thermal'] for e in release.heat_exposures(self.db, 'P4')],
                             ['OUTSIDE_WINDOW', 'LOGGER_COMPLETE'])
            self.assertEqual(release.assess(self.db, 'P4', 'clean')['decision'], 'HOLD')
        with self.assertRaisesRegex(Refused, 'historical heat risk'), transaction(self.db):
            settlement.dispatch(self.db, 'D-RISK3', 'O-A', ['P4'])
        # the original postings and their acceptance rows remain on the books, while audit
        # surfaces which firing and which position carries the new measured heat risk
        result = audit.recompute(self.db)
        self.assertEqual(result['status'], 'AUDIT_OK')
        p1_late = [row for row in result['late_evidence'] if row['piece'] == 'P1']
        self.assertTrue(any(row['current_thermal'] == 'OUTSIDE_WINDOW' for row in p1_late))
        self.assertTrue(any(row.get('firing_id') == 'F1' for row in p1_late))
        p2_late = [row for row in result['late_evidence'] if row['piece'] == 'P2']
        self.assertTrue(any(row.get('zone') == 'bottom' and
                            row['current_thermal'] == 'OUTSIDE_WINDOW' for row in p2_late))

    def test_pieces_without_refire_keep_the_legacy_dispatch_rules(self):
        with transaction(self.db):
            # P1 was downgraded on a coupon and is deliverable under the old rule
            self.assertEqual(release.latest(self.db, 'P1')['decision'], 'DOWNGRADED')
            manifest = settlement.dispatch(self.db, 'D-LEGACY', 'O-A', ['P1'])
        self.assertEqual(manifest['total_cents'], 800)
        # a later excursion freezes nothing that was already posted, but is audited
        with transaction(self.db):
            evidence.capture_trace(self.db, 'F1', 'C1', 'top', 2, 1240, 'recovered-logger')
        result = audit.recompute(self.db)
        self.assertEqual(result['status'], 'AUDIT_OK')
        self.assertIn({'piece': 'P1', 'current_thermal': 'OUTSIDE_WINDOW'},
                      result['late_evidence'])

    def test_audit_replays_each_assessment_at_its_recorded_position(self):
        self._send_through_refire()
        with transaction(self.db):
            for tick in range(4):
                evidence.capture_trace(self.db, 'F2', 'C9', 'top', tick, 1200, 'new-log')
            release.assess(self.db, 'P2', 'clean')
        rows = self.db.execute('SELECT seq, firing_id, car, zone, decision FROM assessment '
                               'WHERE piece_id=? ORDER BY seq', ('P2',)).fetchall()
        self.assertEqual([(r['firing_id'], r['zone'], r['decision']) for r in rows],
                         [('F1', 'bottom', 'REWORK_QUEUE'), ('F2', 'top', 'STANDARD')])
        result = audit.recompute(self.db)
        self.assertEqual(result['status'], 'AUDIT_OK')
        refire = self.db.execute('SELECT * FROM refire').fetchone()
        self.assertEqual((refire['state'], refire['firing_id'], refire['car'], refire['zone']),
                         ('FIRED', 'F2', 'C9', 'top'))

    # ---- helpers ---------------------------------------------------------

    def _send_through_refire(self):
        with transaction(self.db):
            release.reserve(self.db, 'P2')
            loading.refire(self.db, 'P2', 'F2', 'C9', 'top')
            loading.lock(self.db, 'F2')
            loading.finish_refire(self.db, 'P2')


if __name__ == '__main__':
    unittest.main()
