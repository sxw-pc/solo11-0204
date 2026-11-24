from pathlib import Path
import sqlite3
from contextlib import contextmanager


class Refused(ValueError):
    pass


def connect(path, *, readonly=False):
    target = Path(path).resolve().as_uri() + '?mode=ro' if readonly else path
    db = sqlite3.connect(target, uri=readonly, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if not readonly:
        db.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
    return db


@contextmanager
def transaction(db, *, write=True):
    db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    else:
        db.commit()


def one(db, sql, args):
    row = db.execute(sql, args).fetchone()
    if row is None:
        raise Refused(f'unknown record: {args}')
    return row


def insert(db, table, **fields):
    columns = ','.join(fields)
    marks = ','.join('?' for _ in fields)
    return db.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', tuple(fields.values())).lastrowid

