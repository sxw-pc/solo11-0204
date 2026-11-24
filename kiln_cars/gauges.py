"""Reference loads and scale clearance at the cold weigh bench."""
from .db import insert, one


def register(db, id, cell, tolerance):
    insert(db, 'bench_scale', id=id, cell=cell, tolerance=tolerance)
    return {'scale': id, 'cell': cell}


def check(db, id, scale, minute, standard, display, assessor):
    insert(db, 'bench_check', id=id, scale=scale, minute=minute,
           standard=standard, display=display, assessor=assessor)
    return status(db, scale, minute)


def status(db, scale, minute):
    device = one(db, 'SELECT * FROM bench_scale WHERE id=?', (scale,))
    witness = db.execute('''SELECT * FROM bench_check WHERE scale=? AND minute<=?
                           ORDER BY minute DESC, seq DESC LIMIT 1''', (scale, minute)).fetchone()
    if witness is None:
        return {'scale': scale, 'state': 'UNCHECKED'}
    error = abs(witness['display'] - witness['standard'])
    return {'scale': scale, 'state': 'CLEARED' if error <= device['tolerance'] else 'STOPPED',
            'check_id': witness['id'], 'error': error}
