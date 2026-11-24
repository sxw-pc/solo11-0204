"""Record net weights against a cleared bench scale and its reference receipt."""
from .db import Refused, insert, one
from .gauges import status


def record(db, id, scale, minute, item, tare, gross):
    clearance = status(db, scale, minute)
    if clearance['state'] != 'CLEARED':
        raise Refused(f'scale not cleared: {scale}')
    insert(db, 'bench_weighing', id=id, scale=scale, minute=minute, item=item,
           tare=tare, gross=gross, check_id=clearance['check_id'], net=gross-tare)
    return ticket(db, id)


def ticket(db, id):
    return dict(one(db, 'SELECT * FROM bench_weighing WHERE id=?', (id,)))
