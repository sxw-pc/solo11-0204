"""Consume a track movement receipt and move the cart to its surveyed endpoint."""
from .db import Refused, insert, one
from .yard_map import edge


def move(db, id, cart, track, document):
    arc = edge(db, track)
    old = one(db,'SELECT * FROM yard_cart WHERE id=?',(cart,))
    if arc['source'] != old['node'] or arc['length'] < old['length']:
        raise Refused('cart cannot enter this track')
    insert(db, 'yard_move', id=id, cart=cart, track=track, document=document)
    db.execute('UPDATE yard_cart SET node=? WHERE id=?',(arc['target'],cart))
    return status(db, cart)


def status(db, cart):
    return dict(one(db,'SELECT * FROM yard_cart WHERE id=?',(cart,)))
