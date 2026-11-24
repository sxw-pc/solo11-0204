"""Capture immutable cumulative readings from independently metered kiln circuits."""

from .db import Refused, insert, one


def register(db, meter_id, circuit, modulus):
    if not all(isinstance(item, str) and item.strip() for item in (meter_id, circuit)):
        raise Refused('meter and circuit are required')
    if type(modulus) is not int or modulus < 2:
        raise Refused('meter modulus must be an integer greater than one')
    insert(db, 'energy_meter', id=meter_id, circuit=circuit, modulus=modulus)


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


def circuit_energy(db, meter, begin, end):
    rows = list(db.execute('''SELECT * FROM energy_reading
                             WHERE meter_id=? AND minute BETWEEN ? AND ? ORDER BY minute,id''',
                           (meter['id'], begin, end)))
    counts = {}
    for row in rows:
        counts.setdefault(row['minute'], set()).add(row['count'])
    proof = [row['id'] for row in rows]
    if any(len(values) > 1 for values in counts.values()):
        return dict(state='CONFLICT', watt_hours=None, proof=proof)
    if begin not in counts or end not in counts:
        return dict(state='MISSING_BOUNDARY', watt_hours=None, proof=proof)
    measures = [next(iter(values)) for values in counts.values()]
    energy = sum((right - left) % meter['modulus'] for left, right in zip(measures, measures[1:]))
    return dict(state='MEASURED', watt_hours=energy, proof=proof)
