from . import release
from .db import Refused, insert, one


def piece_price(order, grade):
    if grade == 'STANDARD':
        return order['standard_cents']
    if grade == 'DOWNGRADED' and order['downgrade']:
        return order['reduced_cents']
    raise Refused('piece is not deliverable')


def manifest(db, dispatch_id):
    order_id = one(db, 'SELECT order_id FROM dispatch WHERE id=?', (dispatch_id,))[0]
    rows = [dict(row) for row in db.execute('''SELECT piece_id, assessment_id, cents FROM settlement
                                               WHERE dispatch_id=? ORDER BY piece_id''', (dispatch_id,))]
    return {'dispatch': dispatch_id, 'order': order_id, 'items': rows, 'total_cents': sum(row['cents'] for row in rows)}


def dispatch(db, id, order_id, piece_ids):
    if not isinstance(piece_ids, list) or not piece_ids or len(piece_ids) != len(set(piece_ids)):
        raise Refused('dispatch requires a unique nonempty piece list')
    if db.execute('SELECT 1 FROM dispatch WHERE id=?', (id,)).fetchone():
        posted = manifest(db, id)
        if posted['order'] != order_id or [r['piece_id'] for r in posted['items']] != sorted(piece_ids):
            raise Refused('dispatch ID was already posted with another manifest')
        return posted
    order = one(db, 'SELECT * FROM customer_order WHERE id=?', (order_id,))
    insert(db, 'dispatch', id=id, order_id=order_id)
    for piece_id in piece_ids:
        piece_order = one(db, 'SELECT order_id FROM piece WHERE id=?', (piece_id,))[0]
        if piece_order != order_id:
            raise Refused('dispatch contains a piece from another order')
        assessment = release.latest(db, piece_id)
        if not release.is_current(db, assessment):
            raise Refused('piece assessment is stale after new zonal evidence')
        insert(db, 'settlement', piece_id=piece_id, dispatch_id=id, assessment_id=assessment['seq'],
               cents=piece_price(order, assessment['decision']))
    return manifest(db, id)


def balance(db, order_id):
    one(db, 'SELECT 1 FROM customer_order WHERE id=?', (order_id,))
    rows = list(db.execute('''SELECT p.id, s.cents FROM piece p LEFT JOIN settlement s ON s.piece_id=p.id
                              WHERE p.order_id=? ORDER BY p.id''', (order_id,)))
    return {'order': order_id, 'unshipped': [r['id'] for r in rows if r['cents'] is None],
            'settled_cents': sum(r['cents'] or 0 for r in rows)}
