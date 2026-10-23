"""Find and import complete, user-supplied MasterData and LocalizeText offline.

Each candidate is one installation/account and one catalog pair. File counts alone
are insufficient: every catalog entry must match both its encrypted reference and
its compressed content hash. A partial cache is never combined with another one.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil

import msgpack

from engine import codec, storage

MAX_CATALOG_BYTES = 8 << 20
MAX_FILE_BYTES = 32 << 20
MAX_DECODED_BYTES = 64 << 20
MAX_TOTAL_BYTES = 512 << 20
HASH = r'[A-Z2-7]{52}'
NAME = re.compile(r'[A-Za-z0-9_]{1,128}')
RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
            *(f'lpt{i}' for i in range(1, 10))}
# The clean Steam 4.0.10 installation contains complete file lists but only
# baseline tables, including empty placeholders. It is not a completed cache.
BASELINE_CATALOGS = frozenset({
    'U52ZU3BMCXBMZRN3R7PTXHGLXUKIHDFV26TNITQJG2F44HBOAQDA',
    '5IJGAMZ5OQK6CNLM3FLI4SEW3SVBBM5F4CNRVMQWGWW65GNHHYRQ',
})
# Identifies the baseline weapon table independently of the catalog encryption IV.
BASELINE_WEAPON_HASH = 'RVQA3QTFHSFJHL3A2N67IUAVLQC3VP42GXL54URVDNXZQTTDX7BQ'


_ERRORS = (OSError, ValueError, EOFError, msgpack.ExtraData, msgpack.FormatError, msgpack.StackError)


class IncompleteClientData(ValueError):
    """No inspected source contains a complete matching client-data pair."""


def _digest(data):
    return base64.b32encode(hashlib.sha256(data).digest()).decode('ascii').rstrip('=')


def _safe(base, *parts):
    base = Path(base).absolute()
    if any(part in ('..', '.') for part in parts):
        raise ValueError('Client data contains an unsafe path.')
    target = base.joinpath(*parts)
    if not target.is_relative_to(base):
        raise ValueError('Client data path escapes its folder.')
    for item in (base, *reversed(base.parents), *[base.joinpath(*target.relative_to(base).parts[:n])
                                               for n in range(1, len(target.relative_to(base).parts) + 1)]):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('Client data contains a linked path.')
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError('Client data path escapes its folder.')
    return target


def _read(path, limit):
    path = _safe(path.parent, path.name)
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f'{path.name} exceeds its size limit.')
    return raw


def _decode(raw):
    compressed = codec.aes_decrypt(raw, codec.FILE_KEY)
    if compressed.startswith(b'\x04"M\x18'):
        decoder = codec.lz4.frame.LZ4FrameDecompressor()
        try:
            decoded = decoder.decompress(compressed, max_length=MAX_DECODED_BYTES + 1)
        except RuntimeError as exc:
            raise ValueError('Client data compression is invalid.') from exc
        if len(decoded) > MAX_DECODED_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError('Client data compression is invalid or exceeds its size limit.')
    elif compressed.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            decoded = stream.read(MAX_DECODED_BYTES + 1)
    else:
        decoded = compressed
    if len(decoded) > MAX_DECODED_BYTES:
        raise ValueError('Decoded client data exceeds its size limit.')
    return compressed, decoded


def _catalog(raw, kind, reference=None):
    checksum = _digest(raw)
    if checksum in BASELINE_CATALOGS:
        raise ValueError('Bundled baseline data is incomplete; use a completed player cache.')
    prefix = '/catalogs/' if kind == 'master' else '/language_catalogs/en/'
    inferred = prefix + checksum + '.json'
    if reference is not None and reference != inferred:
        raise ValueError('Catalog checksum does not match its reference.')
    _, decoded = _decode(raw)
    parsed = json.loads(decoded)
    collection, key = ('tables', 'table') if kind == 'master' else ('files', 'name')
    entries = parsed.get(collection) if isinstance(parsed, dict) else None
    if not isinstance(entries, list) or not 1 <= len(entries) <= 4096:
        raise ValueError('Catalog has an invalid file list.')
    names, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Catalog has an invalid entry.')
        name = entry.get(key)
        if (not isinstance(name, str) or not NAME.fullmatch(name) or name.casefold() in names
                or name.casefold() in RESERVED):
            raise ValueError('Catalog has unsafe or duplicate file names.')
        names.add(name.casefold())
        asset_prefix = '/assets' if kind == 'master' else '/language_assets/en'
        extension = 'dat' if kind == 'master' else 'json'
        if not re.fullmatch(rf'{asset_prefix}/{HASH}/{HASH}\.{extension}', str(entry.get('path', ''))):
            raise ValueError('Catalog has an invalid file reference.')
        if not re.fullmatch(HASH, str(entry.get('hash', ''))):
            raise ValueError('Catalog has an invalid content checksum.')
        if kind == 'master' and name == 'm_weapon' and entry['hash'] == BASELINE_WEAPON_HASH:
            raise ValueError('Bundled weapon baseline is incomplete; use a completed player cache.')
        if type(entry.get('size')) is not int or not 32 <= entry['size'] <= MAX_FILE_BYTES:
            raise ValueError('Catalog has an invalid file size.')
        total += entry['size']
    if total > MAX_TOTAL_BYTES:
        raise ValueError('Catalog exceeds its size limit.')
    if kind == 'master' and 'm_weapon' not in names:
        raise ValueError('MasterData does not contain the required weapon table.')
    return parsed, inferred


def _verify(raw, entry, kind):
    if len(raw) != entry['size']:
        raise ValueError('File size does not match its catalog.')
    if _digest(raw) != entry['path'].rsplit('/', 1)[1].split('.', 1)[0]:
        raise ValueError('File checksum does not match its catalog reference.')
    compressed, decoded = _decode(raw)
    if _digest(compressed) != entry['hash']:
        raise ValueError('File content checksum does not match its catalog.')
    _check_content(decoded, entry, kind)


def _check_content(decoded, entry, kind):
    if kind == 'master':
        parsed = msgpack.unpackb(decoded, raw=False, strict_map_key=False,
                                 max_str_len=MAX_DECODED_BYTES, max_bin_len=MAX_DECODED_BYTES,
                                 max_array_len=2_000_000, max_map_len=2_000_000)
        if not isinstance(parsed, list):
            raise ValueError('MasterData table is not a row list.')
        if entry['table'] == 'm_weapon' and not parsed:
            raise ValueError('Required weapon table contains only an empty placeholder.')
    else:
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict) or not isinstance(parsed.get('data'), list):
            raise ValueError('LocalizeText file has an invalid data list.')


def _client_name(name):
    """m_weapon -> Weapon; ignore_m_* is also used by the client."""
    name = name.removeprefix('ignore_').removeprefix('m_')
    return ''.join(part[:1].upper() + part[1:] for part in name.split('_'))


@dataclass
class _Candidate:
    folder: Path
    layout: str
    label: str


def _documents():
    """Use the real Windows Documents known folder, including OneDrive redirects."""
    import sys
    if sys.platform == 'darwin':
        return []            # Mac build: nothing to find there, and reading Documents triggers a permission prompt
    folders = []
    if os.name == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            import uuid
            guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID('FDD39AD0-238F-46AF-ADB4-6C85480369C7').bytes_le)
            output = ctypes.c_wchar_p()
            function = ctypes.windll.shell32.SHGetKnownFolderPath
            function.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                 ctypes.POINTER(ctypes.c_wchar_p)]
            function.restype = ctypes.c_long
            if function(ctypes.byref(guid), 0, None, ctypes.byref(output)) == 0:
                try:
                    folders.append(Path(output.value))
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(output, ctypes.c_void_p))
        except (AttributeError, OSError, ValueError):
            pass
    folders.append(Path.home() / 'Documents')
    for key in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.environ.get(key):
            folders.append(Path(os.environ[key]) / 'Documents')
    return list(dict.fromkeys(folders))


def _cache_candidates(parent, label):
    """Look at each Steam account separately; never merge different players."""
    parent = Path(parent)
    patterns = ('My Games/FF7EC/Steam/*', 'Steam/*', '*', 'ec-saves/Steam/*', 'ec-saves/*')
    if (parent / 'MasterData').is_dir() or (parent / 'LocalizeText').is_dir():
        yield _Candidate(parent, 'cache', label)
    for pattern in patterns:
        try:
            folders = sorted(parent.glob(pattern))
        except OSError:
            continue
        for folder in folders:
            try:
                if folder.is_dir() and ((folder / 'MasterData').is_dir() or (folder / 'LocalizeText').is_dir()):
                    yield _Candidate(folder, 'cache', f'{label}: {folder.name}')
            except OSError:
                continue


def _candidates(root, game_folders, documents):
    assets = root / 'gameassets'
    yield _Candidate(assets, 'stock', 'Installation gameassets')
    yield _Candidate(assets, 'effective', 'Installation gameassets with local table modifications')
    documents = _documents() if documents is None else ([Path(documents)] if isinstance(documents, (str, os.PathLike)) else documents)
    for folder in documents:
        yield from _cache_candidates(folder, 'Documents player cache')
    copies = root / 'gamecopies'
    if copies.is_dir():
        for copy in sorted(copies.iterdir()):
            if copy.is_dir():
                yield from _cache_candidates(copy, 'Copied client player cache')
    for game in game_folders:
        game = Path(game)
        yield from _cache_candidates(game, 'Selected player cache')
        for other_assets in (game, game / 'gameassets'):
            if not (other_assets / 'masterdata').is_dir() and not (other_assets / 'language').is_dir():
                continue
            if other_assets.is_dir():
                yield _Candidate(other_assets, 'stock', 'Selected gameassets')
                yield _Candidate(other_assets, 'effective', 'Selected gameassets with local table modifications')
        for baseline in (game / 'FF7EC_Data/StreamingAssets/MasterData', game / 'StreamingAssets/MasterData', game):
            if (baseline / 'index.json').is_file():
                yield _Candidate(baseline, 'streaming', 'Game client installation')


def _reference(folder, name):
    return _read(_safe(folder, name), 1024).decode('utf-8-sig').strip()


def _plan(candidate):
    """A candidate's two catalogs and where it keeps each listed file; nothing is verified yet."""
    folder = _safe(candidate.folder)
    layout = candidate.layout
    effective = layout == 'effective'
    mapping = {}
    references = {}
    if layout in ('stock', 'effective'):
        master = _safe(folder, 'masterdata', 'live' if effective else 'stock')
        language = _safe(folder, 'language', 'stock')
        master_file, language_file = _safe(master, 'catalog.enc'), _safe(language, 'language_catalog.enc')
        references['master'] = _reference(master, 'x-master-path.txt') if effective else _reference(folder / 'masterdata', 'catalog-path.txt')
        references['language'] = _reference(folder / 'language', 'language-path.txt')
        if effective:
            mapping = json.loads(_read(_safe(master, 'served_paths.json'), MAX_CATALOG_BYTES))
            if not isinstance(mapping, dict):
                raise ValueError('Local table path mapping is invalid.')
    elif layout == 'cache':
        master, language = _safe(folder, 'MasterData'), _safe(folder, 'LocalizeText', 'En')
        master_file, language_file = _safe(master, 'master_catalog.json'), _safe(language, 'text_catalog.json')
    else:
        index = json.loads(_read(_safe(folder, 'index.json'), 64 << 10))
        for kind, key, prefix in (('master', 'master_data_catalog', '/catalogs/'),
                                  ('language', 'language_catalog_en', '/language_catalogs/en/')):
            value = index.get(key) if isinstance(index, dict) else None
            if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + HASH + r'\.json', value):
                raise ValueError('Client index contains an invalid catalog path.')
            references[kind] = value
        master_file = _safe(folder, *references['master'].lstrip('/').split('/'))
        language_file = _safe(folder, *references['language'].lstrip('/').split('/'))
    catalogs, jobs = {}, []
    for kind, path in (('master', master_file), ('language', language_file)):
        raw = _read(path, MAX_CATALOG_BYTES)
        parsed, reference = _catalog(raw, kind, references.get(kind))
        if layout in ('stock', 'effective'):
            plain_file = master / 'catalog.json' if kind == 'master' else language / 'language_catalog.json'
            if json.loads(_read(plain_file, MAX_DECODED_BYTES)) != parsed:
                raise ValueError('Decoded catalog does not match its encrypted catalog.')
        catalogs[kind] = (raw, parsed, reference)
        entries = parsed['tables' if kind == 'master' else 'files']
        for entry in entries:
            name = entry['table' if kind == 'master' else 'name']
            if layout == 'cache':
                path = _safe(master, _client_name(name) + '.bin') if kind == 'master' else _safe(language, _client_name(name) + '.json')
            elif layout == 'streaming':
                path = _safe(folder, *entry['path'].lstrip('/').split('/'))
            elif kind == 'language':
                path = _safe(language, 'language', name + '.enc')
            elif effective and entry['path'] in mapping:
                relative = mapping[entry['path']]
                if not isinstance(relative, str) or '\\' in relative or relative.startswith('/'):
                    raise ValueError('Local table mapping contains an unsafe path.')
                path = _safe(master, *relative.split('/'))
            else:
                path = _safe(folder, 'masterdata', 'stock', 'tables', name + '.enc')
            jobs.append((kind, name, entry, path))
    total_bytes = sum(entry['size'] for _, _, entry, _ in jobs)
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValueError('Client data exceeds its size limit.')
    return catalogs, jobs


def _loosen(raw, entry, kind):
    """A catalog entry describing ``raw`` as it is, for a file no folder has in its catalog's version."""
    compressed, decoded = _decode(raw)
    _check_content(decoded, entry, kind)
    folder, extension = entry['path'].rsplit('/', 1)[0], entry['path'].rsplit('.', 1)[1]
    return {**entry, 'size': len(raw), 'hash': _digest(compressed), 'path': f'{folder}/{_digest(raw)}.{extension}'}


def _rebuilt_catalog(kind, parsed, jobs):
    """Re-encode a catalog whose entries were loosened, with the reference the game will request."""
    collection, key = ('tables', 'table') if kind == 'master' else ('files', 'name')
    entries = {name: entry for job_kind, name, entry, _ in jobs if job_kind == kind}
    parsed = {**parsed, collection: [entries.get(entry[key], entry) for entry in parsed[collection]]}
    raw = codec.aes_encrypt(json.dumps(parsed, separators=(',', ':'), ensure_ascii=False).encode(), codec.FILE_KEY)
    prefix = '/catalogs/' if kind == 'master' else '/language_catalogs/en/'
    return raw, parsed, prefix + _digest(raw) + '.json'


def _inspect(candidate, report, pool=None, loose=False):
    """Verify every file a candidate's catalogs list. With ``pool`` ({(kind, name): [paths]} from every
    candidate), a file that is missing or stale here is taken from another folder with a copy matching this
    catalog's checksums; such a copy is the same file. With ``loose``, a file no folder has in that version is
    used as it is when it decodes, and its catalog entry is rewritten to match.
    Returns catalogs, jobs, {name: borrowed path} and {name: path used as it is}."""
    catalogs, jobs = _plan(candidate)
    total_bytes = sum(entry['size'] for _, _, entry, _ in jobs)
    size, borrowed, loosened = 0, {}, {}
    for done, (kind, name, entry, path) in enumerate(jobs, 1):
        error = None
        options = [path, *(p for p in (pool or {}).get((kind, name), ()) if p != path)]
        for option in options:
            try:
                _verify(_read(option, entry['size']), entry, kind)
            except (ValueError, OSError) as exc:
                error = error or exc
                continue
            if option != path:
                borrowed[name] = option
                jobs[done - 1] = (kind, name, entry, option)
            error = None
            break
        if error is not None and loose:
            for option in options:
                try:
                    raw = _read(option, MAX_FILE_BYTES)
                    jobs[done - 1] = (kind, name, _loosen(raw, entry, kind), option)
                except _ERRORS:
                    continue
                loosened[name] = option
                error = None
                break
        if error is not None:
            raise ValueError(f'{name}: {error}' + (' No other folder has a matching copy.' if pool else '')) from error
        size += entry['size']
        report({'phase': 'checking', 'label': candidate.label, 'done': done, 'total': len(jobs),
                'bytes': size, 'total_bytes': total_bytes})
    for kind in {job[0] for job in jobs if job[1] in loosened}:
        catalogs[kind] = _rebuilt_catalog(kind, catalogs[kind][1], jobs)
    return catalogs, jobs, borrowed, loosened


def _install(root, catalogs, jobs, report):
    assets = _safe(root, 'gameassets')
    relatives = ('masterdata/stock', 'language/stock/language', 'language/stock/language_catalog.enc',
                 'language/stock/language_catalog.json', 'masterdata/catalog-path.txt', 'language/language-path.txt')
    targets = [_safe(assets, *relative.split('/')) for relative in relatives]
    for target in targets:
        if target.is_dir():
            for item in target.rglob('*'):
                _safe(root, *item.relative_to(root).parts)
    assets.mkdir(parents=True, exist_ok=True)
    with storage.staging(assets, '.client-ingest-') as temporary:
        stage = Path(temporary)
        for directory in ('masterdata/stock/tables', 'language/stock/language'):
            (stage / directory).mkdir(parents=True)
        for kind, (raw, parsed, reference) in catalogs.items():
            directory = stage / ('masterdata/stock' if kind == 'master' else 'language/stock')
            name = 'catalog' if kind == 'master' else 'language_catalog'
            (directory / (name + '.enc')).write_bytes(raw)
            (directory / (name + '.json')).write_text(json.dumps(parsed, indent=2) + '\n', encoding='utf-8')
            pointer = 'masterdata/catalog-path.txt' if kind == 'master' else 'language/language-path.txt'
            (stage / pointer).write_text(reference + '\n', encoding='utf-8')
        for done, (kind, name, entry, path) in enumerate(jobs, 1):
            raw = _read(path, entry['size'])
            _verify(raw, entry, kind)  # The client may have changed while we checked it.
            directory = 'masterdata/stock/tables' if kind == 'master' else 'language/stock/language'
            (stage / directory / (name + '.enc')).write_bytes(raw)
            report({'phase': 'importing', 'done': done, 'total': len(jobs), 'label': 'Importing existing client tables'})
        backup = stage / '.previous'
        backup.mkdir()
        completed = []
        try:
            for number, (relative, target) in enumerate(zip(relatives, targets)):
                target.parent.mkdir(parents=True, exist_ok=True)
                previous = backup / str(number)
                existed = target.exists()
                if existed:
                    target.rename(previous)
                completed.append((target, previous, existed))
                (stage / relative).rename(target)
        except BaseException:
            for target, previous, existed in reversed(completed):
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                if existed:
                    previous.rename(target)
            raise


def ensure_client_data(root, game_folders=(), documents=None, progress=None, check=None):
    """Return a complete local dataset, importing existing client files if needed.

    ``documents`` can override discovered Documents locations for portable hosts
    or tests. ``game_folders`` accepts player folders, installations, or gameassets.
    ``check`` receives {table: file} for a complete source and raises ValueError to skip
    it, such as a player cache another server patched. When no folder is complete on its
    own, a second pass fills each folder's missing or stale files from the others, and a
    last pass uses files no folder has in their catalog's version as they are.
    The source files, accounts, live overrides and manually imported Japanese data
    are preserved. No network requests are made.
    """
    root = _safe(Path(root).absolute())
    report = progress or (lambda _: None)
    errors = _ERRORS
    candidates, seen = [], set()
    for candidate in _candidates(root, game_folders, documents):
        identity = (str(candidate.folder.absolute()), candidate.layout)
        if identity not in seen:
            seen.add(identity)
            candidates.append(candidate)
    rejected, rejected_folders, failures = [], set(), []
    pool = None
    for attempt in ('alone', 'pooled', 'loose'):
        failures = []
        if attempt == 'pooled':
            pool = {}
            for candidate in candidates:
                try:
                    for kind, name, _, path in _plan(candidate)[1]:
                        pool.setdefault((kind, name), []).append(path)
                except errors:
                    continue
        for candidate in candidates:
            if (candidate.folder, candidate.layout) in rejected_folders:
                continue
            report({'phase': 'searching', 'label': candidate.label, 'source': str(candidate.folder), 'done': 0, 'total': 0})
            try:
                catalogs, jobs, borrowed, loosened = _inspect(candidate, report, pool, attempt == 'loose')
            except errors as exc:
                failures.append(f'{candidate.folder}: {exc}')
                continue
            if check is not None:
                try:
                    check({name: path for kind, name, _, path in jobs if kind == 'master'})
                except ValueError as exc:
                    failures.append(f'{candidate.folder}: {exc}')
                    rejected.append(f'{candidate.folder}: {exc}')
                    rejected_folders.add((candidate.folder, candidate.layout))
                    continue
            installed = (candidate.layout == 'stock' and candidate.folder.absolute() == (root / 'gameassets')
                         and not borrowed and not loosened)
            if not installed:
                _install(root, catalogs, jobs, report)
            result = {'source': str(candidate.folder), 'label': candidate.label,
                      'tables': len(catalogs['master'][1]['tables']),
                      'language_files': len(catalogs['language'][1]['files']), 'imported': not installed,
                      'master_catalog': catalogs['master'][2], 'language_catalog': catalogs['language'][2],
                      'rejected': rejected, 'borrowed': {name: str(path) for name, path in borrowed.items()},
                      'loose': {name: str(path) for name, path in loosened.items()}}
            report({'phase': 'complete', **result})
            return result
    explanation = '\n'.join(failures[-8:])
    raise IncompleteClientData('MasterData and English LocalizeText are incomplete or missing.\n'
                               'Supply a completed player cache with matching catalogs. '
                               'Checked installation gameassets, Documents player folders, and selected client folders. '
                               'The clean client\'s bundled baseline is insufficient.\n' + explanation)
