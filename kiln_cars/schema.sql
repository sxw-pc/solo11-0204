PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS customer_order (
    id TEXT PRIMARY KEY, recipient TEXT NOT NULL, downgrade INTEGER NOT NULL CHECK(downgrade IN (0,1)),
    standard_cents INTEGER NOT NULL CHECK(standard_cents>0),
    reduced_cents INTEGER NOT NULL CHECK(reduced_cents>=0 AND reduced_cents<standard_cents)
);
CREATE TABLE IF NOT EXISTS recipe (
    id TEXT PRIMARY KEY, body TEXT NOT NULL, color TEXT NOT NULL, atmosphere TEXT NOT NULL,
    emission TEXT NOT NULL, sensitive_to TEXT NOT NULL, low INTEGER NOT NULL, high INTEGER NOT NULL CHECK(high>=low)
);
CREATE TABLE IF NOT EXISTS piece (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES customer_order(id), recipe_id TEXT NOT NULL REFERENCES recipe(id)
);
CREATE TABLE IF NOT EXISTS firing (
    id TEXT PRIMARY KEY, atmosphere TEXT NOT NULL, ticks INTEGER NOT NULL CHECK(ticks>0),
    logger_owner TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('OPEN','LOCKED'))
);
CREATE TABLE IF NOT EXISTS kiln_slot (
    firing_id TEXT REFERENCES firing(id), car TEXT NOT NULL, zone TEXT NOT NULL, setpoint INTEGER NOT NULL,
    PRIMARY KEY(firing_id,car,zone)
);
CREATE TABLE IF NOT EXISTS placement (
    piece_id TEXT PRIMARY KEY REFERENCES piece(id), firing_id TEXT NOT NULL, car TEXT NOT NULL, zone TEXT NOT NULL,
    UNIQUE(firing_id,car,zone), FOREIGN KEY(firing_id,car,zone) REFERENCES kiln_slot(firing_id,car,zone)
);
CREATE TABLE IF NOT EXISTS trace (
    seq INTEGER PRIMARY KEY, firing_id TEXT NOT NULL, car TEXT NOT NULL, zone TEXT NOT NULL,
    tick INTEGER NOT NULL CHECK(tick>=0), temperature INTEGER NOT NULL, document TEXT NOT NULL,
    UNIQUE(firing_id,car,zone,tick), FOREIGN KEY(firing_id,car,zone) REFERENCES kiln_slot(firing_id,car,zone)
);
CREATE TABLE IF NOT EXISTS witness (
    seq INTEGER PRIMARY KEY, coupon TEXT UNIQUE NOT NULL, firing_id TEXT NOT NULL, car TEXT NOT NULL, zone TEXT NOT NULL,
    first_tick INTEGER NOT NULL, last_tick INTEGER NOT NULL CHECK(last_tick>=first_tick),
    low INTEGER NOT NULL, high INTEGER NOT NULL CHECK(high>=low), observer TEXT NOT NULL,
    FOREIGN KEY(firing_id,car,zone) REFERENCES kiln_slot(firing_id,car,zone)
);
CREATE TABLE IF NOT EXISTS assessment (
    seq INTEGER PRIMARY KEY, piece_id TEXT NOT NULL REFERENCES placement(piece_id),
    trace_asof INTEGER NOT NULL, witness_asof INTEGER NOT NULL, surface TEXT NOT NULL,
    thermal TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rework_slot (
    id TEXT PRIMARY KEY, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rework_claim (
    seq INTEGER PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES rework_slot(id),
    assessment_id INTEGER NOT NULL UNIQUE REFERENCES assessment(seq),
    state TEXT NOT NULL CHECK(state IN ('HELD','RELEASED','USED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS held_slot ON rework_claim(slot_id) WHERE state='HELD';
CREATE TABLE IF NOT EXISTS dispatch (
    id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES customer_order(id)
);
CREATE TABLE IF NOT EXISTS settlement (
    piece_id TEXT PRIMARY KEY REFERENCES piece(id), dispatch_id TEXT NOT NULL REFERENCES dispatch(id),
    assessment_id INTEGER NOT NULL REFERENCES assessment(seq), cents INTEGER NOT NULL CHECK(cents>=0)
);

CREATE TABLE IF NOT EXISTS material_carton (
    id TEXT PRIMARY KEY, lot TEXT, units INTEGER NOT NULL CHECK(units>0), document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_segment (
    id TEXT PRIMARY KEY, carton TEXT NOT NULL REFERENCES material_carton,
    units INTEGER NOT NULL CHECK(units>0), lot TEXT, document TEXT
);
CREATE TABLE IF NOT EXISTS material_pick (
    id TEXT PRIMARY KEY, carton TEXT NOT NULL REFERENCES material_carton,
    firing_id TEXT, car TEXT, zone TEXT, units INTEGER NOT NULL CHECK(units>0),
    picked_at TEXT NOT NULL, reissue INTEGER NOT NULL DEFAULT 0 CHECK(reissue IN (0,1)),
    FOREIGN KEY(firing_id,car,zone) REFERENCES kiln_slot(firing_id,car,zone)
);
CREATE TABLE IF NOT EXISTS material_return (id TEXT PRIMARY KEY, pick TEXT NOT NULL REFERENCES material_pick, units INTEGER NOT NULL CHECK(units>0));
CREATE TABLE IF NOT EXISTS material_lot_proof (
    seq INTEGER PRIMARY KEY, document TEXT NOT NULL, carton TEXT NOT NULL REFERENCES material_carton,
    lot TEXT NOT NULL, units INTEGER NOT NULL CHECK(units>0),
    pick TEXT REFERENCES material_pick, kind TEXT NOT NULL CHECK(kind IN ('composition','located'))
);
CREATE UNIQUE INDEX IF NOT EXISTS proof_carton_doc_lot ON material_lot_proof(carton,document,lot,pick);
CREATE TABLE IF NOT EXISTS quarantine_position (
    id TEXT PRIMARY KEY, capacity INTEGER NOT NULL CHECK(capacity>=0)
);
CREATE TABLE IF NOT EXISTS quarantine_reservation (
    id TEXT PRIMARY KEY, position TEXT NOT NULL REFERENCES quarantine_position,
    carton TEXT NOT NULL REFERENCES material_carton, units INTEGER NOT NULL CHECK(units>0),
    state TEXT NOT NULL CHECK(state IN ('RESERVED','QUARANTINED','VERIFIED')),
    pick TEXT REFERENCES material_pick
);
CREATE TABLE IF NOT EXISTS material_hold (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('zone','carton','quarantine')),
    firing_id TEXT, car TEXT, zone TEXT, carton TEXT, position TEXT,
    reason TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('FROZEN','RELEASED')),
    group_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS hold_zone ON material_hold(firing_id,car,zone) WHERE kind='zone' AND state='FROZEN';
CREATE UNIQUE INDEX IF NOT EXISTS hold_carton ON material_hold(carton) WHERE kind='carton' AND state='FROZEN';
CREATE UNIQUE INDEX IF NOT EXISTS hold_quarantine ON material_hold(position) WHERE kind='quarantine' AND state='FROZEN';
