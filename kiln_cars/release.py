from . import evidence
from .db import Refused, insert, one


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


def unmet_repair(db, piece_id, before=None):
    obligation = db.execute('''SELECT c.state FROM assessment a LEFT JOIN rework_claim c ON c.assessment_id=a.seq
                               WHERE a.piece_id=? AND a.decision='REWORK_QUEUE' AND (? IS NULL OR a.seq<?)
                               ORDER BY a.seq DESC LIMIT 1''', (piece_id, before, before)).fetchone()
    return obligation is not None and obligation['state'] != 'USED'


def assess(db, piece_id, surface):
    if db.execute('SELECT 1 FROM settlement WHERE piece_id=?', (piece_id,)).fetchone():
        raise Refused('delivered piece has a frozen decision')
    if db.execute('''SELECT 1 FROM rework_claim c JOIN assessment a ON a.seq=c.assessment_id
                     WHERE a.piece_id=? AND c.state='HELD' ''', (piece_id,)).fetchone():
        raise Refused('resolve the occupied rework position before reassessment')
    position = evidence.location(db, piece_id)
    cutoff = evidence.versions(db, position)
    thermal = evidence.temperature_decision(db, position, cutoff)
    grade, reason = decision(db, position, thermal, surface)
    if grade in ('STANDARD', 'DOWNGRADED') and unmet_repair(db, piece_id):
        raise Refused('repair work must be recorded before a deliverable reassessment')
    seq = insert(db, 'assessment', piece_id=piece_id, trace_asof=cutoff[0], witness_asof=cutoff[1],
                 surface=surface, thermal=thermal, decision=grade, reason=reason)
    return {'assessment': seq, 'decision': grade}


def is_current(db, assessment):
    position = evidence.location(db, assessment['piece_id'])
    return evidence.versions(db, position) == (assessment['trace_asof'], assessment['witness_asof'])


def reserve(db, piece_id):
    assessment = latest(db, piece_id)
    if assessment['decision'] != 'REWORK_QUEUE' or not is_current(db, assessment):
        raise Refused('no current rework-eligible assessment')
    if db.execute('SELECT 1 FROM rework_claim WHERE assessment_id=?', (assessment['seq'],)).fetchone():
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
    assessment = latest(db, piece_id)
    claim = one(db, "SELECT * FROM rework_claim WHERE assessment_id=? AND state='HELD'", (assessment['seq'],))
    if completed and not is_current(db, assessment):
        raise Refused('new evidence requires release and a fresh assessment')
    state = 'USED' if completed else 'RELEASED'
    db.execute('UPDATE rework_claim SET state=? WHERE seq=?', (state, claim['seq']))
    return state


