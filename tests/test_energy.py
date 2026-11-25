import unittest
from contextlib import closing
from pathlib import Path

from kiln_cars.__main__ import run_fixture, apply
from kiln_cars.db import Refused, connect, transaction
from kiln_cars import energy, meter


class ShiftEnergy(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(connect(':memory:')))
        fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'energy.json'
        self.steps = {item['step']: item['result'] for item in run_fixture(self.db, fixture)}

    def _register_loads(self, watts):
        with transaction(self.db):
            meter.register_load(self.db, 'M1', watts, 'load-cert-M1')
            meter.register_load(self.db, 'M2', watts, 'load-cert-M2')

    def _statement(self):
        with transaction(self.db):
            return energy.statement(self.db, 10, 20, ['heat', 'fan'])

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

    def test_legacy_query_unchanged_without_load_or_master_evidence(self):
        report = self._statement()
        self.assertEqual(report['state'], 'COMPLETE')
        self.assertEqual(report['watt_hours'], 15)
        self.assertEqual(report['billing']['level'], 'BRANCH')
        self.assertEqual(report['circuits']['heat']['trajectories'],
                         [{'total': 8, 'per_segment': [8]}])
        self.assertFalse(report['circuits']['heat']['has_load'])

    def test_load_certificates_open_turn_choices_but_shift_stays_pending(self):
        self._register_loads(900)
        report = self._statement()
        self.assertEqual(report['state'], 'PENDING')
        self.assertIsNone(report['watt_hours'])
        self.assertEqual(report['billing']['level'], 'NONE')
        self.assertEqual(
            sorted(t['total'] for t in report['circuits']['heat']['trajectories']),
            [8, 108])
        self.assertEqual(
            sorted(t['total'] for t in report['circuits']['fan']['trajectories']),
            [7, 107])
        self.assertEqual(report['feasible_shift_watt_hours'], [15, 115, 215])

    def test_master_voucher_below_any_split_is_evidence_conflict(self):
        self._register_loads(900)
        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-68', 10, 20, 68, 'master-card-68')
        report = self._statement()
        self.assertEqual(report['state'], 'CONFLICT')
        self.assertEqual(report['reason'], 'NO_JOINT_TRAJECTORY')
        self.assertIsNone(report['watt_hours'])
        self.assertEqual(report['billing']['level'], 'NONE')
        self.assertEqual(report['billing']['allocations'], [])
        # conflict is distinct from an absent boundary: every face reading exists
        self.assertEqual(report['missing_boundaries'], {})
        self.assertEqual([v['id'] for v in report['master_vouchers']], ['MV-68'])

    def test_master_voucher_pins_shift_total_while_branches_still_split(self):
        self._register_loads(900)
        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-115', 10, 20, 115, 'master-card-115')
        report = self._statement()
        self.assertEqual(report['state'], 'SHIFT_DETERMINED')
        self.assertEqual(report['watt_hours'], 115)
        self.assertEqual(report['billing']['level'], 'SHIFT')
        allocations = report['billing']['allocations']
        self.assertEqual(sorted(sorted(a['circuits'].values()) for a in allocations),
                         [[7, 108], [8, 107]])
        self.assertTrue(all(a['total'] == 115 for a in allocations))
        blocking = {item['circuit']: item['feasible_watt_hours']
                    for item in report['billing']['blocking']}
        self.assertEqual(blocking, {'fan': [7, 107], 'heat': [8, 108]})
        self.assertEqual(sorted(report['billing']['pending']), ['fan', 'heat'])
        self.assertEqual(report['billing']['fixed'], {})

    def test_overlapping_vouchers_are_joint_constraints_not_double_counting(self):
        # Interior readings split the shift at minute 15; 1500 W permits at most one
        # extra turn per half (capacity 125 Wh) and none beyond that.
        self._register_loads(1500)
        with transaction(self.db):
            meter.capture(self.db, 'H-mid', 'M1', 15, 50, 'card-H-mid')
            meter.capture(self.db, 'F-mid', 'M2', 15, 22, 'card-F-mid')
        # heat halves: (97->50)=53 or 153; (50->5)=55 (155 exceeds the 125 Wh cap)
        # fan halves:  (20->22)=2 or 102; (22->27)=5 or 105

        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-FIRST', 10, 15, 55, 'master-first')
        first = self._statement()
        # the first half is pinned to the rollover-free split; the open second half
        # still leaves two distinct shift totals, so nothing is billable yet
        self.assertEqual(first['state'], 'PENDING')
        self.assertEqual(first['billing']['level'], 'NONE')
        self.assertEqual(first['feasible_shift_watt_hours'], [115, 215])
        # the pinned first half shows up as single joint options on each segment;
        # heat's second-half turn (155 Wh) exceeds the 125 Wh half-shift cap, so heat
        # is fully determined while only the fan second half stays open
        self.assertEqual(first['circuits']['heat']['segments'][0]['joint_options'], [53])
        self.assertEqual(first['circuits']['heat']['segments'][1]['joint_options'], [55])
        self.assertEqual(first['circuits']['fan']['segments'][0]['joint_options'], [2])
        self.assertEqual(
            sorted(first['circuits']['fan']['segments'][1]['joint_options']), [5, 105])
        self.assertEqual(first['billing']['fixed'], {'heat': 108})
        self.assertEqual(first['billing']['pending'], ['fan'])

        # A second voucher overlapping the first. On its own 315 Wh is feasible
        # (heat 108 + fan 207, i.e. the fan takes its one permitted turn in each
        # half), but with the first half pinned to 55 Wh every compatible trajectory
        # totals at most 215 Wh, so no joint trajectory exists. The overlap is a
        # second equation, never energy to sum and bill twice.
        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-CLASH', 10, 20, 315, 'master-clash')
        clash = self._statement()
        self.assertEqual(clash['state'], 'CONFLICT')
        self.assertEqual(clash['reason'], 'NO_JOINT_TRAJECTORY')
        self.assertEqual(clash['billing']['level'], 'NONE')
        self.assertEqual(clash['billing']['allocations'], [])
        self.assertEqual({v['id'] for v in clash['master_vouchers']},
                         {'MV-FIRST', 'MV-CLASH'})
        # prove the clashing voucher alone would have been admissible
        solo_db = self.enterContext(closing(connect(':memory:')))
        run_fixture(solo_db, Path(__file__).resolve().parents[1] / 'fixtures' / 'energy.json')
        with transaction(solo_db):
            meter.register_load(solo_db, 'M1', 1500, 'c')
            meter.register_load(solo_db, 'M2', 1500, 'c')
            meter.capture(solo_db, 'H-mid', 'M1', 15, 50, 'card-H-mid')
            meter.capture(solo_db, 'F-mid', 'M2', 15, 22, 'card-F-mid')
            meter.capture_master_voucher(solo_db, 'SOLO', 10, 20, 315, 'master-solo')
        solo = energy.statement(solo_db, 10, 20, ['heat', 'fan'])
        self.assertEqual(solo['state'], 'COMPLETE')
        self.assertEqual(solo['watt_hours'], 315)
        self.assertEqual(solo['billing']['allocations'],
                         [{'total': 315, 'circuits': {'fan': 207, 'heat': 108}}])
        # a further voucher cannot reconcile two contradictory source documents
        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-EXTRA', 10, 20, 115, 'master-extra')
        still_clash = self._statement()
        self.assertEqual(still_clash['state'], 'CONFLICT')
        self.assertEqual(still_clash['reason'], 'NO_JOINT_TRAJECTORY')
        # the clashing receipt is immutable, so a corrected figure needs a new voucher
        with self.assertRaises(Refused), transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-CLASH', 10, 20, 114, 'master-clash-redo')

    def test_consistent_overlapping_vouchers_adjust_the_split_and_reissue_bill(self):
        # Same interior readings; here both vouchers are chosen so a joint trajectory
        # exists and the second one tightens what the first half alone left open.
        self._register_loads(1500)
        with transaction(self.db):
            meter.capture(self.db, 'H-mid', 'M1', 15, 50, 'card-H-mid')
            meter.capture(self.db, 'F-mid', 'M2', 15, 22, 'card-F-mid')
            meter.capture_master_voucher(self.db, 'MV-FIRST', 10, 15, 155, 'master-first')
        first = self._statement()
        # first half pinned to one-turn fan (53+102=155); the second half is still
        # open. Heat stays fixed at 108 Wh across every compatible trajectory.
        self.assertEqual(first['billing']['fixed'], {'heat': 108})
        self.assertEqual(sorted(first['feasible_shift_watt_hours']), [215, 315])
        self.assertEqual(first['circuits']['fan']['segments'][0]['joint_options'], [102])
        self.assertEqual(sorted(first['circuits']['fan']['segments'][1]['joint_options']),
                         [5, 105])

        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-FULL', 10, 20, 215, 'master-full')
        aligned = self._statement()
        # the second overlapping voucher removes the last ambiguity: the shift can now
        # be issued down to branch amounts, with one surviving allocation
        self.assertEqual(aligned['state'], 'COMPLETE')
        self.assertEqual(aligned['watt_hours'], 215)
        self.assertEqual(aligned['billing']['level'], 'BRANCH')
        self.assertEqual(aligned['billing']['pending'], [])
        self.assertEqual(aligned['billing']['allocations'],
                         [{'total': 215, 'circuits': {'fan': 107, 'heat': 108}}])
        self.assertEqual(aligned['billing']['fixed'], {'fan': 107, 'heat': 108})
        # the newly admitted voucher changes the surviving per-segment choice
        self.assertEqual(aligned['circuits']['fan']['segments'][1]['joint_options'], [5])

    def test_uniquely_proven_segment_evidence_survives_open_branch_totals(self):
        self._register_loads(1500)
        with transaction(self.db):
            meter.capture(self.db, 'H-mid', 'M1', 15, 50, 'card-H-mid')
            meter.capture(self.db, 'F-mid', 'M2', 15, 22, 'card-F-mid')
            # pin the first half; second half still carries the turn ambiguity
            meter.capture_master_voucher(self.db, 'MV-FIRST', 10, 15, 55, 'master-first')
        report = self._statement()
        self.assertEqual(report['state'], 'PENDING')
        self.assertEqual(report['billing']['level'], 'NONE')
        # every compatible trajectory keeps the same first-half branch energies:
        # that determined evidence is retained as single joint options even though
        # the shift total is not yet billable
        heat_segments = report['circuits']['heat']['segments']
        fan_segments = report['circuits']['fan']['segments']
        self.assertEqual(heat_segments[0]['joint_options'], [53])
        self.assertEqual(heat_segments[1]['joint_options'], [55])
        self.assertEqual(fan_segments[0]['joint_options'], [2])
        self.assertEqual(sorted(fan_segments[1]['joint_options']), [5, 105])
        # heat is fully proven; only the fan keeps an allocation open
        self.assertEqual(report['billing']['fixed'], {'heat': 108})
        self.assertEqual(sorted(
            {a['circuits']['fan'] for a in report['billing']['allocations']}),
            [7, 107])

    def test_missing_boundary_is_distinct_from_conflict(self):
        self._register_loads(900)
        with transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-SHORT', 12, 18, 30, 'master-short')
        report = self._statement()
        self.assertEqual(report['state'], 'INCOMPLETE')
        self.assertEqual(report['reason'], 'MISSING_BOUNDARY_READING')
        self.assertEqual(report['missing_boundaries'],
                         {'fan': [12, 18], 'heat': [12, 18]})
        self.assertIsNone(report['watt_hours'])

    def test_load_and_voucher_receipts_are_idempotent_and_immutable(self):
        with transaction(self.db):
            first = meter.register_load(self.db, 'M1', 900, 'load-cert-M1')
            self.assertEqual(meter.register_load(self.db, 'M1', 900, 'load-cert-M1'), first)
            voucher = meter.capture_master_voucher(self.db, 'MV-1', 10, 20, 115, 'master-card')
            self.assertEqual(
                meter.capture_master_voucher(self.db, 'MV-1', 10, 20, 115, 'master-card'),
                voucher)
        with self.assertRaises(Refused), transaction(self.db):
            meter.register_load(self.db, 'M1', 950, 'load-cert-M1')
        with self.assertRaises(Refused), transaction(self.db):
            meter.capture_master_voucher(self.db, 'MV-1', 10, 20, 116, 'master-card')

    def test_load_cap_below_face_difference_is_evidence_conflict(self):
        with transaction(self.db):
            # 30 W over ten minutes gives a 5 Wh ceiling, below both face differences
            meter.register_load(self.db, 'M1', 30, 'too-small-cert')
            meter.register_load(self.db, 'M2', 30, 'too-small-cert')
        report = self._statement()
        # this is an evidence conflict (certified cap rules out every trajectory),
        # not a missing boundary
        self.assertEqual(report['state'], 'CONFLICT')
        self.assertEqual(report['reason'], 'LOAD_CAP_EXCEEDED')
        self.assertEqual(report['missing_boundaries'], {})
        self.assertIsNone(report['watt_hours'])
        self.assertEqual(report['billing']['level'], 'NONE')
        self.assertEqual(
            sorted(report['conflict_circuits']), ['fan', 'heat'])

    def test_truncated_enumeration_never_claims_a_unique_bill(self):
        big = self.enterContext(closing(connect(':memory:')))
        with transaction(big):
            meter.register(big, 'A', 'heat', 10)
            meter.register(big, 'B', 'fan', 10)
            # huge certified load admits far more than the 1024 trajectory cap
            meter.register_load(big, 'A', 100000, 'cert')
            meter.register_load(big, 'B', 100000, 'cert')
            meter.capture(big, 'a1', 'A', 0, 1, 'd')
            meter.capture(big, 'a2', 'A', 60, 2, 'd')
            meter.capture(big, 'b1', 'B', 0, 3, 'd')
            meter.capture(big, 'b2', 'B', 60, 4, 'd')
        report = energy.statement(big, 0, 60, ['heat', 'fan'])
        self.assertTrue(report['billing']['truncated'])
        self.assertEqual(len(report['billing']['allocations']), 1024)
        # the partial enumeration cannot support uniqueness or billable claims
        self.assertEqual(report['state'], 'PENDING')
        self.assertEqual(report['billing']['level'], 'NONE')
        self.assertIsNone(report['watt_hours'])
        self.assertEqual(report['billing']['fixed'], {})
        self.assertEqual(sorted(report['billing']['pending']), ['fan', 'heat'])
