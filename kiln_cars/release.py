from . import evidence, loading
from .db import Refused, insert, one


HISTORICAL_RISK_REASON = (
    'measured thermal excursion in an earlier firing is irreversible; the factory has not '
    'agreed that a refire may release this historical heat risk'
)


def add_rework_slot(db, id, body):
    insert(db, 'rework_slot', id=id, body=body)


def decision(db, position, thermal, surface):
    consent = one(db, 'SELECT downgrade FROM customer_order WHERE id=?', (position['order_id'],))[0]
    if surface not in ('clean', 'cosmetic', 'repairable', 'broken'):
        raise Refused('unknown inspection surface')
    if surface == 'broken' or thermal == 'OUTSIDE_WINDOW':
        return 'REJECTED', 'terminal damage or measured thermal excursion'
    if thermal == 'EVIDENCE_GAP':
        return 'HOLD', 'unresolved zonal evidence'
    if surface == 'clean':
        return 'STANDARD', 'thermal and surface acceptance'
    if surface == 'cosmetic' and consent:
        return 'DOWNGRADED', 'customer accepts cosmetic grading'
    return 'REWORK_QUEUE', 'surface needs repair or downgrade consent absent'


def latest(db, piece_id):
    return one(db, 'SELECT * FROM assessment WHERE piece_id=? ORDER BY seq DESC LIMIT 1', (piece_id,))


def heat_exposures(db, piece_id, cutoffs=None):
    """Every real firing this piece has actually taken, oldest first, with the
    thermal verdict each position supports and the evidence cutoffs that decide
    it. Withdrawn or unfinished refire attempts are not heat.

    ``cutoffs`` maps a (firing_id, car, zone) position to the (trace_asof,
    witness_asof) tuple that was visible when a historical decision was taken;
    positions without an entry are evaluated at their current evidence."""
    placement = one(db, 'SELECT * FROM placement WHERE piece_id=?', (piece_id,))
    refires = list(db.execute(
        "SELECT * FROM refire WHERE piece_id=? AND state='FIRED' ORDER BY seq", (piece_id,)))
    if not refires:
        keys = [evidence.location_key(placement)]
    else:
        first = one(db, 'SELECT * FROM assessment WHERE seq=?',
                    (refires[0]['source_assessment_id'],))
        keys = [(first['firing_id'], first['car'], first['zone'])]
        keys += [(r['firing_id'], r['car'], r['zone']) for r in refires]
    keys = list(dict.fromkeys(keys))
    profile = one(db, 'SELECT r.low, r.high FROM piece p JOIN recipe r ON r.id=p.recipe_id '
                      'WHERE p.id=?', (piece_id,))
    exposures = []
    for index, key in enumerate(keys):
        firing_id, car, zone = key
        slot = evidence.slot_position(db, firing_id, car, zone)
        cutoff = cutoffs.get(key) if cutoffs is not None else None
        if cutoff is None:
            cutoff = evidence.versions_at(db, firing_id, car, zone)
        position = dict(slot)
        position.update({'low': profile['low'], 'high': profile['high']})
        thermal = evidence.temperature_decision(db, position, cutoff)
        exposures.append({'heat_no': index + 1, 'firing_id': firing_id, 'car': car, 'zone': zone,
                          'trace_asof': cutoff[0], 'witness_asof': cutoff[1], 'thermal': thermal})
    return exposures


def historical_excursion(db, piece_id, cutoffs=None):
    """Measured OUTSIDE_WINDOW evidence in any actual heat exposure, including
    the piece's current position. Witness-coupon coverage and evidence gaps do
    not count: only a real logger measurement past the body window."""
    return [exposure for exposure in heat_exposures(db, piece_id, cutoffs)
            if exposure['thermal'] == 'OUTSIDE_WINDOW']


def delivery_blocked_by_history(db, piece_id):
    excursions = historical_excursion(db, piece_id)
    if not excursions:
        return None
    exposure = excursions[-1]
    return (f"historical heat risk: measured excursion in {exposure['firing_id']} "
            f"{exposure['car']}/{exposure['zone']} (heat {exposure['heat_no']})")


def unmet_repair(db, piece_id, before=None):
    obligation = db.execute('''SELECT c.state FROM assessment a LEFT JOIN rework_claim c ON c.assessment_id=a.seq
                               WHERE a.piece_id=? AND a.decision='REWORK_QUEUE' AND (? IS NULL OR a.seq<?)
                               ORDER BY a.seq DESC LIMIT 1''', (piece_id, before, before)).fetchone()
    return obligation is not None and obligation['state'] != 'USED'


def assess(db, piece_id, surface):
    if db.execute('SELECT 1 FROM settlement WHERE piece_id=?', (piece_id,)).fetchone():
        raise Refused('delivered piece has a frozen decision')
    if loading.open_refire(db, piece_id) is not None:
        raise Refused('finish or withdraw the unfinished refire before reassessment')
    if db.execute('''SELECT 1 FROM rework_claim c JOIN assessment a ON a.seq=c.assessment_id
                     WHERE a.piece_id=? AND c.state='HELD' ''', (piece_id,)).fetchone():
        raise Refused('resolve the occupied rework position before reassessment')
    position = evidence.location(db, piece_id)
    cutoff = evidence.versions(db, position)
    thermal = evidence.temperature_decision(db, position, cutoff)
    grade, reason = decision(db, position, thermal, surface)
    if grade in ('STANDARD', 'DOWNGRADED') and unmet_repair(db, piece_id):
        raise Refused('repair work must be recorded before a deliverable reassessment')
    if grade in ('STANDARD', 'DOWNGRADED') and historical_excursion(db, piece_id):
        grade, reason = 'HOLD', HISTORICAL_RISK_REASON
    seq = insert(db, 'assessment', piece_id=piece_id, firing_id=position['firing_id'],
                 car=position['car'], zone=position['zone'], trace_asof=cutoff[0],
                 witness_asof=cutoff[1], surface=surface, thermal=thermal, decision=grade,
                 reason=reason)
    return {'assessment': seq, 'decision': grade}


def is_current(db, assessment):
    position = evidence.slot_position(db, assessment['firing_id'], assessment['car'],
                                      assessment['zone'])
    return evidence.versions(db, position) == (assessment['trace_asof'], assessment['witness_asof'])


def reserve(db, piece_id):
    if loading.open_refire(db, piece_id) is not None:
        raise Refused('finish or withdraw the unfinished refire before reserving')
    assessment = latest(db, piece_id)
    if assessment['decision'] != 'REWORK_QUEUE' or not is_current(db, assessment):
        raise Refused('no current rework-eligible assessment')
    if db.execute("SELECT 1 FROM rework_claim WHERE assessment_id=? AND state='HELD'",
                  (assessment['seq'],)).fetchone():
        raise Refused('assessment has already claimed a position')
    body = evidence.location(db, piece_id)['body']
    slot = db.execute('''SELECT s.id FROM rework_slot s WHERE s.body=? AND NOT EXISTS
                         (SELECT 1 FROM rework_claim c WHERE c.slot_id=s.id AND c.state='HELD')
                         ORDER BY s.id LIMIT 1''', (body,)).fetchone()
    if slot is None:
        raise Refused('no compatible free rework position')
    insert(db, 'rework_claim', slot_id=slot[0], assessment_id=assessment['seq'], state='HELD')
    return slot[0]


def finish(db, piece_id, completed):
    if type(completed) is not bool:
        raise Refused('completed must be boolean')
    if loading.open_refire(db, piece_id) is not None:
        raise Refused('the held reservation is bound to an unfinished refire; finish or withdraw it')
    assessment = latest(db, piece_id)
    claim = one(db, "SELECT * FROM rework_claim WHERE assessment_id=? AND state='HELD'", (assessment['seq'],))
    if completed and not is_current(db, assessment):
        raise Refused('new evidence requires release and a fresh assessment')
    state = 'USED' if completed else 'RELEASED'
    db.execute('UPDATE rework_claim SET state=? WHERE seq=?', (state, claim['seq']))
    return state


