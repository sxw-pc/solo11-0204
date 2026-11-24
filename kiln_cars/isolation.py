"""Find stock and kiln zones still holding parts of an identified lot."""

from .materials import available


def locate(db, lot):
    cartons = {}
    for row in db.execute('SELECT * FROM material_carton WHERE lot=? ORDER BY id',(lot,)):
        cartons[row['id']] = available(db,row['id'])
    zones = {}
    for row in db.execute('''SELECT p.id,p.firing_id,p.car,p.zone,p.units-COALESCE(SUM(r.units),0) AS units
                             FROM material_pick p JOIN material_carton c ON c.id=p.carton
                             LEFT JOIN material_return r ON r.pick=p.id
                             WHERE c.lot=? GROUP BY p.id ORDER BY p.id''',(lot,)):
        if row['units']:
            zones[row['id']] = {key:row[key] for key in ('firing_id','car','zone','units')}
    return {'lot':lot,'stock':cartons,'zones':zones}
