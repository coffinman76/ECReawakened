"""Locate and verify user-supplied Octo blobs for the local listener."""
import hashlib, json, os
from functools import lru_cache

HOME = os.environ.get("EC_HOME") or os.getcwd()


def _md5_of(path):
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, 'md5').hexdigest()
    except OSError: return None


def blob_dir(catalog):
    return os.path.join(HOME, 'gameassets', 'blobs', catalog.platform, str(catalog.revision))


@lru_cache(maxsize=8)
def _archive_config(path, modified):
    with open(path, encoding='utf-8-sig') as f:
        config = json.load(f).get('archive_roots', {})
    if not isinstance(config, dict) or any(not isinstance(v, list) or any(not isinstance(p, str) for p in v) for v in config.values()):
        raise ValueError('archive_roots must map platforms to lists of directory paths')
    return config


def find_blob(catalog, obj, md5, verify=False):
    """MD5 storage first, verified legacy names second. Never guess case-colliding content."""
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
    for root in dict.fromkeys(roots):
        for name in (md5.lower(), obj):
            f = os.path.join(root, name)
            if os.path.isfile(f) and ((name == md5.lower() and not verify) or _md5_of(f) == md5.lower()):
                return f
    return None


_NOT_A_SOURCE = {"5336b506f8162ca928d92ad7d585b1237e628b8f366d104c5b2ef9af8c629d77"}


def refused(url):
    """True for an address under a domain this program never downloads from (kept as sha256 of the domain)."""
    import hashlib, urllib.parse
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return any(hashlib.sha256(".".join(host.split(".")[-n:]).encode()).hexdigest() in _NOT_A_SOURCE for n in (2, 3))
