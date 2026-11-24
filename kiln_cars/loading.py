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


