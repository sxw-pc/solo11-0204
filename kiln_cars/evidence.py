from itertools import groupby

from .db import Refused, insert, one
from .loading import require_state


def capture_trace(db, firing_id, car, zone, tick, temperature, document):
    batch = require_state(db, firing_id, 'LOCKED')
    if type(tick) is not int or type(temperature) is not int or not 0 <= tick < batch['ticks'] or not document.strip():
        raise Refused('trace tick or document is invalid')
    key = (firing_id, car, zone, tick)
    previous = db.execute('SELECT * FROM trace WHERE firing_id=? AND car=? AND zone=? AND tick=?', key).fetchone()
    if previous:
        if (previous['temperature'], previous['document']) != (temperature, document):
            raise Refused('conflicting logger tick')
        return previous['seq']
    return insert(db, 'trace', firing_id=firing_id, car=car, zone=zone, tick=tick,
                  temperature=temperature, document=document)


def capture_witness(db, firing_id, car, zone, coupon, first_tick, last_tick, low, high, observer):
    batch = require_state(db, firing_id, 'LOCKED')
    observer = observer.strip().casefold()
    if not observer or observer == batch['logger_owner'].strip().casefold():
        raise Refused('witness must be independent')
    if any(type(value) is not int for value in (first_tick, last_tick, low, high)):
        raise Refused('witness interval and temperatures must be integers')
    if not coupon.strip() or not 0 <= first_tick <= last_tick < batch['ticks']:
        raise Refused('witness bounds outside firing window')
    values = dict(firing_id=firing_id, car=car, zone=zone, coupon=coupon, first_tick=first_tick,
                  last_tick=last_tick, low=low, high=high, observer=observer)
    previous = db.execute('SELECT * FROM witness WHERE coupon=?', (coupon,)).fetchone()
    if previous:
        if any(previous[key] != value for key, value in values.items()):
            raise Refused('coupon already registered with different evidence')
        return previous['seq']
    return insert(db, 'witness', **values)


def location(db, piece_id):
    return one(db, '''SELECT a.*, f.ticks, f.state, r.low, r.high, r.body, p.order_id
                     FROM placement a JOIN firing f ON f.id=a.firing_id
                     JOIN piece p ON p.id=a.piece_id JOIN recipe r ON r.id=p.recipe_id
                     WHERE a.piece_id=?''', (piece_id,))


def location_key(position):
    return tuple(position[key] for key in ('firing_id', 'car', 'zone'))


def versions(db, position):
    return tuple(db.execute(f'''SELECT COALESCE(MAX(seq),0) FROM {table}
                            WHERE firing_id=? AND car=? AND zone=?''', location_key(position)).fetchone()[0]
                 for table in ('trace', 'witness'))


def temperature_decision(db, position, cutoff=None):
    if position['state'] != 'LOCKED':
        raise Refused('thermal acceptance requires a locked load')
    trace_asof, witness_asof = cutoff if cutoff is not None else versions(db, position)
    query = ' WHERE firing_id=? AND car=? AND zone=? AND seq<=?'
    key = location_key(position)
    recorded = {r['tick']: r['temperature'] for r in db.execute('SELECT * FROM trace' + query, (*key, trace_asof))}
    coupons = list(db.execute('SELECT * FROM witness' + query, (*key, witness_asof)))
    low, high = position['low'], position['high']
    if any(not low <= value <= high for value in recorded.values()):
        return 'OUTSIDE_WINDOW'
    missing = (tick for tick in range(position['ticks']) if tick not in recorded)
    gaps = [[tick for _, tick in group]
            for _, group in groupby(enumerate(missing), key=lambda pair: pair[1] - pair[0])]
    for gap in gaps:
        exact = [row for row in coupons if (row['first_tick'], row['last_tick']) == (gap[0], gap[-1])]
        if not exact or any(not low <= row['low'] <= row['high'] <= high for row in exact):
            return 'EVIDENCE_GAP'
    return 'WITNESSED' if gaps else 'LOGGER_COMPLETE'


def zone_status(db, piece_id):
    position = location(db, piece_id)
    return temperature_decision(db, position)


