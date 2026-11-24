from . import evidence, loading, release, settlement


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
        position = evidence.location(db, a['piece_id'])
        thermal = evidence.temperature_decision(db, position, (a['trace_asof'], a['witness_asof']))
        grade, reason = release.decision(db, position, thermal, a['surface'])
        if grade in ('STANDARD', 'DOWNGRADED') and release.unmet_repair(db, a['piece_id'], a['seq']):
            errors.append(f"unresolved repair before assessment {a['seq']}")
        history[a['seq']] = (a['piece_id'], grade)
        if (thermal, grade, reason) != (a['thermal'], a['decision'], a['reason']):
            errors.append(f"assessment mismatch {a['seq']}")
    for row in db.execute('''SELECT c.*, a.piece_id, s.body FROM rework_claim c
                             JOIN assessment a ON a.seq=c.assessment_id JOIN rework_slot s ON s.id=c.slot_id'''):
        if (history[row['assessment_id']][1] != 'REWORK_QUEUE' or
                evidence.location(db, row['piece_id'])['body'] != row['body']):
            errors.append(f"invalid rework claim {row['seq']}")
    manifests = []
    late = []
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
                late.append({'piece': item['piece_id'], 'current_thermal': evidence.temperature_decision(db, position)})
        manifests.append(result)
    return {'status': 'AUDIT_MISMATCH' if errors else 'AUDIT_OK', 'errors': errors,
            'manifests': manifests, 'late_evidence': late}

