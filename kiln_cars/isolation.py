"""Turn suspected gasket destinations into executable stop-issue dispositions.

Evidence arrives in stages. A supplier trace may only give the lot multiset of
a mixed carton; a locatable packing record then binds part of that multiset to
an original issue line. Between those moments every feasible physical
assignment remains live: zones that could hold the problem lot are frozen,
zones proven not to hold it keep issuing. All quantities stay conserved per
supplier carton across stock, issue lines, kiln zones and quarantine groups.
"""

from collections import defaultdict

from .db import Refused, insert, one
from .materials import available


def locate(db, lot):
    cartons = {}
    for row in db.execute('SELECT * FROM material_carton WHERE lot=? ORDER BY id', (lot,)):
        cartons[row['id']] = available(db, row['id'])
    zones = {}
    for row in db.execute('''SELECT p.id,p.firing_id,p.car,p.zone,p.units-COALESCE(SUM(r.units),0) AS units
                             FROM material_pick p JOIN material_carton c ON c.id=p.carton
                             LEFT JOIN material_return r ON r.pick=p.id
                             WHERE c.lot=? GROUP BY p.id ORDER BY p.id''', (lot,)):
        if row['units']:
            zones[row['id']] = {key: row[key] for key in ('firing_id', 'car', 'zone', 'units')}
    return {'lot': lot, 'stock': cartons, 'zones': zones}


# ---------------------------------------------------------------- conservation

def lot_ledger(db, carton):
    """Lot multiset a carton is known to contain.

    Certified segments and locatable composition quantities are exact counts.
    Remaining units carry lot=None: nothing binds them, so relative to a
    problem lot they stay wildcards that could physically be that lot.
    """
    total = one(db, 'SELECT units FROM material_carton WHERE id=?', (carton,))[0]
    ledger = defaultdict(int)
    unattributed = 0
    for segment in db.execute('SELECT lot, units FROM material_segment WHERE carton=?', (carton,)):
        if segment['lot'] is None:
            unattributed += segment['units']
        else:
            ledger[segment['lot']] += segment['units']
    explained = 0
    for row in db.execute('''SELECT lot, COALESCE(SUM(units),0) AS units FROM material_lot_proof
                             WHERE carton=? AND kind='composition' GROUP BY lot''', (carton,)):
        ledger[row['lot']] += row['units']
        explained += row['units']
    if explained > unattributed:
        raise Refused('packing composition exceeds unattributed carton segments')
    if unattributed - explained:
        ledger[None] += unattributed - explained
    if sum(ledger.values()) != total:
        raise Refused('carton candidate quantities are not conserved')
    return dict(ledger)


def _returns(db, pick):
    return db.execute('SELECT COALESCE(SUM(units),0) FROM material_return WHERE pick=?',
                      (pick,)).fetchone()[0]


def _net_pick_units(db, row):
    return row['units'] - _returns(db, row['id'])

def _current_holders(db, carton):
    """Physical units per current location, following return/reissue moves.

    Returned units leave their original receipt; the carton's single reissue
    pick receives the whole returned balance and is where those units now sit.
    """
    picks = list(db.execute('SELECT * FROM material_pick WHERE carton=? ORDER BY picked_at, id',
                            (carton,)))
    holders = []

    def add(kind, holder_id, units, pick_row):
        if units <= 0:
            return
        entry = {'kind': kind, 'holder': holder_id, 'pick': pick_row['id'], 'units': units,
                 'picked_at': pick_row['picked_at'], 'reissue': bool(pick_row['reissue'])}
        if kind == 'zone':
            entry.update({'firing_id': pick_row['firing_id'], 'car': pick_row['car'],
                          'zone': pick_row['zone']})
        holders.append(entry)

    quarantined_picks = {row['pick'] for row in db.execute(
        '''SELECT DISTINCT pick FROM quarantine_reservation
           WHERE carton=? AND state IN ('QUARANTINED','VERIFIED')''', (carton,))}
    for row in picks:
        if row['id'] in quarantined_picks:
            # The physical group now sits in a quarantine position, not on the kiln.
            continue
        if row['reissue']:
            if row['firing_id'] is None:
                add('line', f"line:{row['id']}", row['units'], row)
            else:
                add('zone', f"zone:{row['firing_id']}|{row['car']}|{row['zone']}", row['units'], row)
            continue
        kept = _net_pick_units(db, row)
        if row['firing_id'] is None:
            add('line', f"line:{row['id']}", kept, row)
        else:
            add('zone', f"zone:{row['firing_id']}|{row['car']}|{row['zone']}", kept, row)
    for row in db.execute('''SELECT position, pick, COALESCE(SUM(units),0) AS units FROM quarantine_reservation
                             WHERE carton=? AND state IN ('RESERVED','QUARANTINED','VERIFIED')
                             GROUP BY position, pick''', (carton,)):
        holders.append({'kind': 'quarantine', 'holder': f"quarantine:{row['position']}",
                        'position': row['position'], 'pick': row['pick'], 'units': row['units']})
    # Reservations without a receipt line reserve stock units directly; those
    # units no longer sit on the shelf while the group is held for transfer.
    reserved_stock = db.execute('''SELECT COALESCE(SUM(units),0) FROM quarantine_reservation
                                   WHERE carton=? AND pick IS NULL
                                   AND state IN ('RESERVED','QUARANTINED','VERIFIED')''',
                                (carton,)).fetchone()[0]
    # Stock is what remains physically in the carton after every pick, with
    # returns re-entering stock and the single reissue pick taking them back out.
    stock = available(db, carton) - reserved_stock
    if stock < 0:
        raise Refused('carton candidate quantities are not conserved')
    if stock:
        holders.append({'kind': 'stock', 'holder': f'stock:{carton}', 'carton': carton, 'units': stock})
    received = db.execute('SELECT units FROM material_carton WHERE id=?', (carton,)).fetchone()[0]
    if sum(holder['units'] for holder in holders) != received:
        raise Refused('carton candidate quantities are not conserved')
    return holders, received


def _proof_holder(db, proof):
    if proof['kind'] == 'composition':
        return None
    pick = one(db, 'SELECT * FROM material_pick WHERE id=?', (proof['pick'],))
    reservation = db.execute('''SELECT position FROM quarantine_reservation
                                WHERE pick=? AND state IN ('QUARANTINED','VERIFIED') LIMIT 1''',
                             (proof['pick'],)).fetchone()
    if reservation is not None:
        return f"quarantine:{reservation['position']}"
    if _net_pick_units(db, pick) <= 0:
        # The physical units moved through a return and its single later issue.
        reissue = one(db, '''SELECT * FROM material_pick WHERE carton=? AND reissue=1
                             ORDER BY picked_at, id''', (proof['carton'],))
        if reissue['firing_id'] is None:
            return f"line:{reissue['id']}"
        return f"zone:{reissue['firing_id']}|{reissue['car']}|{reissue['zone']}"
    if pick['firing_id'] is None:
        return f"line:{pick['id']}"
    return f"zone:{pick['firing_id']}|{pick['car']}|{pick['zone']}"


def _fixed_claims(db, carton, capacities):
    claims = defaultdict(lambda: defaultdict(int))
    for proof in db.execute("SELECT * FROM material_lot_proof WHERE carton=? AND kind='located' ORDER BY seq",
                            (carton,)):
        holder_id = _proof_holder(db, proof)
        claims[holder_id][proof['lot']] += proof['units']
    for holder_id, lots in claims.items():
        if holder_id not in capacities or sum(lots.values()) > capacities[holder_id]:
            raise Refused('packing evidence exceeds the physical units at its original receipt')
    return claims


def _enumerate(db, carton, query_lot):
    holders, received = _current_holders(db, carton)
    capacities = defaultdict(int)
    for holder in holders:
        capacities[holder['holder']] += holder['units']
    claims = _fixed_claims(db, carton, capacities)
    ledger = defaultdict(int, lot_ledger(db, carton))
    possible = defaultdict(set)
    free_slots = []
    for holder_id, units in capacities.items():
        fixed = claims.get(holder_id, {})
        for lot, amount in fixed.items():
            if ledger.get(lot, 0) < amount:
                raise Refused(f'packing evidence cannot be satisfied for lot {lot}')
            ledger[lot] -= amount
            possible[holder_id].add(lot)
        open_units = units - sum(fixed.values())
        if open_units > 0:
            free_slots.extend([holder_id] * open_units)
    wildcard_count = ledger.pop(None, 0)
    candidates = ({lot for lot in ledger if lot is not None}
                  | set().union(*(set(lots) for lots in claims.values()), set()) | {query_lot})
    pool = {lot: units for lot, units in ledger.items() if units}
    # Known-lot units must be placed; wildcard units may take any candidate lot.
    if sum(pool.values()) + wildcard_count != len(free_slots):
        raise Refused('carton candidate quantities are not conserved')
    solutions = set()

    def walk(index, arrangement, remaining, wildcards_left):
        if index == len(free_slots):
            if wildcards_left == 0 and not any(remaining.values()):
                solutions.add(tuple(sorted(arrangement)))
            return
        for lot in candidates:
            if remaining.get(lot, 0):
                remaining[lot] -= 1
                walk(index + 1, arrangement + ((free_slots[index], lot),),
                     remaining, wildcards_left)
                remaining[lot] += 1
            elif wildcards_left:
                walk(index + 1, arrangement + ((free_slots[index], lot),),
                     remaining, wildcards_left - 1)

    walk(0, (), dict(pool), wildcard_count)
    if not solutions:
        raise Refused('recorded packing evidence has no feasible physical assignment')
    wildcard_by_holder = defaultdict(int)
    if wildcard_count:
        # With at least one unattributed unit, every open holder could be the
        # wildcard carrier: record that explicitly for the verdict step.
        for slot in free_slots:
            wildcard_by_holder[slot] += 1
    presence = defaultdict(int)
    for arrangement in solutions:
        for holder_id, lot in arrangement:
            possible[holder_id].add(lot)
            if lot == query_lot:
                presence[holder_id] += 1
    totals = defaultdict(int)
    for holder in holders:
        totals[holder['holder']] += holder['units']
    rows = []
    for holder_id, units in totals.items():
        fixed_lots = possible[holder_id]
        forced = {lot for lot, amount in claims.get(holder_id, {}).items() if amount >= units}
        if query_lot in forced or presence[holder_id] == len(solutions):
            verdict = 'ISOLATED'
        elif wildcard_by_holder[holder_id] or query_lot in fixed_lots:
            verdict = 'SUSPECT'
        else:
            verdict = 'CLEAR'
        rows.append({'holder': holder_id, 'units': units, 'verdict': verdict})
    return rows, received, possible, len(solutions) or 1


def _suspect_cartons(db, lot):
    result = []
    for row in db.execute('SELECT id FROM material_carton ORDER BY id'):
        carton = row['id']
        ledger = lot_ledger(db, carton)
        mentioned = (lot in ledger
                     or db.execute('SELECT 1 FROM material_lot_proof WHERE carton=? AND lot=? LIMIT 1',
                                   (carton, lot)).fetchone())
        # A carton still carrying fully unattributed units cannot rule the lot out.
        unattributed = db.execute(
            'SELECT COALESCE(SUM(units),0) FROM material_segment WHERE carton=? AND lot IS NULL',
            (carton,)).fetchone()[0]
        explained = db.execute(
            "SELECT COALESCE(SUM(units),0) FROM material_lot_proof WHERE carton=? AND kind='composition'",
            (carton,)).fetchone()[0]
        if mentioned or unattributed - explained > 0:
            result.append(carton)
    return result


def directive(db, lot):
    """Minimum safe isolation scope for one problem lot, over every linked carton."""
    verdicts, conservation = [], []
    details = {}
    for carton in _suspect_cartons(db, lot):
        holders, received = _current_holders(db, carton)
        for holder in holders:
            details[holder['holder']] = holder
        rows, _, _, _ = _enumerate(db, carton, lot)
        conservation.append({'carton': carton, 'received': received,
                             'held': sum(holder['units'] for holder in holders),
                             'ledger': {str(key): value for key, value in lot_ledger(db, carton).items()}})
        for row in rows:
            holder = details[row['holder']]
            entry = {key: value for key, value in holder.items() if key != 'holder'}
            entry.update({'holder': holder['holder'], 'carton': carton, 'verdict': row['verdict']})
            verdicts.append(entry)
    return {'lot': lot, 'holders': verdicts, 'conservation': conservation,
            'frozen': active_holds(db)}


# ------------------------------------------------------------------ evidence

def _same_document(db, document, carton, kind):
    return list(db.execute('''SELECT * FROM material_lot_proof
                              WHERE document=? AND carton=? AND kind=?''', (document, carton, kind)))


def register_composition(db, document, carton, lots):
    one(db, 'SELECT 1 FROM material_carton WHERE id=?', (carton,))
    if not isinstance(document, str) or not document.strip() or not isinstance(lots, dict) or not lots:
        raise Refused('composition requires a document and lot quantities')
    existing = _same_document(db, document, carton, 'composition')
    if existing and {row['lot']: row['units'] for row in existing} != dict(lots):
        raise Refused('document was already posted with a different composition')
    known = {(row['lot'], row['units']) for row in existing}
    for lot, units in lots.items():
        if type(units) is not int or units < 1 or not isinstance(lot, str) or not lot.strip():
            raise Refused('invalid lot composition')
        if (lot, units) not in known:
            insert(db, 'material_lot_proof', document=document, carton=carton, lot=lot,
                   units=units, pick=None, kind='composition')
    _enumerate(db, carton, next(iter(lots)))
    return document


def register_packing(db, document, carton, pick, lot, units):
    row = one(db, 'SELECT * FROM material_pick WHERE id=? AND carton=?', (pick, carton))
    if type(units) is not int or units < 1 or not isinstance(lot, str) or not lot.strip() \
            or not isinstance(document, str) or not document.strip():
        raise Refused('invalid packing record')
    previous = db.execute('''SELECT * FROM material_lot_proof WHERE carton=? AND pick=? AND kind='located'
                             ORDER BY seq''', (carton, pick)).fetchall()
    duplicate = any(item['document'] == document and item['lot'] == lot and item['units'] == units
                    for item in previous)
    if not duplicate and sum(item['units'] for item in previous) + units > row['units']:
        raise Refused('packing record exceeds the units of its original receipt')
    for item in previous:
        if item['document'] == document and (item['lot'], item['units']) != (lot, units):
            raise Refused('document was already posted with a different packing record')
    if not duplicate:
        insert(db, 'material_lot_proof', document=document, carton=carton, lot=lot,
               units=units, pick=pick, kind='located')
    _enumerate(db, carton, lot)
    return document


# ------------------------------------------------------------- stop-issue holds

def active_holds(db):
    return [dict(row) for row in
            db.execute("SELECT seq,kind,firing_id,car,zone,carton,position,reason,group_id FROM material_hold "
                       "WHERE state='FROZEN' ORDER BY seq")]


def _add_hold(db, kind, reason, *, firing_id=None, car=None, zone=None, carton=None,
              position=None, group_id=None):
    existing = db.execute("SELECT seq FROM material_hold WHERE state='FROZEN' AND kind=? AND "
                          "COALESCE(firing_id,'')=COALESCE(?, '') AND COALESCE(car,'')=COALESCE(?, '') "
                          "AND COALESCE(zone,'')=COALESCE(?, '') AND COALESCE(carton,'')=COALESCE(?, '') "
                          "AND COALESCE(position,'')=COALESCE(?, '')",
                          (kind, firing_id, car, zone, carton, position)).fetchone()
    if existing:
        if group_id is not None:
            db.execute('UPDATE material_hold SET group_id=? WHERE seq=? AND group_id IS NULL',
                       (group_id, existing['seq']))
        return existing['seq']
    return insert(db, 'material_hold', kind=kind, firing_id=firing_id, car=car, zone=zone,
                  carton=carton, position=position, reason=reason, state='FROZEN', group_id=group_id)


def impose(db, lot):
    """Freeze every holder that could still physically contain the problem lot."""
    plan = directive(db, lot)
    frozen = []
    for holder in plan['holders']:
        if holder['verdict'] == 'CLEAR':
            continue
        reason = f"{lot} {holder['verdict'].lower()} at holder"
        if holder['kind'] == 'zone':
            seq = _add_hold(db, 'zone', reason, firing_id=holder['firing_id'],
                            car=holder['car'], zone=holder['zone'])
        elif holder['kind'] == 'quarantine':
            seq = _add_hold(db, 'quarantine', reason, position=holder['position'])
        else:
            seq = _add_hold(db, 'carton', reason, carton=holder['carton'])
        frozen.append(seq)
    return {'lot': lot, 'frozen': frozen, 'directive': plan}


def reconcile(db, lot):
    """Release directive holds whose holders are proven clear.

    Grouped holds (in-place stops and quarantine moves) are operational and
    stay until their own release/verification step. A carton or zone absent
    from the recomputed directive keeps its hold: the proof that would clear
    it is not on file.
    """
    plan = directive(db, lot)
    verdict_by_holder = {holder['holder']: holder['verdict'] for holder in plan['holders']}
    uncertain_cartons = {holder['carton'] for holder in plan['holders']
                         if holder['kind'] in ('stock', 'line') and holder['verdict'] != 'CLEAR'}
    linked_cartons = {holder['carton'] for holder in plan['holders']}
    released = []
    for hold in db.execute("SELECT * FROM material_hold WHERE state='FROZEN' AND group_id IS NULL"):
        if hold['kind'] == 'zone':
            clear = verdict_by_holder.get(
                f"zone:{hold['firing_id']}|{hold['car']}|{hold['zone']}") == 'CLEAR'
        elif hold['kind'] == 'carton':
            clear = hold['carton'] in linked_cartons and hold['carton'] not in uncertain_cartons
        else:
            clear = False
        if clear:
            db.execute("UPDATE material_hold SET state='RELEASED' WHERE seq=?", (hold['seq'],))
            released.append(hold['seq'])
    return {'lot': lot, 'released': released, 'frozen': active_holds(db)}


def hold_in_place(db, id, firing_id, car, zone, reason):
    """Stop units that cannot be identified on the spot; the zone stays frozen."""
    one(db, 'SELECT 1 FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?',
        (firing_id, car, zone))
    if not isinstance(reason, str) or not reason.strip():
        raise Refused('in-place stop requires a reason')
    return _add_hold(db, 'zone', reason, firing_id=firing_id, car=car, zone=zone,
                     group_id=f'INPLACE:{id}')


def release_in_place(db, firing_id, car, zone):
    hold = one(db, "SELECT * FROM material_hold WHERE state='FROZEN' AND kind='zone' AND "
                   "firing_id=? AND car=? AND zone=? AND group_id LIKE 'INPLACE:%'",
               (firing_id, car, zone))
    db.execute("UPDATE material_hold SET state='RELEASED' WHERE seq=?", (hold['seq'],))
    return hold['seq']


# ----------------------------------------------------------- quarantine positions

def quarantine_position(db, id, capacity):
    if type(capacity) is not int or capacity < 0:
        raise Refused('quarantine capacity must be a nonnegative integer')
    insert(db, 'quarantine_position', id=id, capacity=capacity)
    return id


def _position_load(db, position):
    return db.execute('''SELECT COALESCE(SUM(units),0) FROM quarantine_reservation
                         WHERE position=? AND state IN ('RESERVED','QUARANTINED','VERIFIED')''',
                      (position,)).fetchone()[0]


def reserve_group(db, id, position, carton, units, pick=None):
    """Reserve capacity for the whole physical group that may arrive together."""
    row = one(db, 'SELECT * FROM quarantine_position WHERE id=?', (position,))
    if type(units) is not int or units < 1:
        raise Refused('invalid quarantine group size')
    if pick is not None:
        one(db, 'SELECT 1 FROM material_pick WHERE id=? AND carton=?', (pick, carton))
    if _position_load(db, position) + units > row['capacity']:
        raise Refused('quarantine position capacity cannot hold the whole affected group')
    insert(db, 'quarantine_reservation', id=id, position=position, carton=carton,
           units=units, state='RESERVED', pick=pick)
    return id


def transfer_out(db, id):
    reservation = one(db, "SELECT * FROM quarantine_reservation WHERE id=?", (id,))
    if reservation['state'] != 'RESERVED':
        raise Refused('group has already been transferred')
    if reservation['pick'] is None:
        origin = one(db, "SELECT * FROM material_hold WHERE state='FROZEN' AND kind='carton' AND carton=?",
                     (reservation['carton'],))
    else:
        pick = one(db, 'SELECT * FROM material_pick WHERE id=?', (reservation['pick'],))
        if not pick['reissue'] and _net_pick_units(db, pick) != reservation['units']:
            raise Refused('only the complete physical group may be moved together')
        if pick['firing_id'] is None:
            raise Refused('issue line must be directed before it can be moved to quarantine')
        origin = db.execute("SELECT * FROM material_hold WHERE state='FROZEN' AND kind='zone' AND "
                            "firing_id=? AND car=? AND zone=?",
                            (pick['firing_id'], pick['car'], pick['zone'])).fetchone()
        if origin is None:
            raise Refused('only a frozen holder can be moved to quarantine')
    db.execute("UPDATE quarantine_reservation SET state='QUARANTINED' WHERE id=?", (id,))
    db.execute("UPDATE material_hold SET group_id=? WHERE seq=?", (id, origin['seq']))
    _add_hold(db, 'quarantine', f'quarantine group {id}',
              position=reservation['position'], group_id=id)
    return id


def verify_transfer(db, id, document):
    reservation = one(db, "SELECT * FROM quarantine_reservation WHERE id=? AND state='QUARANTINED'", (id,))
    if not isinstance(document, str) or not document.strip():
        raise Refused('transfer verification needs the original receipt document')
    db.execute("UPDATE quarantine_reservation SET state='VERIFIED' WHERE id=?", (id,))
    db.execute("UPDATE material_hold SET state='RELEASED' WHERE group_id=? AND kind IN ('zone','carton')",
               (id,))
    return id
