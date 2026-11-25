from .db import Refused, insert, one


def order(db, id, recipient, standard_cents, reduced_cents, downgrade):
    if any(type(value) is not int for value in (standard_cents, reduced_cents)) or type(downgrade) is not bool:
        raise Refused('prices must be integer cents and consent must be boolean')
    insert(db, 'customer_order', id=id, recipient=recipient, standard_cents=standard_cents,
           reduced_cents=reduced_cents, downgrade=downgrade)


def recipe(db, id, body, color, atmosphere, emission, sensitive_to, low, high):
    if any(type(value) is not int for value in (low, high)):
        raise Refused('body window endpoints must be integer temperatures')
    insert(db, 'recipe', id=id, body=body, color=color, atmosphere=atmosphere,
           emission=emission, sensitive_to=sensitive_to, low=low, high=high)


def piece(db, id, order_id, recipe_id):
    insert(db, 'piece', id=id, order_id=order_id, recipe_id=recipe_id)


def firing(db, id, atmosphere, ticks, logger_owner):
    if type(ticks) is not int or not 1 <= ticks <= 1440 or not logger_owner.strip():
        raise Refused('firing requires a logger owner and 1 to 1440 minute slots')
    insert(db, 'firing', id=id, atmosphere=atmosphere, ticks=ticks, logger_owner=logger_owner, state='OPEN')


def require_state(db, firing_id, state):
    current = one(db, 'SELECT * FROM firing WHERE id=?', (firing_id,))
    if current['state'] != state:
        raise Refused(f'firing {firing_id} must be {state}')
    return current


def slot(db, firing_id, car, zone, setpoint):
    require_state(db, firing_id, 'OPEN')
    if type(setpoint) is not int:
        raise Refused('setpoint must be an integer temperature')
    insert(db, 'kiln_slot', firing_id=firing_id, car=car, zone=zone, setpoint=setpoint)


def load_profile(db, piece_id):
    return one(db, '''SELECT p.id AS piece_id, r.* FROM piece p JOIN recipe r ON r.id=p.recipe_id
                      WHERE p.id=?''', (piece_id,))


def compatibility(db, firing_id, profile, slot_row):
    batch = one(db, 'SELECT * FROM firing WHERE id=?', (firing_id,))
    if profile['atmosphere'] != batch['atmosphere']:
        return 'ATMOSPHERE_MISMATCH'
    if not profile['low'] <= slot_row['setpoint'] <= profile['high']:
        return 'SLOT_OUTSIDE_BODY_WINDOW'
    peers = db.execute('''SELECT r.* FROM placement a JOIN piece p ON a.piece_id=p.id
                          JOIN recipe r ON p.recipe_id=r.id WHERE a.firing_id=? AND a.piece_id<>?''',
                       (firing_id, profile['piece_id']))
    for peer in peers:
        if ((profile['emission'] and profile['emission'] == peer['sensitive_to']) or
                (peer['emission'] and peer['emission'] == profile['sensitive_to'])):
            return 'CROSS_LOAD_COLOR_CONFLICT'
    return 'COMPATIBLE'


def place(db, piece_id, firing_id, car, zone):
    require_state(db, firing_id, 'OPEN')
    position = one(db, 'SELECT * FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?', (firing_id, car, zone))
    result = compatibility(db, firing_id, load_profile(db, piece_id), position)
    if result != 'COMPATIBLE':
        raise Refused(result)
    insert(db, 'placement', piece_id=piece_id, firing_id=firing_id, car=car, zone=zone)
    return result


def unload(db, piece_id):
    placement = one(db, 'SELECT * FROM placement WHERE piece_id=?', (piece_id,))
    require_state(db, placement['firing_id'], 'OPEN')
    db.execute('DELETE FROM placement WHERE piece_id=?', (piece_id,))
    return 'UNLOADED'


def lock(db, firing_id):
    require_state(db, firing_id, 'OPEN')
    one(db, 'SELECT 1 FROM placement WHERE firing_id=? LIMIT 1', (firing_id,))
    db.execute("UPDATE firing SET state='LOCKED' WHERE id=?", (firing_id,))
    return 'LOCKED'


def open_refire(db, piece_id):
    return db.execute("SELECT * FROM refire WHERE piece_id=? AND state='PLACED'",
                      (piece_id,)).fetchone()


def refire(db, piece_id, firing_id, car, zone):
    """Move a rework-queued piece, via its held rework reservation, onto a
    destination kiln slot in another firing for a re-firing."""
    placement = one(db, 'SELECT * FROM placement WHERE piece_id=?', (piece_id,))
    if open_refire(db, piece_id) is not None:
        raise Refused('piece already has an unfinished refire')
    claim = db.execute('''SELECT c.* FROM rework_claim c JOIN assessment a ON a.seq=c.assessment_id
                          WHERE a.piece_id=? AND c.state='HELD'
                          ORDER BY c.seq DESC LIMIT 1''', (piece_id,)).fetchone()
    if claim is None:
        raise Refused('refire needs a held rework reservation from the current assessment')
    assessment = one(db, 'SELECT * FROM assessment WHERE seq=?', (claim['assessment_id'],))
    if assessment['decision'] != 'REWORK_QUEUE':
        raise Refused('refire source assessment must be rework-queued')
    profile = load_profile(db, piece_id)
    slot_row = one(db, 'SELECT * FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?',
                   (firing_id, car, zone))
    reserved = one(db, 'SELECT * FROM rework_slot WHERE id=?', (claim['slot_id'],))
    if reserved['body'] != profile['body']:
        raise Refused('rework reservation body no longer matches the piece body')
    require_state(db, firing_id, 'OPEN')
    if db.execute('SELECT 1 FROM placement WHERE firing_id=? AND car=? AND zone=?',
                  (firing_id, car, zone)).fetchone():
        raise Refused('destination kiln position is occupied')
    result = compatibility(db, firing_id, profile, slot_row)
    if result != 'COMPATIBLE':
        raise Refused(result)
    seq = insert(db, 'refire', piece_id=piece_id, source_assessment_id=assessment['seq'],
                 claim_id=claim['seq'], firing_id=firing_id, car=car, zone=zone, state='PLACED')
    db.execute('DELETE FROM placement WHERE piece_id=?', (piece_id,))
    insert(db, 'placement', piece_id=piece_id, firing_id=firing_id, car=car, zone=zone)
    return {'refire': seq, 'state': 'PLACED', 'firing_id': firing_id, 'car': car, 'zone': zone}


def finish_refire(db, piece_id):
    """Confirm the re-firing actually happened: the destination batch must be
    locked and its thermal evidence becomes the piece's new heat history."""
    record = open_refire(db, piece_id)
    if record is None:
        raise Refused('piece has no unfinished refire')
    destination = one(db, 'SELECT * FROM firing WHERE id=?', (record['firing_id'],))
    if destination['state'] != 'LOCKED':
        raise Refused('refire can only finish after the destination batch is locked')
    from . import evidence
    position = evidence.location(db, piece_id)
    cutoff = evidence.versions(db, position)
    thermal = evidence.temperature_decision(db, position, cutoff)
    db.execute("UPDATE refire SET state='FIRED' WHERE seq=?", (record['seq'],))
    db.execute("UPDATE rework_claim SET state='USED' WHERE seq=?", (record['claim_id'],))
    return {'refire': record['seq'], 'state': 'FIRED', 'thermal': thermal,
            'firing_id': position['firing_id'], 'car': position['car'], 'zone': position['zone'],
            'trace_asof': cutoff[0], 'witness_asof': cutoff[1]}


def withdraw_refire(db, piece_id):
    """Take the piece back before the destination batch has fired. This attempt
    never becomes a heat history and frees the rework position."""
    record = open_refire(db, piece_id)
    if record is None:
        raise Refused('piece has no unfinished refire')
    require_state(db, record['firing_id'], 'OPEN')
    source = one(db, 'SELECT * FROM assessment WHERE seq=?', (record['source_assessment_id'],))
    db.execute('DELETE FROM placement WHERE piece_id=?', (piece_id,))
    insert(db, 'placement', piece_id=piece_id, firing_id=source['firing_id'],
           car=source['car'], zone=source['zone'])
    db.execute("UPDATE refire SET state='WITHDRAWN' WHERE seq=?", (record['seq'],))
    db.execute("UPDATE rework_claim SET state='RELEASED' WHERE seq=?", (record['claim_id'],))
    return {'refire': record['seq'], 'state': 'WITHDRAWN',
            'firing_id': source['firing_id'], 'car': source['car'], 'zone': source['zone']}


