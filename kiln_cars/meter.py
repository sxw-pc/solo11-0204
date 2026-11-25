"""Capture immutable cumulative readings from independently metered kiln circuits.

Readings only record what the meter face showed in a given minute (a value below the
modulus). Recovering the true cumulative energy between two readings requires choosing
how many whole modulus turns happened in between. A registered load limit bounds the
turns allowed over any interval; without load evidence the legacy single-rollover
convention is still reported.
"""

from .db import Refused, insert, one


def register(db, meter_id, circuit, modulus):
    if not all(isinstance(item, str) and item.strip() for item in (meter_id, circuit)):
        raise Refused('meter and circuit are required')
    if type(modulus) is not int or modulus < 2:
        raise Refused('meter modulus must be an integer greater than one')
    insert(db, 'energy_meter', id=meter_id, circuit=circuit, modulus=modulus)


def register_load(db, meter_id, max_watts, document):
    """Register the certified continuous load cap (watts) for one meter.

    The cap is independently sourced evidence: the same receipt may be re-sent, but a
    different payload under the same meter is refused.
    """
    one(db, 'SELECT * FROM energy_meter WHERE id=?', (meter_id,))
    if type(max_watts) is not int or max_watts <= 0:
        raise Refused('load limit must be a positive integer wattage')
    if not isinstance(document, str) or not document.strip():
        raise Refused('load document is required')
    fields = dict(meter_id=meter_id, max_watts=max_watts, document=document)
    old = db.execute('SELECT * FROM energy_meter_load WHERE meter_id=?', (meter_id,)).fetchone()
    if old:
        if any(old[key] != value for key, value in fields.items()):
            raise Refused('meter already carries another load certificate')
        return dict(old)
    insert(db, 'energy_meter_load', **fields)
    return fields


def capture(db, id, meter_id, minute, count, document):
    meter = one(db, 'SELECT * FROM energy_meter WHERE id=?', (meter_id,))
    if not all(isinstance(item, str) and item.strip() for item in (id, document)):
        raise Refused('receipt and source document are required')
    if type(minute) is not int or minute < 0 or type(count) is not int or not 0 <= count < meter['modulus']:
        raise Refused('reading is outside the meter or shift range')
    fields = dict(id=id, meter_id=meter_id, minute=minute, count=count, document=document)
    old = db.execute('SELECT * FROM energy_reading WHERE id=?', (id,)).fetchone()
    if old:
        if any(old[key] != value for key, value in fields.items()):
            raise Refused('reading ID already has another payload')
        return dict(old)
    insert(db, 'energy_reading', **fields)
    return fields


def capture_master_voucher(db, id, begin_minute, end_minute, watt_hours, document):
    """Capture an independent master-meter voucher for a closed minute interval.

    Vouchers are source documents, not derived allocations: re-sending the same receipt
    is idempotent and a changed payload is refused. Overlapping vouchers are accepted at
    capture time; joint feasibility is decided when a shift statement is queried.
    """
    if not isinstance(id, str) or not id.strip() or not isinstance(document, str) or not document.strip():
        raise Refused('voucher and source document are required')
    if (type(begin_minute) is not int or type(end_minute) is not int
            or not 0 <= begin_minute < end_minute):
        raise Refused('voucher must have ordered integer minute boundaries')
    if type(watt_hours) is not int or watt_hours < 0:
        raise Refused('voucher energy must be a nonnegative integer watt-hour value')
    fields = dict(id=id, begin_minute=begin_minute, end_minute=end_minute,
                  watt_hours=watt_hours, document=document)
    old = db.execute('SELECT * FROM energy_master_voucher WHERE id=?', (id,)).fetchone()
    if old:
        if any(old[key] != value for key, value in fields.items()):
            raise Refused('voucher ID already has another payload')
        return dict(old)
    insert(db, 'energy_master_voucher', **fields)
    return fields


def _load_row(db, meter_id):
    return db.execute('SELECT * FROM energy_meter_load WHERE meter_id=?', (meter_id,)).fetchone()


def _distinct_face_counts(db, meter, begin, end):
    """Return per-minute face-count sets and the proof receipt ids for the window."""
    rows = list(db.execute('''SELECT * FROM energy_reading
                             WHERE meter_id=? AND minute BETWEEN ? AND ? ORDER BY minute,id''',
                           (meter['id'], begin, end)))
    by_minute = {}
    for row in rows:
        by_minute.setdefault(row['minute'], set()).add(row['count'])
    return by_minute, [row['id'] for row in rows]


def segment_options(modulus, left, right, minutes, load_row):
    """Every cumulative energy that matches two face readings over one interval.

    The face only pins the residue, so the cumulative energy is remainder plus a whole
    number of modulus turns. With no registered load, the legacy single-rollover
    convention reports the bare residue as the one option; with a load certificate,
    every turn count whose implied average draw stays within the certified cap is
    admissible, from zero turns up to the cap.
    """
    remainder = (right - left) % modulus
    if load_row is None:
        return [remainder]
    capacity = (load_row['max_watts'] * minutes) // 60
    return [remainder + turns * modulus for turns in range(capacity // modulus + 1)
            if remainder + turns * modulus <= capacity]


def circuit_energy(db, meter, begin, end):
    by_minute, proof = _distinct_face_counts(db, meter, begin, end)
    if any(len(values) > 1 for values in by_minute.values()):
        return dict(state='CONFLICT', reason='CONFLICTING_FACE_READINGS',
                    watt_hours=None, proof=proof, segments=[], trajectories=[],
                    minutes=sorted(by_minute), has_load=_load_row(db, meter['id']) is not None,
                    minute_values={minute: sorted(values) for minute, values in by_minute.items()
                                   if len(values) > 1})
    if begin not in by_minute or end not in by_minute:
        missing = [minute for minute in (begin, end) if minute not in by_minute]
        return dict(state='MISSING_BOUNDARY', reason='MISSING_BOUNDARY_READING',
                    watt_hours=None, proof=proof, segments=[], trajectories=[],
                    minutes=sorted(by_minute), has_load=_load_row(db, meter['id']) is not None,
                    missing_minutes=missing)

    ordered = sorted(by_minute)
    face = {minute: next(iter(values)) for minute, values in by_minute.items()}
    load_row = _load_row(db, meter['id'])
    modulus = meter['modulus']
    segments = []
    for left_minute, right_minute in zip(ordered, ordered[1:]):
        options = segment_options(modulus, face[left_minute], face[right_minute],
                                  right_minute - left_minute, load_row)
        if not options:
            return dict(state='CONFLICT', reason='LOAD_CAP_EXCEEDED', watt_hours=None,
                        proof=proof, segments=segments, trajectories=[], minutes=ordered,
                        has_load=True, minute_values={},
                        segment=(left_minute, right_minute))
        segments.append(dict(begin=left_minute, end=right_minute, options=options))

    legacy_energy = sum((face[right] - face[left]) % modulus
                        for left, right in zip(ordered, ordered[1:]))
    trajectories = [dict(total=total, per_segment=per_segment)
                    for total, per_segment in _cumulatives(segments)]
    return dict(state='MEASURED', reason=None, watt_hours=legacy_energy, proof=proof,
                minutes=ordered, face=face, has_load=load_row is not None,
                segments=segments, trajectories=trajectories)


def _cumulatives(segments):
    """Yield (total energy, per-segment energy list) for every combination of options."""
    chosen = []

    def walk(index, total):
        if index == len(segments):
            yield total, list(chosen)
            return
        for option in segments[index]['options']:
            chosen.append(option)
            yield from walk(index + 1, total + option)
            chosen.pop()

    yield from walk(0, 0)
