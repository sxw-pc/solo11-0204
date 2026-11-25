"""Sum circuit evidence for a shift only when a joint trajectory honours every record.

A shift billable energy is a *trajectory*: one cumulative energy choice per sub-meter
segment (face reading plus the whole modulus turns the certified load allows) that also
reproduces every independent master-meter voucher covering part of the shift. Vouchers
overlapping in time are independent equations on the same trajectory, never an
invitation to sum and bill the same sub-meter energy twice.
"""

from itertools import product

from .db import Refused
from .meter import circuit_energy

# Bound the exhaustive search so a statement degrades to "more evidence needed"
# instead of an unbounded enumeration on a meter with many feasible turns.
TRAJECTORY_CAP = 1024


def statement(db, begin, end, circuits):
    if type(begin) is not int or type(end) is not int or not 0 <= begin < end:
        raise Refused('shift must have ordered integer boundaries')
    if not isinstance(circuits, list) or not circuits or any(not isinstance(c, str) for c in circuits):
        raise Refused('supply a nonempty circuit list')
    if len(circuits) != len(set(circuits)):
        raise Refused('circuits must be unique')

    result = {}
    for circuit in sorted(circuits):
        meter = db.execute('SELECT * FROM energy_meter WHERE circuit=?', (circuit,)).fetchone()
        if meter is None:
            raise Refused(f'unknown circuit {circuit}')
        result[circuit] = circuit_energy(db, meter, begin, end)

    vouchers = list(db.execute('''SELECT * FROM energy_master_voucher
                                  WHERE begin_minute>=? AND end_minute<=?
                                  ORDER BY begin_minute,end_minute,id''', (begin, end)))
    conflicts = [name for name, row in result.items() if row['state'] == 'CONFLICT']
    missing = {name: row.get('missing_minutes', []) for name, row in result.items()
               if row['state'] == 'MISSING_BOUNDARY'}

    if conflicts or missing:
        state = 'CONFLICT' if conflicts else 'INCOMPLETE'
        return dict(begin=begin, end=end, circuits=result, state=state, watt_hours=None,
                    reason=(result[conflicts[0]]['reason'] if conflicts else 'MISSING_BOUNDARY_READING'),
                    conflict_circuits=conflicts, missing_boundaries=missing,
                    master_vouchers=[dict(row) for row in vouchers],
                    billing=dict(level='NONE', watt_hours=None, fixed={}, pending=sorted(result),
                                 blocking=[], allocations=[], truncated=False))

    voucher_bounds = sorted({begin, end}
                            | {v['begin_minute'] for v in vouchers}
                            | {v['end_minute'] for v in vouchers})
    missing_minutes = {}
    for circuit, row in result.items():
        known = set(row['minutes'])
        absent = [minute for minute in voucher_bounds if minute not in known]
        if absent:
            missing_minutes[circuit] = absent
    if missing_minutes:
        return _missing_boundary(begin, end, result, vouchers, missing_minutes)

    joint, truncated = _joint_trajectories(result, vouchers)
    if not joint:
        return _no_trajectory(begin, end, result, vouchers, 'NO_JOINT_TRAJECTORY',
                              conflict_circuits=sorted(result))

    # Retain evidence that is determined across every compatible trajectory: annotate
    # each meter segment with the choices surviving the joint constraints, even when
    # the branch shift total itself is still ambiguous. When the exhaustive search was
    # capped, uniqueness claims are unsafe, so only the observed ranges are reported.
    for name, row in result.items():
        for index, segment in enumerate(row['segments']):
            surviving = sorted({
                trajectory['per_segment'][name][index] for trajectory in joint})
            segment['joint_options'] = surviving if not truncated else segment['options']

    totals = sorted({trajectory['total'] for trajectory in joint})
    projections = {circuit: sorted({trajectory['circuits'][circuit] for trajectory in joint})
                   for circuit in result}
    unique_total = len(totals) == 1
    if truncated or any(len(values) > 1 for values in projections.values()):
        if not truncated and unique_total:
            state, level, billable = 'SHIFT_DETERMINED', 'SHIFT', totals[0]
        else:
            state, level, billable = 'PENDING', 'NONE', None
    else:
        state, level, billable = 'COMPLETE', 'BRANCH', totals[0]

    fixed = {} if truncated else {circuit: values[0] for circuit, values in projections.items()
                                  if len(values) == 1}
    pending = sorted(result) if truncated else sorted(
        circuit for circuit, values in projections.items() if len(values) > 1)
    blocking = [] if level == 'BRANCH' else [
        dict(circuit=circuit,
             feasible_watt_hours=(projections[circuit] if not truncated
                                  else sorted({t['total'] for t in result[circuit]['trajectories']})))
        for circuit in pending]
    billing = dict(level=level, watt_hours=billable, fixed=fixed, pending=pending,
                   blocking=blocking,
                   allocations=[{'total': item['total'], 'circuits': item['circuits']}
                                for item in joint[:TRAJECTORY_CAP]],
                   truncated=truncated)
    return dict(begin=begin, end=end, circuits=result, state=state,
                reason=None, conflict_circuits=[], missing_boundaries={},
                watt_hours=billable, feasible_shift_watt_hours=totals,
                master_vouchers=[dict(row) for row in vouchers], billing=billing)


def _segment_energy(per_segment, segment_bounds, left, right):
    """Sum the per-segment energies of one meter between two voucher boundaries."""
    total = 0
    for index, (seg_left, seg_right) in enumerate(segment_bounds):
        if seg_left >= left and seg_right <= right:
            total += per_segment[index]
    return total


def _joint_trajectories(circuit_rows, vouchers):
    """Enumerate sub-meter trajectory combinations satisfying every voucher."""
    names = sorted(circuit_rows)
    per_circuit = {}
    segment_bounds = {}
    for name in names:
        row = circuit_rows[name]
        bounds = list(zip(row['minutes'], row['minutes'][1:]))
        options = [segment['options'] for segment in row['segments']]
        segment_bounds[name] = bounds
        per_circuit[name] = []
        for choice in product(*options):
            per_circuit[name].append(dict(per_segment=choice, total=sum(choice)))

    joint = []
    truncated = False
    for combination in product(*(per_circuit[name] for name in names)):
        chosen = dict(zip(names, combination))
        if all(sum(_segment_energy(chosen[meter]['per_segment'], segment_bounds[meter],
                                   voucher['begin_minute'], voucher['end_minute'])
                   for meter in names) == voucher['watt_hours']
               for voucher in vouchers):
            joint.append(dict(total=sum(item['total'] for item in chosen.values()),
                              circuits={name: chosen[name]['total'] for name in names},
                              per_segment={name: chosen[name]['per_segment'] for name in names}))
            if len(joint) >= TRAJECTORY_CAP:
                truncated = True
                break
    return joint, truncated


def _missing_boundary(begin, end, result, vouchers, missing_minutes):
    pending = sorted(result)
    return dict(begin=begin, end=end, circuits=result, state='INCOMPLETE', watt_hours=None,
                reason='MISSING_BOUNDARY_READING', conflict_circuits=[],
                missing_boundaries=missing_minutes,
                master_vouchers=[dict(row) for row in vouchers],
                billing=dict(level='NONE', watt_hours=None, fixed={}, pending=pending,
                             blocking=[], allocations=[], truncated=False))


def _no_trajectory(begin, end, result, vouchers, reason, *, conflict_circuits):
    pending = sorted(result)
    return dict(begin=begin, end=end, circuits=result, state='CONFLICT', watt_hours=None,
                reason=reason, conflict_circuits=conflict_circuits,
                missing_boundaries={},
                master_vouchers=[dict(row) for row in vouchers],
                billing=dict(level='NONE', watt_hours=None, fixed={}, pending=pending,
                             blocking=[dict(circuit=name,
                                            feasible_watt_hours=sorted(
                                                {t['total'] for t in result[name]['trajectories']}))
                                       for name in pending],
                             allocations=[], truncated=False))
