"""Files the Story screen's Crossover Stories and Event Stories titles need (engine/storytitles.py), made at start from
the player's own copies of the game's files and added to the asset lists this server hands out.

Every file here is a copy of one the game already has, changed in place without moving a byte:
- each title's logo (sprites/story/logo/<lang>/img_thumb_story_logo_00N.a): a copy of the DIRGE of CERBERUS logo with
  the picture swapped for ours (engine/data/storyart/, made by tools/storyart/), already in the texture format of the
  file it replaces (DXT5 on PC, ASTC 6x6 on phones), so no encoder runs here.
- each title's look (data/story/title/<lang>/dat_ui_asset_story_title0N.a): a copy of DIRGE's with its background
  movie set to the recipe's and its logo pointing at our logo file.
- the Story copy of every event scene dungeon the titles play (data/dungeon/dat_asset_dungeon_<id - 200000>.a and its
  data/dungeonassetlist/<lang>/ list): the event's own with dungeonType 1 instead of -2, as the game's own story ports
  are. A Story episode on an event dungeon plays, then ends on a black screen.

Names keep their length (title06 -> title08, 9903600 -> 9703600, a new CAB-<md5 of the new name>), so a copy is the
original's bytes with those spans replaced. A file this PC doesn't have is left out (logged); its scene can't play
anywhere then, the event's own card included. The lists get the new entries after their own, new ids, 6-letter object
names (phones refuse longer ones) and a higher revision, so games read the list again.
"""
import hashlib
import json
import struct
import zlib
from pathlib import Path

import lz4.block

from engine import files

VERSION = 1                      # raise when the art or the copies change, so games read the lists again
DATA = Path(__file__).parent / 'data' / 'storyart'
TEMPLATE = 6                     # DIRGE of CERBERUS
LANGS = ('en', 'ja')
LOGO = 'sprites/story/logo/{lang}/img_thumb_story_logo_{n:03d}.a'
TITLE = 'data/story/title/{lang}/dat_ui_asset_story_title{n:02d}.a'
DUNGEON = 'data/dungeon/dat_asset_dungeon_{id}.a'
DUNGEON_LIST = 'data/dungeonassetlist/{lang}/dat_asset_list_dungeon_{id}.a'
TEXTURE = {'steam': 'dxt5', 'android': 'astc6x6', 'ios': 'astc6x6'}
EVENT_TYPE, STORY_TYPE = struct.pack('<i', -2), struct.pack('<i', 1)      # DungeonAssetData.dungeonType
MARK = {'steam': 'S', 'android': 'A', 'ios': 'I'}


# ---- objects as the game stores them ------------------------------------------------------------------------------

def _mask(name, seed=0xAB):
    b = name.encode('ascii'); n = len(b) * 2
    mask = bytearray(n); front, back = 0, n - 1
    for v in b:
        mask[front] = v; front += 2
        mask[back] = v; back -= 2
    roll = seed
    for v in mask:
        roll = ((((roll & 1) << 7) | (roll >> 1)) ^ v) & 0xFF
    return bytes(x ^ roll for x in mask)


def _xor(data, name):
    m = _mask(name)
    rep = (m * (len(data) // len(m) + 1))[:len(data)]
    return (int.from_bytes(data, 'little') ^ int.from_bytes(rep, 'little')).to_bytes(len(data), 'little')


def unwrap(stored, name):
    """An object's bytes -> the plain bundle."""
    out = bytearray(_xor(stored, name)); out[0] = ord('U')
    return bytes(out)


def wrap(plain, name, marker):
    """A plain bundle -> object bytes; marker = byte 0 of the object it copies."""
    out = bytearray(_xor(plain, name)); out[0] = marker
    return bytes(out)


# ---- UnityFS bundles ----------------------------------------------------------------------------------------------

def _decompress(data, kind, size):
    if kind == 0:
        return data
    if kind in (2, 3):
        return lz4.block.decompress(data, uncompressed_size=size)
    if kind == 1:
        import lzma
        props, dict_size = data[0], int.from_bytes(data[1:5], 'little')
        lc = props % 9; props //= 9
        f = [{'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': props % 5, 'pb': props // 5}]
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=f).decompress(data[5:], size)
    raise ValueError(f'unknown bundle compression {kind}')


class Bundle:
    """A UnityFS bundle as its header, its files (name, flags) and their data run together."""
    BLOCK = 128 * 1024

    def __init__(self, plain):
        if not plain.startswith(b'UnityFS\x00'):
            raise ValueError('not a UnityFS bundle')
        i = 8
        self.format = struct.unpack('>I', plain[i:i + 4])[0]; i += 4
        j = plain.index(b'\x00', i); self.version = plain[i:j]; i = j + 1
        j = plain.index(b'\x00', i); self.revision = plain[i:j]; i = j + 1
        _, csize, usize, self.flags = struct.unpack('>qIII', plain[i:i + 20]); i += 20
        if self.format >= 7:
            i = (i + 15) & ~15
        if self.flags & 0x80:
            info, start = plain[len(plain) - csize:], i
        else:
            info, start = plain[i:i + csize], i + csize
        if self.flags & 0x200:
            start = (start + 15) & ~15
        info = _decompress(info, self.flags & 0x3F, usize)
        k = 16
        blocks = struct.unpack('>i', info[k:k + 4])[0]; k += 4
        data, pos, self.block_flags = [], start, 0
        for _ in range(blocks):
            u, c, bf = struct.unpack('>IIH', info[k:k + 10]); k += 10
            data.append(_decompress(plain[pos:pos + c], bf & 0x3F, u)); pos += c
            self.block_flags = bf & ~0x3F
        self.data = b''.join(data)
        nodes = struct.unpack('>i', info[k:k + 4])[0]; k += 4
        self.nodes = []
        for _ in range(nodes):
            offset, size, flags = struct.unpack('>qqI', info[k:k + 20]); k += 20
            j = info.index(b'\x00', k); path = info[k:j].decode(); k = j + 1
            self.nodes.append([offset, size, flags, path])

    def node(self, suffix):
        hits = [n for n in self.nodes if n[3].endswith(suffix)]
        if len(hits) != 1:
            raise ValueError(f'{len(hits)} files ending {suffix!r} in the bundle')
        return hits[0]

    def save(self):
        """The bundle with LZ4 blocks; crc() of it equals the CRC of the same data however it was packed."""
        chunks = [self.data[i:i + self.BLOCK] for i in range(0, len(self.data), self.BLOCK)] or [b'']
        packed = [lz4.block.compress(c, store_size=False) for c in chunks]
        info = bytes(16) + struct.pack('>i', len(chunks))
        info += b''.join(struct.pack('>IIH', len(c), len(p), self.block_flags | 2) for c, p in zip(chunks, packed))
        info += struct.pack('>i', len(self.nodes))
        info += b''.join(struct.pack('>qqI', o, s, f) + p.encode() + b'\x00' for o, s, f, p in self.nodes)
        cinfo = lz4.block.compress(info, store_size=False)
        flags = (self.flags & ~0x3F & ~0x80) | 2
        head = b'UnityFS\x00' + struct.pack('>I', self.format) + self.version + b'\x00' + self.revision + b'\x00'
        fixed = len(head) + 20
        if self.format >= 7:
            fixed = (fixed + 15) & ~15
        start = fixed + len(cinfo)
        pad = ((start + 15) & ~15) - start if flags & 0x200 else 0
        total = start + pad + sum(len(p) for p in packed)
        out = head + struct.pack('>qIII', total, len(cinfo), len(info), flags)
        out += bytes(fixed - len(out)) + cinfo + bytes(pad) + b''.join(packed)
        assert len(out) == total
        return out


def crc(plain):
    """The CRC the asset list carries for a bundle: CRC32 over its files' data, unpacked."""
    return zlib.crc32(Bundle(plain).data) & 0xFFFFFFFF


def cab(name):
    return 'CAB-' + hashlib.md5(name.encode()).hexdigest()


def copy(plain, swaps, files_data=None):
    """A copy of a bundle: every (old, new) byte span swapped (equal lengths, old present at least once, in the data
    or the file names), and files_data {name suffix: bytes} replacing a file's data outright (same size)."""
    b = Bundle(plain)
    data = b.data
    for old, new in swaps:
        if len(old) != len(new):
            raise ValueError(f'{old!r} -> {new!r}: lengths differ')
        found = data.count(old) + sum(n[3].encode().count(old) for n in b.nodes)
        if not found:
            raise ValueError(f'{old!r} is not in the bundle')
        data = data.replace(old, new)
        for n in b.nodes:
            n[3] = n[3].encode().replace(old, new).decode()
    for suffix, payload in (files_data or {}).items():
        offset, size, _, _ = b.node(suffix)
        if len(payload) != size:
            raise ValueError(f'{suffix}: {len(payload)} bytes for a {size}-byte file')
        data = data[:offset] + payload + data[offset + size:]
    b.data = data
    return b.save()


def _cab_of(plain):
    main = [n[3] for n in Bundle(plain).nodes if not n[3].endswith('.resS')]
    if len(main) != 1 or not main[0].startswith('CAB-') or len(main[0]) != 36:
        raise ValueError(f'bundle files {main}')
    return main[0]


def logo(plain, number, lang, picture):
    """The template's logo with our picture and the names of logo <number>."""
    old, new = LOGO.format(lang=lang, n=TEMPLATE), LOGO.format(lang=lang, n=number)
    stem = lambda n: n.rsplit('/', 1)[1][:-2].encode()
    return copy(plain, [(_cab_of(plain)[4:].encode(), cab(new)[4:].encode()), (stem(old), stem(new))],
                {'.resS': picture})


def title(plain, number, lang, movie):
    """The template's title look with title <number>'s names, our movie and our logo."""
    old, new = TITLE.format(lang=lang, n=TEMPLATE), TITLE.format(lang=lang, n=number)
    new_logo = LOGO.format(lang=lang, n=number)
    stem = lambda n: n.rsplit('/', 1)[1][:-2].encode()
    b = Bundle(plain)
    movies = [m for m in set(_strings(b.data, b'movie/')) if m.endswith(b'.usm')]
    if len(movies) != 1 or len(movies[0]) != len(movie):
        raise ValueError(f'title {TEMPLATE} movie {movies}, ours {movie!r}')
    return copy(plain, [(_cab_of(plain)[4:].encode(), cab(new)[4:].encode()), (stem(old), stem(new)),
                        (_logo_cab(b, plain), cab(new_logo)[4:].encode()),
                        (movies[0], movie.encode())])


def _logo_cab(b, plain):
    """The CAB hex of the logo bundle the title points at (its one external file)."""
    own = _cab_of(plain)[4:].encode()
    found = {m for m in _hex_after(b.data, b'archive:/CAB-')} - {own}
    if len(found) != 1:
        raise ValueError(f'title points at {found}')
    return found.pop()


def _hex_after(data, prefix):
    i = data.find(prefix)
    while i >= 0:
        yield data[i + len(prefix):i + len(prefix) + 32]
        i = data.find(prefix, i + 1)


def _strings(data, prefix):
    i = data.find(prefix)
    while i >= 0:
        j = i
        while j < len(data) and 32 < data[j] < 127:
            j += 1
        yield data[i:j]
        i = data.find(prefix, i + 1)


def dungeon(plain, source_id, story_id):
    """An event scene dungeon's Story copy: names and CAB of <story_id>, dungeonType 1."""
    old, new = DUNGEON.format(id=source_id), DUNGEON.format(id=story_id)
    b = Bundle(plain)
    if b.data.count(EVENT_TYPE) != 1:
        raise ValueError(f'{old}: dungeonType -2 found {b.data.count(EVENT_TYPE)} times')
    return copy(plain, [(_cab_of(plain)[4:].encode(), cab(new)[4:].encode()),
                        (f'dungeon_{source_id}'.encode(), f'dungeon_{story_id}'.encode()), (EVENT_TYPE, STORY_TYPE)])


def dungeon_list(plain, source_id, story_id, lang):
    old, new = DUNGEON_LIST.format(lang=lang, id=source_id), DUNGEON_LIST.format(lang=lang, id=story_id)
    return copy(plain, [(_cab_of(plain)[4:].encode(), cab(new)[4:].encode()),
                        (f'dungeon_{source_id}'.encode(), f'dungeon_{story_id}'.encode())])


# ---- asset lists --------------------------------------------------------------------------------------------------

def _varint(b, i):
    v = s = 0
    while True:
        c = b[i]; i += 1; v |= (c & 0x7F) << s; s += 7
        if not c & 0x80:
            return v, i


def _enc(v):
    o = bytearray()
    while True:
        c = v & 0x7F; v >>= 7
        o.append(c | (0x80 if v else 0))
        if not v:
            return bytes(o)


def _frames(raw):
    """[(field, wire, value)] keeping each value's raw bytes."""
    i, out = 0, []
    while i < len(raw):
        key, i = _varint(raw, i); f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(raw, i)
        elif w == 2:
            n, i = _varint(raw, i); v = raw[i:i + n]; i += n
        elif w == 1:
            v = raw[i:i + 8]; i += 8
        elif w == 5:
            v = raw[i:i + 4]; i += 4
        else:
            raise ValueError(f'wire type {w}')
        out.append((f, w, v))
    return out


def _encode(items):
    o = bytearray()
    for f, w, v in items:
        o += _enc((f << 3) | w)
        if w == 0:
            o += _enc(v)
        elif w == 2:
            o += _enc(len(v)) + v
        else:
            o += v
    return bytes(o)


def _entries(raw, names):
    """({name: (field, [(field, wire, value)])} for the list's entries with these names, the highest entry id), reading
    only those entries. Every entry starts with its id (field 1) and its name (field 3); one that doesn't is read whole."""
    out, top, i = {}, 0, 0
    names = {n.encode(): n for n in names}
    while i < len(raw):
        key, i = _varint(raw, i); f, w = key >> 3, key & 7
        if w == 0:
            _, i = _varint(raw, i); continue
        if w != 2:
            i += 8 if w == 1 else 4; continue
        n, i = _varint(raw, i); start, end = i, i + n; i = end
        if f not in (2, 4):
            continue
        if raw[start] == 0x08:
            entry_id, j = _varint(raw, start + 1)
            if raw[j] == 0x1A:
                length, j = _varint(raw, j + 1)
                top = max(top, entry_id)
                name = names.get(raw[j:j + length])
                if name is not None:
                    out[name] = (f, _frames(raw[start:end]))
                continue
        item = _frames(raw[start:end])
        top = max([top] + [x for ff, ww, x in item if ff == 1 and ww == 0])
        name = names.get(next((x for ff, _, x in item if ff == 3), b''))
        if name is not None:
            out[name] = (f, item)
    return out, top


def _set_revision(raw, rev):
    """The list with its revision (top-level field 1) set to rev; everything else byte for byte."""
    hits, i = [], 0
    while i < len(raw):
        at = i; key, i = _varint(raw, i); f, w = key >> 3, key & 7
        if w == 0:
            _, i = _varint(raw, i)
            if f == 1:
                hits.append((at, i))
        elif w == 2:
            n, i = _varint(raw, i); i += n
        else:
            i += 8 if w == 1 else 4
    if len(hits) != 1:
        raise ValueError(f'list has {len(hits)} revision fields')
    at, end = hits[0]
    return raw[:at] + b'\x08' + _enc(rev) + raw[end:]


def revision(base, added):
    """The list's revision with our entries: above any the game saw before, higher whenever more files are added."""
    return (VERSION * 1000 + base) * 1000 + min(added, 999)


def plan(recipe, copies):
    """[(new name, template name, kind, args)] for one language set, before the PC's files are looked at."""
    jobs = []
    for number, art in sorted((int(k), v) for k, v in recipe.get('story_titles', {}).get('art', {}).items()):
        for lang in LANGS:
            jobs.append((LOGO.format(lang=lang, n=number), LOGO.format(lang=lang, n=TEMPLATE), 'logo', (number, lang, art)))
            jobs.append((TITLE.format(lang=lang, n=number), TITLE.format(lang=lang, n=TEMPLATE), 'title', (number, lang, art)))
    for source_id, story_id in sorted(copies.items()):
        jobs.append((DUNGEON.format(id=story_id), DUNGEON.format(id=source_id), 'dungeon', (source_id, story_id)))
        for lang in LANGS:
            jobs.append((DUNGEON_LIST.format(lang=lang, id=story_id), DUNGEON_LIST.format(lang=lang, id=source_id),
                         'dungeon_list', (source_id, story_id, lang)))
    return jobs


def picture(art, platform):
    """Our logo, encoded the way the platform's logo files are (engine/data/storyart/<logo>.dxt5 / .astc6x6)."""
    return (DATA / (art['logo'] + '.' + TEXTURE[platform])).read_bytes()


def extend_catalog(catalog, recipe, copies, read, used_objects, log=None):
    """(new list bytes, {object: bytes}) for one platform's list, or (None, {}) when nothing could be made.
    read(entry) -> the object's stored bytes or None."""
    say = log.info if log else (lambda *a: None)
    jobs = plan(recipe, copies)
    entries, top_id = _entries(catalog.data, {n for job in jobs for n in job[:2]})
    next_id = top_id + 1
    made, missing, failed, objects, adds, keys = {}, [], [], {}, [], {}
    by_name = {}
    for name, template, kind, args in jobs:
        if name in entries:
            raise ValueError(f'{catalog.platform}: {name} is already in the list')
        if template not in entries:
            continue                                     # a language this platform doesn't have (phones: en only)
        f, item = entries[template]
        fields = {ff: x for ff, _, x in item}
        stored = read(fields[11].decode(), fields[10].decode())
        if stored is None:
            missing.append(template); continue
        source = unwrap(stored, template)
        try:
            if kind == 'logo':
                number, lang, art = args
                plain = logo(source, number, lang, picture(art, catalog.platform))
            elif kind == 'title':
                number, lang, art = args
                if LOGO.format(lang=lang, n=number) not in by_name:
                    missing.append(f'{name} (its logo)'); continue
                plain = title(source, number, lang, art['movie'])
            elif kind == 'dungeon':
                plain = dungeon(source, *args)
            else:
                if DUNGEON.format(id=args[1]) not in by_name:
                    continue
                plain = dungeon_list(source, *args)
        except ValueError as error:
            failed.append(f'{template}: {error}'); continue
        data = wrap(plain, name, stored[0])
        md5 = hashlib.md5(data).hexdigest()
        obj = next(o for o in (MARK[catalog.platform] + 'R' + md5[k:k + 4] for k in range(0, 29))
                   if o not in used_objects and o not in objects)
        objects[obj] = data
        new_id = next_id; next_id += 1
        out = []
        for ff, ww, x in item:
            x = {1: new_id, 3: name.encode(), 4: len(data), 5: crc(plain), 10: md5.encode(), 11: obj.encode()}.get(ff, x)
            if ff == 8 and kind == 'title':
                x = by_name[LOGO.format(lang=args[1], n=args[0])]
            out.append((ff, ww, x))
        by_name[name] = new_id
        adds.append((f, 2, _encode(out)))
        keys[obj] = ('A' if f == 2 else 'R') + str(new_id)
        made[name] = obj
    # a dungeon whose list (en) couldn't be made has no use: drop both
    for name, template, kind, args in plan(recipe, copies):
        if kind == 'dungeon' and name in by_name and DUNGEON_LIST.format(lang='en', id=args[1]) not in by_name:
            drop = by_name.pop(name)
            adds = [a for a in adds if {ff: x for ff, _, x in _frames(a[2])}.get(1) != drop]
            objects.pop(made.pop(name), None); missing.append(f'{name} (no dungeon list)')
    if missing:
        say('story titles: %s lacks %d files, left out: %s', catalog.platform, len(missing), ', '.join(missing[:6])
            + (' ...' if len(missing) > 6 else ''))
    for line in failed:
        (log.error if log else say)('story titles: %s %s', catalog.platform, line)
    if not adds:
        return None, {}, {}
    titles_made = sorted(n for n in by_name if n.startswith('data/story/title/'))
    say('story titles: %s list +%d files (%s)', catalog.platform, len(adds), ', '.join(titles_made) or 'no title art')
    rev = revision(catalog.revision, len(adds))
    return _set_revision(catalog.data, rev) + _encode(adds), objects, {o: keys[o] for o in objects}


def extend(registry, root, recipe, copies, log=None):
    """The registry with every platform's list extended and the new objects saved in the shared store."""
    from engine.catalogs import Catalog, CatalogRegistry
    if not recipe.get('story_titles'):
        return registry
    steam = [c for c in registry.catalogs.values() if c.platform == 'steam']
    used = set(registry.objects)
    catalogs = {}
    for ident, catalog in registry.catalogs.items():
        def read(obj, md5, catalog=catalog):
            f = files.find_blob(catalog, obj, md5, others=steam)
            if not f:
                return None
            data = Path(f).read_bytes()
            return data if hashlib.md5(data).hexdigest() == md5 else None
        try:
            raw, objects, keys = extend_catalog(catalog, recipe, copies, read, used, log)
        except (ValueError, OSError) as error:
            if log:
                log.error('story titles: %s list left as it was (%r)', catalog.platform, error)
            raw, objects, keys = None, {}, {}
        if raw is None:
            catalogs[ident] = catalog; continue
        for obj, data in objects.items():
            target = Path(files.store_path(hashlib.md5(data).hexdigest(), root))
            if not target.is_file() or target.stat().st_size != len(data):
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + '.part')
                tmp.write_bytes(data); tmp.replace(target)
        used |= set(objects)
        md5s = {obj: hashlib.md5(data).hexdigest() for obj, data in objects.items()}
        new = Catalog(ident, catalog.label, raw, revision(catalog.revision, len(objects)), catalog.url_format, {**catalog.md5s, **md5s},
                      catalog.count + len(objects), catalog.total_bytes + sum(len(d) for d in objects.values()))
        # what the registry and the file lookup work out from the list, carried over instead of reading it again
        new.__dict__['_entries'] = catalog.entries() + [(obj, md5s[obj], len(objects[obj])) for obj in objects]
        if 'local_keys' in catalog.__dict__:
            new.__dict__['local_keys'] = {**catalog.local_keys, **keys}
        catalogs[ident] = new
    return CatalogRegistry(catalogs)
