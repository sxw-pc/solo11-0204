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

CREATE TABLE IF NOT EXISTS yard_track (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, length INTEGER NOT NULL CHECK(length > 0)
);
CREATE TABLE IF NOT EXISTS yard_cart (
    id TEXT PRIMARY KEY, node TEXT NOT NULL UNIQUE, length INTEGER NOT NULL CHECK(length > 0)
);
CREATE TABLE IF NOT EXISTS yard_move (
    id TEXT PRIMARY KEY, cart TEXT NOT NULL REFERENCES yard_cart, track TEXT NOT NULL REFERENCES yard_track,
    document TEXT NOT NULL
);
