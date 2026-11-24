"""Survey transfer-track geometry before assigning movements to real trial carts."""
from .db import Refused, insert, one


def track(db, id, source, target, length):
    if source == target:
        raise Refused('distinct endpoints required')
    insert(db, 'yard_track', id=id, source=source, target=target, length=length)
    return id


def cart(db, id, node, length):
    insert(db, 'yard_cart', id=id, node=node, length=length)
    return {'cart':id, 'node':node}


def edge(db, id):
    return one(db,'SELECT * FROM yard_track WHERE id=?',(id,))
