"""Locate and verify user-supplied Octo blobs for the local listener."""
import hashlib, json, os, re
from functools import lru_cache

HOME = os.environ.get("EC_HOME") or os.getcwd()


def _md5_of(path):
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, 'md5').hexdigest()
    except OSError: return None


def blob_dir(catalog):
    """Where files were saved before the shared store (one folder per platform and list revision); still searched."""
    return os.path.join(HOME, 'gameassets', 'blobs', catalog.platform, str(catalog.revision))


STORE = 'by-md5'
_MD5 = re.compile(r'[0-9a-f]{32}')


def store_path(md5, root=None):
    """Where a downloaded file is kept: one store for every platform and list revision, filed by checksum
    (gameassets/blobs/by-md5/<first two hex digits>/<md5>), so files survive asset list changes and Android and
    iPhone share the many files they have in common."""
    md5 = md5.lower()
    return os.path.join(root or HOME, 'gameassets', 'blobs', STORE, md5[:2], md5)


def migrate_store(root=None):
    """Move files saved the old way (blobs/<platform>/<revision>/<md5>) into the shared store, once. A file the
    store already has with the same size is dropped; anything else is left where it is. -> files moved."""
    base = os.path.join(root or HOME, 'gameassets', 'blobs')
    moved = 0
    try:
        platforms = [d for d in os.scandir(base) if d.is_dir() and d.name != STORE]
    except OSError:
        return 0
    for platform in platforms:
        for revision in [d for d in os.scandir(platform.path) if d.is_dir()]:
            for entry in os.scandir(revision.path):
                if not entry.is_file() or not _MD5.fullmatch(entry.name):
                    continue
                target = store_path(entry.name, root)
                try:
                    if os.path.exists(target):
                        if os.path.getsize(target) == entry.stat().st_size:
                            os.remove(entry.path)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    os.replace(entry.path, target)
                    moved += 1
                except OSError:
                    continue
            try:
                os.rmdir(revision.path)          # only when empty
            except OSError:
                pass
        try:
            os.rmdir(platform.path)
        except OSError:
            pass
    return moved


@lru_cache(maxsize=8)
def _archive_config(path, modified):
    with open(path, encoding='utf-8-sig') as f:
        config = json.load(f).get('archive_roots', {})
    if not isinstance(config, dict) or any(not isinstance(v, list) or any(not isinstance(p, str) for p in v) for v in config.values()):
        raise ValueError('archive_roots must map platforms to lists of directory paths')
    return config


def _roots(catalog):
    """Folders holding this platform's objects by MD5 (and legacy object names)."""
    from engine import storage
    roots = [blob_dir(catalog)]
    config_path = os.path.join(HOME, 'gameassets', 'fileindex', 'platforms.json')
    try:
        config = _archive_config(config_path, os.stat(config_path).st_mtime_ns)
        roots.extend(config.get(catalog.platform, []))
    except FileNotFoundError:
        pass
    if catalog.platform == 'steam':
        roots.append(storage.path('gamedata', 'objects', root=HOME))
    return roots


def find_blob(catalog, obj, md5, verify=False, others=()):
    """MD5 storage first, verified legacy names second. Never guess case-colliding content.
    ``others``: catalogs of other platforms whose files may be byte-identical (a phone's sound and video files are
    Steam's under other ids); their MD5 storage and client cache are searched by checksum after this platform's."""
    stored = store_path(md5)
    if os.path.isfile(stored) and (not verify or _md5_of(stored) == md5.lower()):
        return stored
    for root in dict.fromkeys(_roots(catalog)):
        for name in (md5.lower(), obj):
            f = os.path.join(root, name)
            if os.path.isfile(f) and ((name == md5.lower() and not verify) or _md5_of(f) == md5.lower()):
                return f
    found = client_cache_file(catalog, obj, md5)
    if found:
        return found
    for other in others:
        if other is catalog or md5.lower() not in other.md5_keys:
            continue
        for root in dict.fromkeys(_roots(other)):
            f = os.path.join(root, md5.lower())
            if os.path.isfile(f) and (not verify or _md5_of(f) == md5.lower()):
                return f
        found = client_cache_file(other, None, md5)
        if found:
            return found
    return None


def client_game_folders():
    """Installed game folders whose download caches may hold objects: the window's protected copy, then Steam's."""
    try:
        from engine.cli import game_dir, steam_game_dir
        return [g for g in dict.fromkeys((game_dir(), steam_game_dir())) if g]
    except Exception:
        return []


def client_cache_file(catalog, obj, md5):
    """The object in an installed client's own download cache, or None. The client keeps each downloaded object as
    octo/v1/<app>/<last digit of its id>/<hex of 'A'+id or 'R'+id>/<md5>, so the path follows from the catalog entry
    (``obj``), or from any of the catalog's entries with this content when ``obj`` is None.
    Off when platforms.json has "steam_cache": false. The caller still checks the MD5 before serving it."""
    from engine.catalogs import settings
    if not settings(HOME)['steam_cache']:
        return None
    md5 = md5.lower()
    keys = [catalog.local_keys[obj]] if obj in catalog.local_keys else catalog.md5_keys.get(md5, []) if obj is None else []
    for key in keys:
        for game in client_game_folders():
            f = os.path.join(game, 'octo', 'v1', '3001', key[-1], key.encode('ascii').hex(), md5)
            if os.path.isfile(f):
                return f
    return None


_NOT_A_SOURCE = {"5336b506f8162ca928d92ad7d585b1237e628b8f366d104c5b2ef9af8c629d77"}


def refused(url):
    """True for an address under a domain this program never downloads from (kept as sha256 of the domain)."""
    import hashlib, urllib.parse
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return any(hashlib.sha256(".".join(host.split(".")[-n:]).encode()).hexdigest() in _NOT_A_SOURCE for n in (2, 3))
