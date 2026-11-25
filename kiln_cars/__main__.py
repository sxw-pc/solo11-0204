import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from . import audit, energy, evidence, loading, meter, release, settlement
from .db import Refused, connect, transaction


ACTIONS = {
    'order': loading.order, 'recipe': loading.recipe, 'piece': loading.piece,
    'firing': loading.firing, 'slot': loading.slot, 'place': loading.place,
    'unload': loading.unload, 'lock': loading.lock, 'trace': evidence.capture_trace,
    'witness': evidence.capture_witness, 'zone': evidence.zone_status,
    'rework-slot': release.add_rework_slot, 'assess': release.assess,
    'reserve': release.reserve, 'finish-rework': release.finish,
    'dispatch': settlement.dispatch, 'balance': settlement.balance,
    'energy-meter': meter.register, 'energy-load': meter.register_load,
    'energy-reading': meter.capture, 'energy-master': meter.capture_master_voucher,
    'energy-statement': energy.statement,
}


def apply(db, action, arguments):
    if action not in ACTIONS or not isinstance(arguments, dict):
        raise Refused('unknown action or invalid argument object')
    return ACTIONS[action](db, **arguments)


def run_fixture(db, path):
    steps = json.loads(Path(path).read_text(encoding='utf-8'))
    results = []
    for step in steps:
        label = step.get('label', step['action'])
        expected = step.get('refused')
        try:
            with transaction(db):
                result = apply(db, step['action'], step['args'])
                if expected:
                    raise RuntimeError(f'{label}: expected refusal {expected}')
        except (Refused, sqlite3.IntegrityError) as error:
            if not expected or expected not in str(error):
                raise
            result = {'refused': str(error)}
        results.append({'step': label, 'result': result})
    return results


def main():
    parser = argparse.ArgumentParser(description='Shared ceramic firing acceptance ledger')
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    action = commands.add_parser('action')
    action.add_argument('name', choices=ACTIONS)
    action.add_argument('arguments', help='JSON object')
    commands.add_parser('audit', aliases=['recompute'])
    demo = commands.add_parser('demo')
    demo.add_argument('--fixture', type=Path, default=Path(__file__).resolve().parents[1] / 'fixtures' / 'shared.json')
    args = parser.parse_args()
    readonly = args.command in ('audit', 'recompute')
    try:
        with closing(connect(args.db, readonly=readonly)) as db:
            if args.command == 'demo':
                result = run_fixture(db, args.fixture)
            else:
                with transaction(db, write=not readonly):
                    result = (apply(db, args.name, json.loads(args.arguments)) if args.command == 'action'
                              else audit.recompute(db))
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return int(isinstance(result, dict) and result.get('status') == 'AUDIT_MISMATCH')
    except (ValueError, TypeError, AttributeError, sqlite3.Error, OSError, RuntimeError) as error:
        parser.exit(2, f'{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())

