"""Game files the publisher pulled, put back from copies saved before (2026-10-21).

The game's asset lists still carry some files only as stand-ins: the REBIRTH crossover's battle wave movies
battle_wave_end2_001_01.usm to end2_011_02 are 6 KB, 1-second black clips (state 2 in the list; the real movies have 1),
and the battle waits on its movie before and after the fight. Copies of the real files saved while the event ran are
kept in gameassets/restored/ (other files go there the same way), named as the list names them
(battle_wave_end2_001_01.usm; a list name with folders, such as data/story/..., in those folders).

At start each list entry with a file there gets that file instead: its size, its checksum, a new object name and
state 1, on every platform's list that names it (phones list the same movies), and the file goes into the shared store.
The list's revision goes up whenever any file is put back, so games read the list again. Nothing changes without the
folder. (Emptying it again lowers the revision: a PC reads the list again anyway, as the server rewrites the game's saved
list at start, but a phone keeps the old list until the revision passes the one it has.)"""
import hashlib
from pathlib import Path

from engine import files

FOLDER = ('gameassets', 'restored')
MARK = {'steam': 'S', 'android': 'A', 'ios': 'I'}


def found(root):
    """{list name: file} for the files in gameassets/restored."""
    base = Path(root).joinpath(*FOLDER)
    try:
        return {p.relative_to(base).as_posix(): p for p in sorted(base.rglob('*')) if p.is_file()}
    except OSError:
        return {}


def restore_catalog(raw, platform, available, root, used):
    """(list bytes with each entry named in available pointing at that file, {object: md5}); every new object is saved
    in the store. used: object names already taken."""
    from engine.storyart import _encode, _frames
    out, added, done = [], {}, {}
    for f, w, v in _frames(raw):
        if f in (2, 4) and w == 2:
            sub = _frames(v)
            name = next((x for ff, _, x in sub if ff == 3), b'').decode('utf-8', 'replace')
            if name in available:
                if name not in done:
                    data = available[name].read_bytes()
                    md5 = hashlib.md5(data).hexdigest()
                    target = Path(files.store_path(md5, root))
                    if not target.is_file() or target.stat().st_size != len(data):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        tmp = target.with_name(target.name + '.part')
                        tmp.write_bytes(data); tmp.replace(target)
                    done[name] = (md5, len(data))
                md5, size = done[name]
                obj = next(o for o in (MARK.get(platform, 'X') + 'V' + md5[k:k + 4] for k in range(0, 29))
                           if o not in used and o not in added)
                added[obj] = md5
                v = _encode([(ff, ww, {4: size, 9: 1, 10: md5.encode(), 11: obj.encode()}.get(ff, x))
                             for ff, ww, x in sub])
        out.append((f, w, v))
    return _encode(out), added


def revision(rev):
    """The list's revision with files put back: above the list's own and any it had before."""
    return rev * 10 + 1


def extend(registry, root, log=None):
    """The registry with the files in gameassets/restored put back in every list that names them."""
    from engine.catalogs import Catalog, CatalogRegistry, inspect_catalog
    from engine.storyart import _set_revision
    available = found(root)
    if not available:
        return registry
    used = set(registry.objects)
    catalogs, named = {}, set()
    for ident, catalog in registry.catalogs.items():
        raw, added = restore_catalog(catalog.data, catalog.platform, available, root, used)
        if not added:
            catalogs[ident] = catalog; continue
        raw = _set_revision(raw, revision(catalog.revision))
        rev, url, md5s, count, total = inspect_catalog(raw)
        catalogs[ident] = Catalog(ident, catalog.label, raw, rev, url, md5s, count, total)
        used |= set(added)
        if log:
            log.info('restored files: %s list, %d entries put back', catalog.platform, len(added))
    for catalog in catalogs.values():
        named.update(n for n in available if n.encode() in catalog.data)
    left = sorted(set(available) - named)
    if left and log:
        log.warning('restored files: no list names %s', ', '.join(left[:6]) + (' ...' if len(left) > 6 else ''))
    return CatalogRegistry(catalogs)
