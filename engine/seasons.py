"""Old season passes, one at a time (2026-10-11).

Each season pass shows in the game only inside its date windows: m_season_pass column 2 (and 4) names the condition set
of the pass itself, column 3 the set of its missions ("Exploration"), each a type 2 date window in m_condition (column 5
start, column 6 end); column 6 of m_season_pass is the season's end. The windows of every season have passed, so the
Season Pass button says "Outside season period". The launcher lets the player pick one season (game.json
"season_pass"); when the server starts, this module writes copies of m_condition and m_season_pass with that season's
ends moved to the year 3000 into gamedata\\season, with a catalog of its own, and the server serves those in place of
gamedata\\live. gamedata\\live is never changed, so picking None puts the game back as it was. Only one season: the
game has one Season Pass button and shows one season. The premium track needs no table: rules._season_premium_heal
gives every account each season's premium item."""
import base64, hashlib, json, os, struct
from engine import storage
from datetime import datetime, timezone

FAR = 32503680000000      # 3000-01-01, as the earlier event openings use
TABLES = ("m_condition", "m_season_pass")
INTS = {0xcc: ">B", 0xcd: ">H", 0xce: ">I", 0xcf: ">Q", 0xd0: ">b", 0xd1: ">h", 0xd2: ">i", 0xd3: ">q"}


def chosen(home):
    try: v = json.load(open(os.path.join(home, "game.json"), encoding="utf-8-sig")).get("season_pass") or 0
    except (OSError, ValueError): return 0
    try: return int(v)
    except (TypeError, ValueError): return 0


def _b32(data): return base64.b32encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def _src(home, table):
    for d in ("live", "stock"):
        f = storage.path('gamedata', d, 'tables', table + '.enc', root=home)
        if os.path.isfile(f): return f
    raise FileNotFoundError(table)


def _rows(home, table):
    import msgpack
    from engine import codec as C
    return msgpack.unpackb(C.decrypt_file(open(_src(home, table), "rb").read()), raw=False, strict_map_key=False)


def _windows(home):
    """season id -> (pass row, [pass window conditions], [missions window conditions])"""
    cond = {r[0]: r for r in _rows(home, "m_condition")}
    rel = {}
    for r in _rows(home, "m_condition_set_rel"): rel.setdefault(r[0], []).append(r[1])
    of = lambda s: [cond[c] for c in rel.get(s, ()) if c in cond and cond[c][1] == 2]
    return {s[0]: (s, of(s[2]) + [c for c in of(s[4]) if c[0] not in {x[0] for x in of(s[2])}], of(s[3])) for s in _rows(home, "m_season_pass")}


def listing(home):
    """[{id, label}] for the launcher, newest first."""
    out = []
    for sid, (s, win, _) in sorted(_windows(home).items(), reverse=True):
        a = min((c[5] for c in win if c[5]), default=0); b = max((c[6] for c in win if c[6]), default=s[6])
        d = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%b %Y") if ms else "?"
        out.append({"id": sid, "label": f"Season {sid} ({d(a)} - {d(b)})"})
    return out


# The cell edits keep each value's byte width, as the maintainer patch_table.py tool does, so nothing else moves.
def _skip(b, i):
    t = b[i]
    if t <= 0x7f or t >= 0xe0 or t in (0xc0, 0xc2, 0xc3): return i + 1
    if t in INTS: return i + 1 + struct.calcsize(INTS[t])
    if t in (0xca, 0xcb): return i + (5 if t == 0xca else 9)
    if 0xa0 <= t <= 0xbf: return i + 1 + (t & 0x1f)
    if t in (0xd9, 0xda, 0xdb):
        w = {0xd9: ">B", 0xda: ">H", 0xdb: ">I"}[t]; n = struct.unpack_from(w, b, i + 1)[0]; return i + 1 + struct.calcsize(w) + n
    n, i = _alen(b, i)
    if n is not None:
        for _ in range(n): i = _skip(b, i)
        return i
    raise ValueError(f"msgpack type 0x{t:02x} at {i} not handled")


def _alen(b, i):
    t = b[i]
    if 0x90 <= t <= 0x9f: return t & 0x0f, i + 1
    if t == 0xdc: return struct.unpack_from(">H", b, i + 1)[0], i + 3
    if t == 0xdd: return struct.unpack_from(">I", b, i + 1)[0], i + 5
    return None, i


def _set_cells(raw, cells):
    """cells: {row id: {column: value}} -> new bytes. Every cell must already hold a uint64 or int64 (dates do)."""
    b = bytearray(raw); n, i = _alen(b, 0); left = {k: dict(v) for k, v in cells.items()}
    for _ in range(n):
        _, j = _alen(b, i); t = b[j]
        rid = struct.unpack_from(INTS[t], b, j + 1)[0] if t in INTS else (t if t <= 0x7f else None)
        if rid in left:
            for col, value in sorted(left.pop(rid).items()):
                k = j
                for _ in range(col): k = _skip(b, k)
                if b[k] not in (0xcf, 0xd3): raise ValueError(f"row {rid} column {col}: not a 64-bit cell")
                struct.pack_into(INTS[b[k]], b, k + 1, value)
        i = _skip(b, i)
    if left: raise KeyError(f"rows not found: {sorted(left)}")
    return bytes(b)


def build(home, sid):
    """Write gamedata\\season for season sid (reused while the season and gamedata\\live are unchanged, so the game
    downloads the tables once). -> (catalog path, {served path: file})."""
    import lz4.frame
    from engine import codec as C
    live = storage.path('gamedata', 'live', root=home); out = storage.path('gamedata', 'season', root=home)
    base = open(os.path.join(live, "x-master-path.txt")).read().strip()
    meta_f = os.path.join(out, "season.json")
    try: meta = json.load(open(meta_f))
    except (OSError, ValueError): meta = {}
    if meta.get("season") == sid and meta.get("base") == base and all(os.path.isfile(os.path.join(out, f)) for f in meta.get("served", {}).values()):
        return meta["catalog"], {k: os.path.join(out, v) for k, v in meta["served"].items()}
    w = _windows(home)
    if sid not in w: raise KeyError(f"no season pass {sid}")
    s, win, miss = w[sid]
    cells = {"m_season_pass": {sid: {6: FAR}}, "m_condition": {c[0]: {6: FAR} for c in win + miss}}
    cat = json.loads(C.decrypt_file(open(os.path.join(live, "catalog.enc"), "rb").read()))
    os.makedirs(os.path.join(out, "tables"), exist_ok=True)
    served = {}
    for table in TABLES:
        raw = lz4.frame.decompress(C.aes_decrypt(open(_src(home, table), "rb").read(), C.FILE_KEY))
        comp = lz4.frame.compress(_set_cells(raw, cells[table]), block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False,
                                  content_checksum=False, block_checksum=False, store_size=False)
        enc = C.aes_encrypt(comp, C.FILE_KEY)
        open(os.path.join(out, "tables", table + ".enc"), "wb").write(enc)
        e = next(t for t in cat["tables"] if t["table"] == table)
        e.update(hash=_b32(comp), size=len(enc), path=f"/assets/{e['table_hash']}/{_b32(enc)}.dat")
        served[e["path"]] = f"tables/{table}.enc"
    cat_enc = C.aes_encrypt(json.dumps(cat, separators=(",", ":"), ensure_ascii=False).encode(), C.FILE_KEY)
    open(os.path.join(out, "catalog.enc"), "wb").write(cat_enc)
    path = f"/catalogs/{_b32(cat_enc)}.json"; served[path] = "catalog.enc"
    json.dump({"season": sid, "base": base, "catalog": path, "served": served,
               "conditions": sorted(c[0] for c in win + miss)}, open(meta_f, "w"), indent=1)
    return path, {k: os.path.join(out, v) for k, v in served.items()}
