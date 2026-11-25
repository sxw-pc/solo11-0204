from . import evidence, loading, release, settlement


def _position(db, assessment):
    slot = evidence.slot_position(db, assessment['firing_id'], assessment['car'],
                                  assessment['zone'])
    profile = one_profile(db, assessment['piece_id'])
    position = dict(slot)
    position.update(profile)
    return position


def one_profile(db, piece_id):
    row = db.execute('''SELECT p.id AS piece_id, p.order_id, r.low, r.high, r.body
                        FROM piece p JOIN recipe r ON r.id=p.recipe_id
                        WHERE p.id=?''', (piece_id,)).fetchone()
    return dict(row)


def recompute(db):
    errors = []
    positions = list(db.execute('SELECT * FROM placement ORDER BY piece_id'))
    for a in positions:
        slot = db.execute('SELECT * FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?',
                          evidence.location_key(a)).fetchone()
        profile = loading.load_profile(db, a['piece_id'])
        if loading.compatibility(db, a['firing_id'], profile, slot) != 'COMPATIBLE':
            errors.append(f"incompatible placement {a['piece_id']}")
    history = {}
    for a in db.execute('SELECT * FROM assessment ORDER BY seq'):
        position = _position(db, a)
        thermal = evidence.temperature_decision(db, position, (a['trace_asof'], a['witness_asof']))
        grade, reason = release.decision(db, position, thermal, a['surface'])
        if grade in ('STANDARD', 'DOWNGRADED') and release.unmet_repair(db, a['piece_id'], a['seq']):
            errors.append(f"unresolved repair before assessment {a['seq']}")
        cutoffs = {(a['firing_id'], a['car'], a['zone']):
                   (a['trace_asof'], a['witness_asof'])}
        if grade in ('STANDARD', 'DOWNGRADED') and release.historical_excursion(
                db, a['piece_id'], cutoffs):
            grade, reason = 'HOLD', release.HISTORICAL_RISK_REASON
        history[a['seq']] = (a['piece_id'], grade)
        if (thermal, grade, reason) != (a['thermal'], a['decision'], a['reason']):
            errors.append(f"assessment mismatch {a['seq']}")
    for row in db.execute('''SELECT c.*, a.piece_id, s.body FROM rework_claim c
                             JOIN assessment a ON a.seq=c.assessment_id JOIN rework_slot s ON s.id=c.slot_id'''):
        if (history[row['assessment_id']][1] != 'REWORK_QUEUE' or
                evidence.location(db, row['piece_id'])['body'] != row['body']):
            errors.append(f"invalid rework claim {row['seq']}")
    for row in db.execute('SELECT * FROM refire ORDER BY seq'):
        label = f"refire {row['seq']} on {row['piece_id']}"
        source = db.execute('SELECT * FROM assessment WHERE seq=?',
                            (row['source_assessment_id'],)).fetchone()
        claim = db.execute('SELECT * FROM rework_claim WHERE seq=?', (row['claim_id'],)).fetchone()
        slot = db.execute('SELECT * FROM kiln_slot WHERE firing_id=? AND car=? AND zone=?',
                          (row['firing_id'], row['car'], row['zone'])).fetchone()
        if source is None or source['piece_id'] != row['piece_id']:
            errors.append(f"{label}: missing source assessment")
        elif source['decision'] != 'REWORK_QUEUE':
            errors.append(f"{label}: source assessment is not rework-queued")
        if claim is None or claim['seq'] != row['claim_id']:
            errors.append(f"{label}: missing rework reservation")
        elif claim['assessment_id'] != row['source_assessment_id']:
            errors.append(f"{label}: reservation is not bound to the source assessment")
        if slot is None:
            errors.append(f"{label}: destination kiln position is unknown")
        else:
            profile = loading.load_profile(db, row['piece_id'])
            if loading.compatibility(db, row['firing_id'], profile, slot) != 'COMPATIBLE':
                errors.append(f"{label}: destination is incompatible with the piece body")
        batch = db.execute('SELECT state FROM firing WHERE id=?', (row['firing_id'],)).fetchone()
        if batch is None:
            errors.append(f"{label}: destination firing is unknown")
        elif row['state'] == 'FIRED' and batch['state'] != 'LOCKED':
            errors.append(f"{label}: fired refire never reached a locked batch")
        if row['state'] == 'FIRED':
            if claim is None or claim['state'] != 'USED':
                errors.append(f"{label}: fired refire did not release the rework position")
        if row['state'] == 'WITHDRAWN' and claim is not None and claim['state'] != 'RELEASED':
            errors.append(f"{label}: withdrawn refire kept the rework position held")
        if row['state'] == 'PLACED' and claim is not None and claim['state'] != 'HELD':
            errors.append(f"{label}: open refire lost its held reservation")
    manifests = []
    late = []
    settled_pieces = set()
    for row in db.execute('SELECT * FROM dispatch ORDER BY id'):
        result = settlement.manifest(db, row['id'])
        order = db.execute('SELECT * FROM customer_order WHERE id=?', (row['order_id'],)).fetchone()
        if not result['items']:
            errors.append(f"empty manifest {row['id']}")
        for item in result['items']:
            piece_id, grade = history[item['assessment_id']]
            assessment = release.latest(db, item['piece_id'])
            position = evidence.location(db, item['piece_id'])
            if (piece_id != item['piece_id'] or position['order_id'] != row['order_id'] or
                    assessment['seq'] != item['assessment_id'] or grade not in ('STANDARD', 'DOWNGRADED') or
                    settlement.piece_price(order, grade) != item['cents']):
                errors.append(f"settlement mismatch {item['piece_id']}")
            if not release.is_current(db, assessment):
                late.append({'piece': item['piece_id'], 'current_thermal':
                             evidence.temperature_decision(db, position)})
            settled_pieces.add(item['piece_id'])
        manifests.append(result)
    for piece_id in sorted(settled_pieces):
        if piece_id in {entry['piece'] for entry in late}:
            continue
        for exposure in release.historical_excursion(db, piece_id):
            late.append({'piece': piece_id, 'heat_no': exposure['heat_no'],
                         'firing_id': exposure['firing_id'], 'car': exposure['car'],
                         'zone': exposure['zone'], 'current_thermal': exposure['thermal']})
    return {'status': 'AUDIT_MISMATCH' if errors else 'AUDIT_OK', 'errors': errors,
            'manifests': manifests, 'late_evidence': late}
