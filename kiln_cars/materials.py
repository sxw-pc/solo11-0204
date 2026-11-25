"""Immutable carton receipts and lot-specific transfers to registered kiln zones.

A supplier carton may hold several packing segments with different lots. Picks
scanned only by carton number keep their lot unknown; later packing evidence
(see isolation) is what binds physical units to lots and zones.
"""

from datetime import datetime, timezone

from .db import Refused, insert, one


def _text(*values):
    return all(type(value) is str and value.strip() for value in values)


def _stamp(picked_at):
    if picked_at is None:
        return datetime.now(timezone.utc).isoformat(timespec='seconds')
    if not _text(picked_at):
        raise Refused('invalid issue timestamp')
    return picked_at


def arrival(db, id, lot, units, document):
    if type(units) is not int or units < 1 or not _text(id, lot, document):
        raise Refused('invalid material receipt')
    insert(db, 'material_carton', id=id, lot=lot, units=units, document=document)
    insert(db, 'material_segment', id=f'{id}:0', carton=id, units=units, lot=lot, document=document)
    return id


def split_arrival(db, id, units, document, segments):
    """Receive one supplier carton containing several physical lot segments.

    Segments without documentary lot certainty register lot=None and carry the
    shared packing record instead; their real composition arrives later.
    """
    if type(units) is not int or units < 1 or not _text(id, document):
        raise Refused('invalid material receipt')
    if (not isinstance(segments, list) or not segments
            or sum((s.get('units', 0) for s in segments), 0) != units):
        raise Refused('segments must conserve the carton quantity')
    prepared = []
    for index, segment in enumerate(segments):
        amount = segment.get('units')
        lot = segment.get('lot')
        source = segment.get('document', document)
        if type(amount) is not int or amount < 1 or not _text(source):
            raise Refused('invalid material segment')
        if lot is not None and not _text(lot):
            raise Refused('invalid material segment')
        prepared.append((f'{id}:{index}', amount, lot, source))
    insert(db, 'material_carton', id=id, lot=None, units=units, document=document)
    for segment_id, amount, lot, source in prepared:
        insert(db, 'material_segment', id=segment_id, carton=id, units=amount, lot=lot, document=source)
    return id


def returned_balance(db, carton):
    """Units already given back and not yet consumed by a later reissue pick."""
    returned = db.execute('''SELECT COALESCE(SUM(r.units),0) FROM material_return r
                             JOIN material_pick p ON p.id=r.pick WHERE p.carton=?''',
                          (carton,)).fetchone()[0]
    reissued = db.execute('SELECT COALESCE(SUM(units),0) FROM material_pick WHERE carton=? AND reissue=1',
                          (carton,)).fetchone()[0]
    return returned - reissued


def available(db, carton):
    received = one(db, 'SELECT units FROM material_carton WHERE id=?', (carton,))[0]
    used = db.execute('SELECT COALESCE(SUM(units),0) FROM material_pick WHERE carton=?',
                      (carton,)).fetchone()[0]
    returned = db.execute('''SELECT COALESCE(SUM(r.units),0) FROM material_return r
                             JOIN material_pick p ON p.id=r.pick WHERE p.carton=?''',
                          (carton,)).fetchone()[0]
    return received - used + returned


def _frozen(db, *, carton=None, position=None, pick=None):
    clauses, args = [], []
    if carton is not None:
        clauses.append("(kind='carton' AND carton=?)"), args.append(carton)
    if position is not None:
        clauses.append("(kind='zone' AND firing_id=? AND car=? AND zone=?)"), args.extend(position)
    if pick is not None:
        clauses.append('''(kind='zone' AND (firing_id,car,zone) =
                          (SELECT firing_id,car,zone FROM material_pick WHERE id=?))'''), args.append(pick)
    if not clauses:
        return None
    row = db.execute(f"SELECT reason FROM material_hold WHERE state='FROZEN' AND ({' OR '.join(clauses)})",
                     args).fetchone()
    return row['reason'] if row else None


def _create_pick(db, id, carton, units, picked_at, reissue, firing_id=None, car=None, zone=None):
    if type(units) is not int or units < 1:
        raise Refused('insufficient material')
    balance = returned_balance(db, carton)
    if reissue:
        if db.execute('SELECT 1 FROM material_pick WHERE carton=? AND reissue=1', (carton,)).fetchone():
            raise Refused('returned remainder can only feed one later issue')
        if units != balance:
            raise Refused('the whole returned remainder must be taken in its single later issue')
    elif balance:
        raise Refused('returned remainder must be cleared before further fresh issues')
    free = available(db, carton) - balance
    allowance = balance if reissue else free
    if units > allowance:
        raise Refused('insufficient material')
    return insert(db, 'material_pick', id=id, carton=carton, firing_id=firing_id, car=car, zone=zone,
                  units=units, picked_at=picked_at, reissue=1 if reissue else 0)


def pick_line(db, id, carton, units, picked_at=None):
    """Initial issue line scanned with carton number only, no zone yet."""
    one(db, 'SELECT 1 FROM material_carton WHERE id=?', (carton,))
    if _frozen(db, carton=carton):
        raise Refused('carton is frozen for isolation')
    return _create_pick(db, id, carton, units, _stamp(picked_at), False)


def direct_line(db, pick, firing_id, car, zone):
    """Direct an already scanned issue line at a registered kiln zone."""
    row = one(db, 'SELECT * FROM material_pick WHERE id=?', (pick,))
    if row['firing_id'] is not None:
        raise Refused('issue line already directed')
    reason = _frozen(db, position=(firing_id, car, zone))
    if reason:
        raise Refused(reason)
    one(db, 'SELECT 1 FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?', (firing_id, car, zone))
    db.execute('UPDATE material_pick SET firing_id=?, car=?, zone=? WHERE id=?',
               (firing_id, car, zone, pick))
    return pick


def pick(db, id, carton, firing_id, car, zone, units, picked_at=None, reissue=False):
    one(db, 'SELECT 1 FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?', (firing_id, car, zone))
    one(db, 'SELECT 1 FROM material_carton WHERE id=?', (carton,))
    reason = _frozen(db, carton=carton, position=(firing_id, car, zone))
    if reason:
        raise Refused(reason)
    return _create_pick(db, id, carton, units, _stamp(picked_at), bool(reissue), firing_id, car, zone)


def give_back(db, id, pick, units):
    row = one(db, 'SELECT * FROM material_pick WHERE id=?', (pick,))
    if row['reissue']:
        raise Refused('reissued remainder cannot be returned again')
    reason = _frozen(db, carton=row['carton'], pick=pick)
    if reason:
        raise Refused(reason)
    sent = row['units']
    already = db.execute('SELECT COALESCE(SUM(units),0) FROM material_return WHERE pick=?',
                         (pick,)).fetchone()[0]
    if type(units) is not int or not 1 <= units <= sent - already:
        raise Refused('return exceeds material delivered')
    insert(db, 'material_return', id=id, pick=pick, units=units)
    return id
