"""Strict per-platform Octo catalogs for the experimental EC Local fork.

Input is *decrypted protobuf* (Octo Database), not encrypted octocacheevai.
This module has no filesystem side-effects other than reading configured files.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from functools import cached_property, lru_cache

MAX_CATALOG_SIZE = 64 * 1024 * 1024


def _varint(data: bytes, offset: int):
    val = 0
    for n in range(10):
        if offset >= len(data):
            raise ValueError('Truncated protobuf varint')
        b = data[offset]
        offset += 1
        val |= (b & 127) << (7 * n)
        if b < 128:
            return val, offset
    raise ValueError('Oversized protobuf varint')


def _encode_varint(value: int):
    if value < 0:raise ValueError('Negative varint')
    b = bytearray()
    while value > 127:
        b.append((value & 127) | 128)
        value >>= 7
    b.append(value)
    return bytes(b)


def fields(raw: bytes):
    """Yield (field number, wire type, interpreted value, original encoded field)."""
    pos = 0
    while pos < len(raw):
        begin = pos
        tag, pos = _varint(raw, pos)
        field, wire = tag >> 3, tag & 7
        if not field: raise ValueError('Invalid protobuf field zero')
        if wire == 0:
            value, pos = _varint(raw, pos)
        elif wire == 2:
            length, pos = _varint(raw, pos)
            if length > len(raw) - pos: raise ValueError('Truncated length-delimited field')
            value = raw[pos:pos + length]
            pos += length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > len(raw):raise ValueError('Truncated fixed field')
            value = raw[pos:pos + size]
            pos += size
        else:
            raise ValueError(f'Invalid/unsupported wire type {wire}')
        yield field, wire, value, raw[begin:pos]


def replace_url_format(payload: bytes, new_url: str):
    """Replace only database field 5, preserving every other protobuf field."""
    if not new_url.startswith('https://') or new_url.count('{o}') != 1:
        raise ValueError('url_format_override must be HTTPS and contain {o} once')
    value = new_url.encode('utf8')
    modified = False
    result = bytearray()
    for field, wire, data, original in fields(payload):
        if field == 5:
            if wire != 2:raise ValueError('Unexpected Octo URL field type')
            result.extend(_encode_varint((5 << 3) | 2))
            result.extend(_encode_varint(len(value)))
            result.extend(value)
            modified = True
        else:
            result.extend(original)
    if not modified:raise ValueError('Catalog missing urlFormat field 5')
    return bytes(result)


def inspect_catalog(payload: bytes):
    revision = None
    url = None
    md5s = {}
    count = 0
    total = 0
    for f, wire, val, _ in fields(payload):
        if f == 1 and wire == 0:
            revision = val
        elif f == 5 and wire == 2:
            url = val.decode('utf8')
        elif f in (2, 4) and wire == 2:
            item = {k:v for k, _wire, v, _raw in fields(val)}
            if item.get(9) == 4:  # DELETE state
                continue
            name, md5 = item.get(11), item.get(10)
            if name is not None and md5 is not None:
                key = name.decode('ascii')
                digest = md5.decode('ascii').lower()
                if key in md5s and md5s[key] != digest:
                    raise ValueError(f'Conflicting hashes for Octo object {key}')
                md5s[key] = digest
                count += 1
                total += item.get(4, 0)
    if revision is None or url is None or count == 0:
        raise ValueError('Not a complete Octo database')
    return revision, url, md5s, count, total


@dataclass(frozen=True)
class Catalog:
    platform_id: str
    label: str
    data: bytes
    revision: int
    url_format: str
    md5s: Mapping[str, str]
    count: int
    total_bytes: int

    @property
    def platform(self):
        return self.label

    def entries(self):
        return self._entries

    @cached_property
    def _entries(self):
        out = {}
        for field, wire, value, _ in fields(self.data):
            if field in (2, 4) and wire == 2:
                item = {k: v for k, _, v, _ in fields(value)}
                if item.get(9) == 4 or 10 not in item or 11 not in item:
                    continue
                obj, md5 = item[11].decode('ascii'), item[10].decode('ascii').lower()
                if not re.fullmatch(r'[A-Za-z0-9]{1,16}', obj) or not re.fullmatch(r'[0-9a-f]{32}', md5):
                    raise ValueError('Invalid Octo object or MD5')
                size = item.get(4, 0)
                if not isinstance(size, int) or size < 0:
                    raise ValueError('Invalid Octo object size')
                if md5 in out and out[md5][2] != size:
                    raise ValueError('Conflicting sizes for the same MD5')
                out[md5] = (obj, md5, size)
        return list(out.values())


class CatalogRegistry:
    def __init__(self, catalogs: dict[str, Catalog]):
        self.catalogs = catalogs
        self.objects = {}
        self.locations = {}
        for c in catalogs.values():
            c.entries()  # validate identifiers before using them as paths
            for obj, md5 in c.md5s.items():
                old = self.objects.get(obj)
                if old is not None and old != md5:
                    raise ValueError(f'Object name collision across platforms: {obj}')
                self.objects[obj] = md5
                self.locations.setdefault(obj, []).append(c)

    def get(self, platform_id: str):
        return self.catalogs.get(platform_id)

    @classmethod
    def from_config(cls, root: Path, config_path: Path):
        config = json.loads(config_path.read_text(encoding='utf8'))
        catalogs = {}
        for ident, entry in config['platforms'].items():
            if not ident.isdecimal() or len(ident) > 20:
                raise ValueError(f'Invalid numeric platform identifier {ident!r}')
            relative = Path(entry['file'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Catalog file paths must remain within the fork directory')
            resolved = (root / relative).resolve()
            if not resolved.is_relative_to(root.resolve()):
                raise ValueError('Catalog symlink escapes the fork directory')
            if resolved.stat().st_size > MAX_CATALOG_SIZE:
                raise ValueError(f'Catalog is too large: {ident}')
            raw = resolved.read_bytes()
            # SecureFile mode 0 is NOT a protobuf; require already-extracted database.
            override = entry.get('url_format_override')
            if override:raw = replace_url_format(raw, override)
            rev, url, md5, count, total = inspect_catalog(raw)
            expected = entry.get('expected_revision')
            if expected is not None and rev != expected:
                raise ValueError(f'Catalog {ident} revision {rev} != expected {expected}')
            catalogs[ident] = Catalog(ident, entry.get('label', ident), raw, rev, url, md5, count, total)
        return cls(catalogs)


DEFAULT_IDS = {'steam': '323173179', 'android': '223173179', 'ios': '123173170'}


@lru_cache(maxsize=12)
def _read_catalog(filename, modified, size, ident, platform):
    candidate = Path(filename)
    if size > MAX_CATALOG_SIZE:
        raise ValueError('Catalog exceeds 64 MiB')
    raw = candidate.read_bytes()
    rev, url, md5s, count, total = inspect_catalog(raw)
    if not url.startswith('https://') or url.count('{o}') != 1:
        raise ValueError('Invalid catalog URL format')
    if candidate.name == 'catalog.pb' and candidate.parent.name != str(rev):
        raise ValueError(f'Catalog revision does not match folder: {candidate}')
    return Catalog(ident, platform, raw, rev, url, md5s, count, total)


def load(root=None):
    """Discover imported full databases. iOS ID remains configurable until live verified."""
    from engine import storage
    root = Path(root) if root is not None else storage.home()
    config_file = root / 'gameassets' / 'fileindex' / 'platforms.json'
    config = json.loads(config_file.read_text(encoding='utf-8-sig')) if config_file.exists() else {}
    ids = dict(DEFAULT_IDS, **config.get('platform_ids', {}))
    catalogs = {}
    for platform, ident in ids.items():
        if platform not in DEFAULT_IDS or not isinstance(ident, str) or not ident.isdecimal():
            raise ValueError('Invalid platform ID configuration')
        candidates = list((root / 'gameassets' / 'fileindex' / platform).glob('*/catalog.pb'))
        legacy = Path(storage.path('gamedata', 'assets.list', root=root))
        if platform == 'steam' and not candidates and legacy.is_file():
            candidates = [legacy]
        if not candidates:
            continue
        versions = []
        for candidate in candidates:
            stat = candidate.stat()
            versions.append(_read_catalog(str(candidate), stat.st_mtime_ns, stat.st_size, ident, platform))
        catalogs[ident] = max(versions, key=lambda c: c.revision)
    return CatalogRegistry(catalogs)


def select(platform='steam', root=None):
    registry = load(root)
    for catalog in registry.catalogs.values():
        if catalog.platform == platform:
            return catalog
    raise ValueError(f'No {platform} catalog supplied. Place an existing catalog in gameassets/fileindex/ingest/{platform}.')


def _securefile_payload(raw):
    """Read the client's existing SecureFile wrapper; no standalone parser needed."""
    if raw[:1] == b'\x00':
        return raw[1:]
    if raw[:1] != b'\x01':
        return raw
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    ciphertext = raw[1:]
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError('Invalid encrypted Octo catalog length')
    iv = hashlib.md5(b'LvAUtf+tnz').digest()
    for seed in ('132mfd8uvkum78h6', '57lk6xzzeqmy4qay', '2heo6veklua365wl',
                 'bqra7ocyyu7he9b5', 'LkFwaWIGcHJvdG8z', 'bnVtc2IGcHJvdG8z',
                 'dGl0eWIGcHJvdG8z', 'v14uh2c77pv8xkfe'):
        key = hashlib.md5(seed.encode()).digest()
        head = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext[:32])
        if len(head) < 17 or head[16] != 8:
            continue
        try:
            tagged = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext), 16)
        except ValueError:
            continue
        if len(tagged) > 16 and hashlib.md5(tagged[16:]).digest() == tagged[:16]:
            return tagged[16:]
    raise ValueError('The supplied Octo catalog is corrupt or uses an unsupported format')


def ingest(platform, source, root=None):
    """Accept encrypted/plain SecureFile or a decrypted Octo database; never modify input."""
    from engine import storage
    root = Path(root) if root is not None else storage.home()
    if platform not in DEFAULT_IDS:
        raise ValueError('Select steam, android or ios')
    source = Path(source)
    if source.stat().st_size > MAX_CATALOG_SIZE:
        raise ValueError('Catalog exceeds 64 MiB')
    raw = source.read_bytes()
    raw = _securefile_payload(raw)
    rev, url, md5s, count, total = inspect_catalog(raw)
    candidate = Catalog(DEFAULT_IDS[platform], platform, raw, rev, url, md5s, count, total)
    candidate.entries()
    if not url.startswith('https://') or url.count('{o}') != 1:
        raise ValueError('Catalog download URL must be HTTPS with one {o} placeholder')
    try:
        registry = load(root)
    except ValueError as e:
        if str(e) != 'At least one catalog is required':
            raise
        registry = None
    if registry:
        CatalogRegistry({**{k:c for k,c in registry.catalogs.items() if c.platform != platform}, candidate.platform_id: candidate})
    folder = root / 'gameassets' / 'fileindex' / platform / str(rev)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'catalog.pb'
    temp = target.with_suffix('.part')
    temp.write_bytes(raw)
    os.replace(temp, target)
    return {'platform': platform, 'revision': rev, 'records': count, 'objects': len(candidate.entries()), 'path': str(target)}
