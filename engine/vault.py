import json
import os
import sys
import time
import copy
import threading
import sqlite3
from engine import storage
HERE = os.environ.get('EC_HOME') or os.getcwd()
from engine import wire as P
from google.protobuf.json_format import MessageToDict, ParseDict
KEYS = json.load(open(storage.path('res', 'tablekeys.json', root=HERE)))
REKEY = ('user_highwind_battle_release_list', 'user_daily_quest_list')
TABLES = P.Command_Entity_Tables.DESCRIPTOR
ROW_TYPE = {f.name: f.message_type for f in TABLES.fields}
FIELD_BY_TYPE = {f.message_type.name: f.name for f in TABLES.fields}

def row_key(table, row):
    ks = KEYS.get(table) or ['user_id']
    if isinstance(row, dict):
        return tuple((row.get(k) for k in ks))
    return tuple((getattr(row, k) for k in ks))

def _is_repeated(f):
    return f.is_repeated if hasattr(f, 'is_repeated') else f.label == f.LABEL_REPEATED

def rewrite_ids(msg, old, new):
    old, new = (int(old), int(new))
    n = 0
    for f, v in msg.ListFields():
        if f.type == f.TYPE_MESSAGE:
            if f.message_type.GetOptions().map_entry:
                vf = f.message_type.fields_by_name['value']
                if vf.type == vf.TYPE_MESSAGE:
                    for x in v.values():
                        n += rewrite_ids(x, old, new)
                else:
                    for k in list(v.keys()):
                        if v[k] == old:
                            v[k] = new
                            n += 1
            elif _is_repeated(f):
                for x in v:
                    n += rewrite_ids(x, old, new)
            else:
                n += rewrite_ids(v, old, new)
        elif f.cpp_type in (f.CPPTYPE_INT64, f.CPPTYPE_UINT64):
            if _is_repeated(f):
                for i, x in enumerate(v):
                    if x == old:
                        v[i] = new
                        n += 1
            elif v == old:
                setattr(msg, f.name, new)
                n += 1
    return n

def rewrite_ids_bytes(plain, old, new):
    m = P.Command_ApiNetwork_Api_ApiResponse()
    m.ParseFromString(plain)
    return m.SerializeToString() if rewrite_ids(m, old, new) else plain
def create_account(folder, user_id, origin, extra=None):
    from engine.new_account import build
    now = time.time()
    raw = build(user_id, now_ms=int(now * 1000)).SerializeToString()
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'base.pb'), 'wb') as stream:
        stream.write(raw)
    meta = {'player_id': str(user_id), 'origin': origin, 'calls': 0, 'endpoints': 0, 'first_seen': now, 'last_seen': now, 'sources': [], 'account_bytes': len(raw), 'exported': now}
    meta.update(extra or {})
    with open(os.path.join(folder, 'info.json'), 'w') as stream:
        json.dump(meta, stream, indent=1)
    return meta

class _Tracked(dict):
    __slots__ = ('_dirty', '_t')

    def __init__(self, rows, dirty, table):
        dict.__init__(self, rows)
        self._dirty = dirty
        self._t = table

    def __setitem__(self, k, v):
        self._dirty.add((self._t, k))
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._dirty.add((self._t, k))
        dict.__delitem__(self, k)

    def pop(self, k, *d):
        self._dirty.add((self._t, k))
        return dict.pop(self, k, *d)

    def popitem(self):
        k, v = dict.popitem(self)
        self._dirty.add((self._t, k))
        return (k, v)

    def setdefault(self, k, d=None):
        if k not in self:
            self._dirty.add((self._t, k))
        return dict.setdefault(self, k, d)

    def update(self, *a, **kw):
        tmp = dict(*a, **kw)
        for k in tmp:
            self._dirty.add((self._t, k))
        dict.update(self, tmp)

    def clear(self):
        for k in self:
            self._dirty.add((self._t, k))
        dict.clear(self)
DB = 'save.db'
FULL_EVERY_SAVES, FULL_EVERY_S = (200, 30)
ROW_CLASS = {f.name: f.message_type._concrete_class for f in TABLES.fields}
PLAY_FILES = (DB, DB + '-wal', DB + '-shm', 'state.json')

def has_play_state(folder):
    return any((os.path.isfile(os.path.join(folder, f)) for f in (DB, 'state.json')))

def play_state_mtime(folder):
    ts = [os.path.getmtime(os.path.join(folder, f)) for f in PLAY_FILES if os.path.isfile(os.path.join(folder, f))]
    return max(ts) if ts else None

def remove_play_state(folder):
    out = []
    for f in PLAY_FILES:
        fp = os.path.join(folder, f)
        if os.path.exists(fp):
            os.remove(fp)
            out.append(f)
    return out

def read_summary(folder):
    fp = os.path.join(folder, DB)
    if not os.path.isfile(fp):
        return None
    try:
        c = sqlite3.connect(f'file:{fp}?mode=ro', uri=True, timeout=5)
        try:
            row = c.execute("SELECT val FROM meta WHERE key='summary'").fetchone()
        finally:
            c.close()
        return json.loads(row[0]) if row and row[0] else None
    except sqlite3.Error:
        return None
SHARED = 'common.db'
LEGACY = {'damage': 'damage_scores.json', 'crisis': 'crisis_scores.json', 'social': 'social.json', 'display_ids': 'friend_ids.json'}
_SH = {}
_SH_LOCK = threading.RLock()

def _shared(acc):
    p = os.path.realpath(os.path.join(acc, SHARED))
    c = _SH.get(p)
    if c is None:
        c = sqlite3.connect(p, timeout=30, check_same_thread=False, isolation_level=None)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('CREATE TABLE IF NOT EXISTS kv(name TEXT PRIMARY KEY, val TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS board(name TEXT NOT NULL, uid TEXT NOT NULL, val TEXT NOT NULL, PRIMARY KEY(name, uid)) WITHOUT ROWID')
        _SH[p] = c
    return c

def _migrate(acc, c, name, board):
    if c.execute('SELECT 1 FROM kv WHERE name=?', ('migrated:' + name,)).fetchone():
        return
    fp = os.path.join(acc, LEGACY[name])
    if os.path.isfile(fp):
        d = json.load(open(fp))
        if board:
            c.executemany('INSERT OR IGNORE INTO board VALUES(?,?,?)', [(name, str(u), json.dumps(v)) for u, v in d.items()])
        else:
            c.execute('INSERT OR IGNORE INTO kv VALUES(?,?)', (name, json.dumps(d)))
    c.execute('INSERT INTO kv VALUES(?,?)', ('migrated:' + name, str(time.time())))
    if os.path.isfile(fp):
        os.replace(fp, fp + '.pre-sqlite')
_DONE = set()

def _tx(acc, name, board, fn, write):
    with _SH_LOCK:
        c = _shared(acc)
        key = (os.path.realpath(acc), name)
        if key not in _DONE:
            c.execute('BEGIN IMMEDIATE')
            try:
                _migrate(acc, c, name, board)
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
            _DONE.add(key)
        if not write:
            return fn(c)
        c.execute('BEGIN IMMEDIATE')
        try:
            r = fn(c)
            c.execute('COMMIT')
            return r
        except BaseException:
            c.execute('ROLLBACK')
            raise

def board_all(acc, name):
    return _tx(acc, name, True, lambda c: {u: json.loads(v) for u, v in c.execute('SELECT uid, val FROM board WHERE name=?', (name,))}, False)

def board_put(acc, name, uid, value):
    _tx(acc, name, True, lambda c: c.execute('INSERT OR REPLACE INTO board VALUES(?,?,?)', (name, str(uid), json.dumps(value))), True)

def kv_get(acc, name, default):

    def f(c):
        r = c.execute('SELECT val FROM kv WHERE name=?', (name,)).fetchone()
        return json.loads(r[0]) if r else default
    return _tx(acc, name, False, f, False)

def kv_set(acc, name, value):
    _tx(acc, name, False, lambda c: c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (name, json.dumps(value))), True)

def _kenc(k):
    return json.dumps(list(k))

def _kdec(s):
    return tuple(json.loads(s))

class State:

    def __init__(self, user_id, tables, other_info=None, folder=None):
        self.user_id = user_id
        self._dirty = set()
        self._conn = None
        self._dv = None
        self._disk = None
        self._full = True
        self._saves = 0
        self._full_t = 0.0
        self.tables = {t: _Tracked(rows, self._dirty, t) for t, rows in tables.items()}
        self.other_info = other_info
        self.folder = folder
        self.log = []
        self.mtime = None
        self.origin = 'archived'
        self.lock = threading.RLock()

    @classmethod
    def load(cls, folder):
        sj = os.path.join(folder, 'state.json')
        mp = os.path.join(folder, 'info.json')
        meta = json.load(open(mp)) if os.path.isfile(mp) else {}
        if os.path.isfile(os.path.join(folder, DB)):
            st = cls._load_db(folder)
            if st is not None:
                st.origin = meta.get('origin', 'archived')
                return st
        if os.path.isfile(sj):
            st = cls.from_json(json.load(open(sj, encoding='utf-8')), folder)
            st.mtime = os.path.getmtime(sj)
            st.origin = meta.get('origin', 'archived')
            return st
        raw = open(os.path.join(folder, 'base.pb'), 'rb').read()
        m = P.Command_ApiNetwork_Api_ApiResponse()
        m.ParseFromString(raw)
        user = m.common.user
        tables = {}
        for f in TABLES.fields:
            rows = getattr(user.update, f.name)
            tables[f.name] = {row_key(f.name, r): r for r in rows}
        st = cls(int(meta['player_id']), tables, user.other_info, folder)
        st.origin = meta.get('origin', 'archived')
        return st

    @staticmethod
    def _connect(folder):
        c = sqlite3.connect(os.path.join(folder, DB), timeout=30, check_same_thread=False, isolation_level=None)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('CREATE TABLE IF NOT EXISTS rows(tbl TEXT NOT NULL, k TEXT NOT NULL, pb BLOB NOT NULL, PRIMARY KEY(tbl, k)) WITHOUT ROWID')
        c.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, val BLOB)')
        return c

    @classmethod
    def _load_db(cls, folder):
        c = cls._connect(folder)
        c.execute('BEGIN')
        meta = dict(c.execute('SELECT key, val FROM meta'))
        if 'user_id' not in meta:
            c.execute('COMMIT')
            c.close()
            return None
        tables = {f.name: {} for f in TABLES.fields}
        disk = {}
        packed = c.execute('SELECT group_concat(tbl || char(29) || k || char(30) || hex(pb), char(31)) FROM rows').fetchone()[0]
        for item in packed.split('\x1f') if packed else ():
            t, rest = item.split('\x1d', 1)
            if t not in tables:
                continue
            k, h = rest.split('\x1e', 1)
            pb = bytes.fromhex(h)
            key = _kdec(k)
            r = ROW_CLASS[t]()
            r.ParseFromString(pb)
            tables[t][key] = r
            disk[t, key] = pb
        dv = c.execute('PRAGMA data_version').fetchone()[0]
        c.execute('COMMIT')
        oi = None
        if meta.get('other_info') is not None:
            oi = P.Command_Entity_UserOtherInfo()
            oi.ParseFromString(meta['other_info'])
        rekeyed = 0
        for t in REKEY:
            rows = tables.get(t) or {}
            for k0, r in list(rows.items()):
                k1 = row_key(t, r)
                if k1 != k0:
                    del rows[k0]
                    rows[k1] = r
                    rekeyed += 1
        st = cls(int(meta['user_id']), tables, oi, folder)
        st._conn = c
        st._dv = dv
        st._disk = disk
        st._full = bool(rekeyed)
        st._full_t = time.time()
        st.mtime = time.time()
        return st

    def to_json(self):
        cache = getattr(self, '_jcache', {})
        fresh = {}
        out = {'user_id': self.user_id, 'saved': time.time(), 'other_info': MessageToDict(self.other_info, preserving_proto_field_name=True) if self.other_info else None, 'tables': {t: [self._row_json(t, k, r, cache, fresh) for k, r in rows.items()] for t, rows in self.tables.items() if rows}}
        self._jcache = fresh
        return out

    def _row_json(self, t, k, r, cache, fresh):
        b = r.SerializeToString(deterministic=True)
        c = cache.get((t, k))
        if c is None or c[0] != b:
            c = (b, MessageToDict(r, preserving_proto_field_name=True))
        fresh[t, k] = c
        return c[1]

    @classmethod
    def from_json(cls, d, folder=None):
        tables = {}
        for f in TABLES.fields:
            rows = [ParseDict(x, f.message_type._concrete_class(), ignore_unknown_fields=True) for x in d['tables'].get(f.name, [])]
            tables[f.name] = {row_key(f.name, r): r for r in rows}
        oi = ParseDict(d['other_info'], P.Command_Entity_UserOtherInfo(), ignore_unknown_fields=True) if d.get('other_info') else None
        return cls(d['user_id'], tables, oi, folder)

    def save(self, folder=None):
        other = folder is not None and os.path.realpath(folder) != os.path.realpath(self.folder or folder)
        folder = folder or self.folder
        c = self._connect(folder) if other else self._conn or self._connect(folder)
        full = other or self._full or self._saves >= FULL_EVERY_SAVES or (time.time() - self._full_t > FULL_EVERY_S)
        c.execute('BEGIN IMMEDIATE')
        try:
            disk = None if other else self._disk
            if full:
                cur = {(t, k): r.SerializeToString() for t, rows in self.tables.items() for k, r in rows.items()}
                if disk is None:
                    c.execute(f"DELETE FROM rows WHERE tbl IN ({','.join('?' * len(ROW_CLASS))})", list(ROW_CLASS))
                    c.executemany('INSERT INTO rows VALUES(?,?,?)', [(t, _kenc(k), b) for (t, k), b in cur.items()])
                else:
                    dl = [(t, _kenc(k)) for t, k in disk if (t, k) not in cur]
                    up = [(t, _kenc(k), b) for (t, k), b in cur.items() if disk.get((t, k)) != b]
                    if dl:
                        c.executemany('DELETE FROM rows WHERE tbl=? AND k=?', dl)
                    if up:
                        c.executemany('INSERT OR REPLACE INTO rows VALUES(?,?,?)', up)
                disk = cur
            else:
                up, dl, done = ([], [], {})
                for t, k in list(self._dirty):
                    r = self.tables[t].get(k)
                    if r is None:
                        dl.append((t, _kenc(k)))
                        done[t, k] = None
                    else:
                        b = r.SerializeToString()
                        up.append((t, _kenc(k), b))
                        done[t, k] = b
                if dl:
                    c.executemany('DELETE FROM rows WHERE tbl=? AND k=?', dl)
                if up:
                    c.executemany('INSERT OR REPLACE INTO rows VALUES(?,?,?)', up)
            c.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [('user_id', str(self.user_id)), ('saved', str(time.time())), ('summary', json.dumps(self.summary())), ('other_info', self.other_info.SerializeToString() if self.other_info is not None else None)])
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise
        if other:
            c.close()
            return
        sj = os.path.join(folder, 'state.json')
        if full and os.path.isfile(sj):
            os.replace(sj, sj + '.pre-sqlite')
        if full:
            self._disk = disk
        elif self._disk is not None:
            for tk, b in done.items():
                if b is None:
                    self._disk.pop(tk, None)
                else:
                    self._disk[tk] = b
        self._conn = c
        self._dirty.clear()
        self._saves = 0 if full else self._saves + 1
        if full:
            self._full = False
            self._full_t = time.time()
        self._dv = c.execute('PRAGMA data_version').fetchone()[0]
        self.mtime = time.time()

    def summary(self):
        p = self.one('user_profile_list')
        s = self.one('user_status_list')
        d = {'name': p.name if p else '', 'exp': s.exp if s else 0, 'combat_power': max((x.combat_power for x in list(self.tables['user_party_list'].values())), default=0)}
        for k in ('top_honor_id', 'bottom_honor_id', 'background_honor_id', 'icon_honor_id'):
            d[k] = getattr(p, k) if p else 0
        return d

    def changed_on_disk(self):
        with self.lock:
            if self._conn is None:
                return False
            return self._conn.execute('PRAGMA data_version').fetchone()[0] != self._dv

    def export_json(self, path):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(json.dumps(self.to_json(), ensure_ascii=False, indent=0))
        os.replace(tmp, path)

    def __getitem__(self, table):
        return self.tables[table]

    def get(self, table, *key):
        return self.tables[table].get(tuple(key))

    def one(self, table):
        rows = self.tables[table]
        return next(iter(rows.values())) if rows else None

    def new_row(self, table, **fields):
        r = ROW_TYPE[table]._concrete_class()
        if 'user_id' in [f.name for f in ROW_TYPE[table].fields]:
            r.user_id = self.user_id
        for k, v in fields.items():
            setattr(r, k, v)
        return r

    def transaction(self, endpoint=''):
        return Transaction(self, endpoint)

    def full_tables(self):
        t = P.Command_Entity_Tables()
        for name, rows in self.tables.items():
            if rows:
                getattr(t, name).extend(rows.values())
        return t

class Transaction:

    def __init__(self, st, endpoint):
        self.st = st
        self.endpoint = endpoint
        self.up = P.Command_Entity_Tables()
        self.dl = P.Command_Entity_Tables()
        self.n_up = self.n_dl = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        if a[0] is None:
            self.st.log.append((time.time(), self.endpoint, self.n_up, self.n_dl))

    def upsert(self, table, row):
        k = row_key(table, row)
        self.st.tables[table][k] = row
        lst = getattr(self.up, table)
        for i, r in enumerate(lst):
            if row_key(table, r) == k:
                lst[i].CopyFrom(row)
                return row
        lst.append(row)
        self.n_up += 1
        return row

    def touch(self, table, row):
        return self.upsert(table, row)

    def delete(self, table, row):
        self.st.tables[table].pop(row_key(table, row), None)
        getattr(self.dl, table).append(row)
        self.n_dl += 1

    def common_response(self):
        c = P.Command_Entity_CommonResponse()
        c.user.update.CopyFrom(self.up)
        c.user.delete.CopyFrom(self.dl)
        if self.st.other_info:
            c.user.other_info.CopyFrom(self.st.other_info)
        return c
