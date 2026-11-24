"""Sum circuit evidence for a shift only when all requested circuits are proven."""

from .db import Refused
from .meter import circuit_energy


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
    complete = all(row['state'] == 'MEASURED' for row in result.values())
    return dict(begin=begin, end=end, circuits=result, state='COMPLETE' if complete else 'INCOMPLETE',
                watt_hours=sum(row['watt_hours'] for row in result.values()) if complete else None)
