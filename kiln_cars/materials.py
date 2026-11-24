"""Immutable carton receipts and lot-specific transfers to registered kiln zones."""

from .db import Refused, insert, one


def arrival(db, id, lot, units, document):
    if type(units) is not int or units < 1 or not all(isinstance(x,str) and x.strip() for x in (id,lot,document)):
        raise Refused('invalid material receipt')
    insert(db,'material_carton',id=id,lot=lot,units=units,document=document)
    return id


def available(db, carton):
    received = one(db,'SELECT units FROM material_carton WHERE id=?',(carton,))[0]
    used = db.execute('SELECT COALESCE(SUM(units),0) FROM material_pick WHERE carton=?',(carton,)).fetchone()[0]
    returned = db.execute('''SELECT COALESCE(SUM(r.units),0) FROM material_return r
                             JOIN material_pick p ON p.id=r.pick WHERE p.carton=?''',(carton,)).fetchone()[0]
    return received - used + returned


def pick(db,id,carton,firing_id,car,zone,units):
    one(db,'SELECT 1 FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?',(firing_id,car,zone))
    if type(units) is not int or not 1 <= units <= available(db,carton):
        raise Refused('insufficient material')
    insert(db,'material_pick',id=id,carton=carton,firing_id=firing_id,car=car,zone=zone,units=units)
    return id


def give_back(db,id,pick,units):
    sent = one(db,'SELECT units FROM material_pick WHERE id=?',(pick,))[0]
    returned = db.execute('SELECT COALESCE(SUM(units),0) FROM material_return WHERE pick=?',(pick,)).fetchone()[0]
    if type(units) is not int or not 1 <= units <= sent-returned:
        raise Refused('return exceeds material delivered')
    insert(db,'material_return',id=id,pick=pick,units=units)
    return id
