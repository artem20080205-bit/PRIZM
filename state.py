"""Durable transfer IDs and deferred cancellations. No access tokens are stored."""
from pathlib import Path
import sqlite3
from contextlib import contextmanager
import uuid

DEFAULT = Path(__file__).resolve().parent / 'client_state.sqlite'

class TransferState:
    def __init__(self,path=DEFAULT):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(profile TEXT,signature TEXT,id TEXT,PRIMARY KEY(profile,signature));
            CREATE TABLE IF NOT EXISTS cancellations(profile TEXT,id TEXT,PRIMARY KEY(profile,id));''')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            with db:yield db
        finally:db.close()
    def session(self,profile,signature):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO sessions VALUES(?,?,?)',(profile,signature,str(uuid.uuid4())))
            return db.execute('SELECT id FROM sessions WHERE profile=? AND signature=?',(profile,signature)).fetchone()[0]
    def forget(self,profile,key):
        with self.connect() as db:db.execute('DELETE FROM sessions WHERE profile=? AND id=?',(profile,key))
    def cancel(self,profile,key):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO cancellations VALUES(?,?)',(profile,key))
            db.execute('DELETE FROM sessions WHERE profile=? AND id=?',(profile,key))
    def pending(self,profile):
        with self.connect() as db:return [row[0] for row in db.execute('SELECT id FROM cancellations WHERE profile=?',(profile,))]
    def acknowledged(self,profile,key):
        with self.connect() as db:db.execute('DELETE FROM cancellations WHERE profile=? AND id=?',(profile,key))
