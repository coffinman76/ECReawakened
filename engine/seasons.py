"""Old season passes, one at a time (2026-10-11).

Each season pass shows in the game only inside its date windows: m_season_pass column 2 (and 4) names the condition set
of the pass itself, column 3 the set of its missions ("Exploration"), each a type 2 date window in m_condition (column 5
start, column 6 end); column 6 of m_season_pass is the season's end. The windows of every season have passed, so the
Season Pass button says "Outside season period". The launcher lets the player pick one season (game.json
"season_pass"); when the server starts, this module writes copies of m_condition and m_season_pass with that season's
ends moved to the year 3000 into gamedata\\season, with a catalog of its own, and the server serves those in place of
gamedata\\live. gamedata\\live is never changed, so picking None puts the game back as it was. Only one season: the
game has one Season Pass button and shows one season. The premium track needs no table: rules._season_premium_heal
gives every account each season's premium item.

Old guild battles the same way (game.json "guild_battle", an event id of m_event_guild_ranking): the Guild screen shows
its Guild Battle button for the first guild battle event whose m_event_base display window (column 5) is open, and the
event is in its mock battle period from the start of its open window (column 7) until its main battle starts
(m_event_guild_ranking column 11). The picked event's display and open windows end in the year 3000 and its main battle,
counting and result days move after that, keeping their lengths, so the Guild Battle screen shows its mock battles
(engine/rules.py guild_battle_*). Both pickers write one set of tables, since both change m_condition.

The Chocobo Farm visitor the same way (game.json "farm_visitor", 1-3, #94): the farm (dungeon 8000001, map 4999 content
1000 in the game's asset bundles) sets up every event visitor whose condition set passes, all on the same spot, so two at
once hang the farm on loading. The sets are fixed in that bundle, not in the master data, hence FARM_VISITORS. The picked
visitor's set points at FARM_ON (a date only the Slime's set uses, ending in the year 3000), the others at FARM_OFF (a
date long over), by editing the condition id of each set's m_condition_set_rel row. No event's own dates change. None
leaves the farm as it is: the Mog, whose date is the Kefka event's, which the offline events keep open."""
import base64, hashlib, json, os, struct
from engine import storage
from datetime import datetime, timedelta, timezone

FAR = 32503680000000      # 3000-01-01, as the earlier event openings use
DAY = 86400000
TABLES = ("m_condition", "m_season_pass", "m_event_guild_ranking", "m_condition_set_rel")
# (visitor, launcher label, the condition set the farm checks for it, the date condition that set holds in the game data)
FARM_VISITORS = ((1, "Palico (Monster Hunter, Jun 2024)", 10024061008, 10024061008),
                 (2, "Mog (Nov 2024)", 890000000001, 10024111002),
                 (3, "Slime (Dragon Quest, Aug 2025)", 890000000002, 10025081001))
FARM_ON, FARM_OFF = 10025081001, 10024061008
FARM_DEFAULT = 2          # the visitor the farm already shows: the launcher's Default
INTS = {0xcc: ">B", 0xcd: ">H", 0xce: ">I", 0xcf: ">Q", 0xd0: ">b", 0xd1: ">h", 0xd2: ">i", 0xd3: ">q"}


def chosen(home, key="season_pass"):
    try: v = json.load(open(os.path.join(home, "game.json"), encoding="utf-8-sig")).get(key) or 0
    except (OSError, ValueError, AttributeError): return 0
    try: return int(v)
    except (TypeError, ValueError): return 0


def chosen_guild_battle(home): return chosen(home, "guild_battle")


def chosen_farm_visitor(home): return chosen(home, "farm_visitor")


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


def _month(ms):
    """'Jun 2026' for a time in ms. Not fromtimestamp: on Windows that fails with Errno 22 past the year 3000."""
    try: return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).strftime("%b %Y")
    except (OverflowError, ValueError): return "?"


def listing(home):
    """[{id, label}] for the launcher, newest first."""
    out = []
    for sid, (s, win, _) in sorted(_windows(home).items(), reverse=True):
        a = min((c[5] for c in win if c[5]), default=0); b = max((c[6] for c in win if c[6]), default=s[6])
        d = lambda ms: _month(ms) if ms else "?"
        out.append({"id": sid, "label": f"Season {sid} ({d(a)} - {d(b)})"})
    return out


def _guild_events(home):
    """guild battle event id -> (m_event_guild_ranking row, [display and open window conditions], [open window])"""
    cond = {r[0]: r for r in _rows(home, "m_condition")}
    rel = {}
    for r in _rows(home, "m_condition_set_rel"): rel.setdefault(r[0], []).append(r[1])
    of = lambda s: [cond[c] for c in rel.get(s, ()) if c in cond and cond[c][1] == 2]
    base = {r[0]: r for r in _rows(home, "m_event_base")}
    return {g[0]: (g, of(base[g[0]][5]) + of(base[g[0]][7]), of(base[g[0]][7])) for g in _rows(home, "m_event_guild_ranking") if g[0] in base}


def _enemy_names(home):
    """m_enemy text id -> English name, or {} when the text isn't installed."""
    from engine import codec as C
    try:
        f = storage.path('gamedata', 'stock', 'language', 'm_enemy.enc', root=home, legacy_fallback=False)
        return {e["id"]: e["value"] for e in json.loads(C.decrypt_file(open(f, "rb").read()))["data"]}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError): return {}


def _bosses(home):
    """guild battle event id -> its boss's English name ('' when unknown): the boss (StatusDisplayType 2, else the first
    enemy) of the last wave of its first battle."""
    try:
        names = _enemy_names(home)
        enemy = {r[0]: r for r in _rows(home, "m_enemy")}
        battle = {r[0]: r for r in _rows(home, "m_battle")}
        waves, groups, first = {}, {}, {}
        for r in _rows(home, "m_battle_wave"): waves.setdefault(r[0], []).append(r)
        for r in _rows(home, "m_battle_enemy"): groups.setdefault(r[0], []).append(r)
        for r in sorted(_rows(home, "m_event_guild_ranking_battle"), key=lambda r: (r[1], r[2])): first.setdefault(r[1], r[3])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError): return {}
    out = {}
    for event_id, battle_id in first.items():
        b = battle.get(battle_id)
        if not b or b[2] not in waves: continue
        members = groups.get(max(waves[b[2]], key=lambda w: w[1])[2], [])
        big = [e for e in members if e[5] == 2] or members
        e = enemy.get(big[0][2]) if big else None
        out[event_id] = names.get(e[5], "") if e else ""
    return out


def guild_listing(home):
    """[{id, label}] of the old guild battles for the launcher, newest first."""
    bosses = _bosses(home); out = []
    for gid, (g, _, opened) in sorted(_guild_events(home).items(), key=lambda kv: -min((c[5] for c in kv[1][2] if c[5]), default=kv[1][0][11])):
        boss = bosses.get(gid)
        start = min((c[5] for c in opened if c[5]), default=g[11])
        rerun = " rerun" if gid // 100 % 10 else ""                 # 9124: guild battle 24 again
        out.append({"id": gid, "label": f"#{gid % 100}{rerun}{': ' + boss if boss else ''} ({_month(start) if start else '?'})"})
    return out


def _guild_cells(home, gid):
    """The cell edits that open guild battle gid for its mock battles."""
    ev = _guild_events(home)
    if gid not in ev: raise KeyError(f"no guild battle {gid}")
    g, win, _ = ev[gid]
    main, count = max(g[12] - g[11], DAY), max(g[8] - g[7], DAY)
    return {"m_condition": {c[0]: {6: FAR} for c in win},
            "m_event_guild_ranking": {gid: {11: FAR, 12: FAR + main, 7: FAR + main, 8: FAR + main + count}}}


def farm_listing(home):
    """[{id, label}] of the Chocobo Farm visitors the launcher offers besides its Default."""
    return [{"id": vid, "label": label} for vid, label, _, _ in FARM_VISITORS if vid != FARM_DEFAULT]


def _farm_cells(home, vid, now=None):
    """The cell edits that make visitor vid the only one at the Chocobo Farm."""
    if vid not in {v[0] for v in FARM_VISITORS}: raise KeyError(f"no farm visitor {vid}")
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    off = next((r for r in _rows(home, "m_condition") if r[0] == FARM_OFF), None)
    if not off or off[1] != 2 or not 0 < off[6] < now: raise ValueError(f"condition {FARM_OFF} is not a date that has ended")
    rel = {r[0]: r for r in _rows(home, "m_condition_set_rel") if r[0] in {v[2] for v in FARM_VISITORS}}
    for _, _, set_id, cond in FARM_VISITORS:
        if set_id not in rel or rel[set_id][1] != cond: raise ValueError(f"condition set {set_id} is not the game's farm visitor set")
    return {"m_condition": {FARM_ON: {6: FAR}},
            "m_condition_set_rel": {set_id: {1: FARM_ON if v == vid else FARM_OFF} for v, _, set_id, _ in FARM_VISITORS}}


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


def build(home, sid, gid=0, vid=0):
    """Write gamedata\\season for season sid, guild battle gid and farm visitor vid, each 0 for none (reused while all
    three and gamedata\\live are unchanged, so the game downloads the tables once). -> (catalog path, {served path: file})."""
    import lz4.frame
    from engine import codec as C
    live = storage.path('gamedata', 'live', root=home); out = storage.path('gamedata', 'season', root=home)
    base = open(os.path.join(live, "x-master-path.txt")).read().strip()
    meta_f = os.path.join(out, "season.json")
    try: meta = json.load(open(meta_f))
    except (OSError, ValueError): meta = {}
    if meta.get("season") == sid and meta.get("guild_battle", 0) == gid and meta.get("farm_visitor", 0) == vid and meta.get("base") == base and all(os.path.isfile(os.path.join(out, f)) for f in meta.get("served", {}).values()):
        return meta["catalog"], {k: os.path.join(out, v) for k, v in meta["served"].items()}
    cells, win, miss = {}, [], []
    if sid:
        w = _windows(home)
        if sid not in w: raise KeyError(f"no season pass {sid}")
        s, win, miss = w[sid]
        cells = {"m_season_pass": {sid: {6: FAR}}, "m_condition": {c[0]: {6: FAR} for c in win + miss}}
    if gid:
        for table, rows in _guild_cells(home, gid).items():
            for rid, cols in rows.items(): cells.setdefault(table, {}).setdefault(rid, {}).update(cols)
    if vid:
        for table, rows in _farm_cells(home, vid).items():
            for rid, cols in rows.items(): cells.setdefault(table, {}).setdefault(rid, {}).update(cols)
    cat = json.loads(C.decrypt_file(open(os.path.join(live, "catalog.enc"), "rb").read()))
    os.makedirs(os.path.join(out, "tables"), exist_ok=True)
    served = {}
    for table in (t for t in TABLES if t in cells):
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
    json.dump({"season": sid, "guild_battle": gid, "farm_visitor": vid, "base": base, "catalog": path, "served": served,
               "conditions": sorted(c[0] for c in win + miss)}, open(meta_f, "w"), indent=1)
    return path, {k: os.path.join(out, v) for k, v in served.items()}
