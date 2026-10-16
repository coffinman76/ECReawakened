"""Rebuild offline MasterData from user-supplied tables and engine-owned edits.

No publisher table rows are shipped here. The recipe contains numeric edits;
new offline conditions and shop rows are generated using the user's tables.
Existing valid live files are retained, keeping their catalog URLs stable.

English text is rebuilt the same way when generated rows need names the stock
text lacks: a Gear Exchange costume's name missing from the shop text, and the
names of list cards (live/text/, named by live/x-master-language-path.txt),
unless that reference was set by hand. An added shop item (recipe "shop_item_additions") is named by its
reward's text, copied into the shop text when the shop text lacks it.

Generated Events-list cards and event towers (recipe "list_cards",
"guild_card", "escalation_cards", "event_towers") are built by engine/cards.py, as
are solo copies of co-op fights unless gameplay-settings.json has "coop_as_solo": false.
"""
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil

import lz4.frame
import msgpack
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from engine import cards, codec, storage


def _safe_path(path, root):
    """Reject links and paths outside the chosen installation before writes."""
    path = Path(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'MasterData path escapes the installation: {path}')
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
            raise ValueError(f'MasterData folder contains a symbolic link or junction: {candidate}')
    return path


def _safe_tree(directory, root):
    _safe_path(directory, root)
    if directory.is_dir():
        for child in directory.iterdir():
            _safe_path(child, root)
            if child.is_dir():
                _safe_tree(child, root)


def _digest(data):
    return base64.b32encode(hashlib.sha256(data).digest()).decode().rstrip('=')


def _encrypt(data):
    # Stable inputs produce stable asset URLs. These are public game resources,
    # not secret messages: deriving an IV here is for reproducible local files.
    iv = hashlib.sha256(b'ECR offline MasterData v1\0' + data).digest()[:16]
    return iv + AES.new(codec.FILE_KEY, AES.MODE_CBC, iv).encrypt(pad(data, 16))


def _go_json(value):
    # the publisher's text files are written the way Go's encoder writes them (these escapes, a final newline);
    # matching it keeps unchanged text identical
    text = json.dumps(value, separators=(',', ':'), ensure_ascii=False)
    for character in '&<>\u2028\u2029':
        text = text.replace(character, '\\u%04x' % ord(character))
    return text.encode() + b'\n'


def _language(root, recipe, source):
    """English text files to serve beside the stock ones: ({live file: bytes}, {served path: live file}).

    Gear Exchange rows name each costume by its costume text id, but the client
    looks shop names up in the m_shop_item text, which lacks some costumes. Their
    names are copied there from the m_character_costume text. Generated cards and
    towers get their names (engine/cards.py). Empty when
    nothing is missing or no English text is installed.
    """
    stock = Path(storage.path('gamedata', 'stock', 'language_catalog.enc', root=root, legacy_fallback=False))
    if not (recipe.get('gear_exchange_costumes') or recipe.get('shop_item_additions')
            or cards.wanted(recipe)) or not stock.is_file():
        return {}, {}
    loaded = {}
    try:
        catalog = json.loads(codec.decrypt_file(_safe_path(stock, root).read_bytes()))
        entries = {entry['name']: entry for entry in catalog['files']}
        def text(name):
            if name not in loaded:
                loaded[name] = json.loads(codec.decrypt_file(
                    _safe_path(stock.parent / 'language' / (name + '.enc'), root).read_bytes()))
            return loaded[name]
        wanted = {}                                        # table -> {text id: value}
        if recipe.get('gear_exchange_costumes') and 'm_shop_item' in entries and 'm_character_costume' in entries:
            present = {entry['id'] for entry in text('m_shop_item')['data']}
            names = {entry['id']: entry['value'] for entry in text('m_character_costume')['data']}
            costumes = {row[0]: row for row in source('m_character_costume')}
            for costume_id in recipe['gear_exchange_costumes']:
                text_id = costumes[costume_id][9]
                if text_id not in present and text_id in names:
                    wanted.setdefault('m_shop_item', {})[text_id] = names[text_id]
        if recipe.get('shop_item_additions') and 'm_shop_item' in entries and 'm_reward' in entries:
            present = {entry['id'] for entry in text('m_shop_item')['data']}
            names = {entry['id']: entry['value'] for entry in text('m_reward')['data']}
            rewards = {row[0]: row for row in source('m_reward')}
            for added in recipe['shop_item_additions']:
                text_id = rewards[added['reward']][3]
                if text_id not in present and text_id in names:
                    wanted.setdefault('m_shop_item', {})[text_id] = names[text_id]
        stock_text = lambda name: ({entry['id']: entry['value'] for entry in text(name)['data']}
                                   if name in entries else {})
        for table, values in cards.texts(recipe, source, stock_text).items():
            if table in entries:
                wanted.setdefault(table, {}).update(values)
        changed = {}
        for name, values in sorted(wanted.items()):
            data = text(name)
            present = {entry['id']: entry['value'] for entry in data['data']}
            new = {text_id: value for text_id, value in values.items() if present.get(text_id) != value}
            for text_id in new:
                if text_id in present:
                    raise ValueError(f'{name} text id {text_id} is already used')
            if new:
                data['data'].extend({'id': text_id, 'value': value} for text_id, value in sorted(new.items()))
                data['data'].sort(key=lambda entry: entry['id'])
                changed[name] = data
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise ValueError(f'English text is incomplete or incompatible: {error}') from error
    if not changed:
        return {}, {}
    files, served = {}, {}
    for name, data in changed.items():
        compressed = gzip.compress(_go_json(data), mtime=0)
        payload = _encrypt(compressed)
        entry = entries[name]
        entry.update(hash=_digest(compressed), size=len(payload),
                     path=f"/language_assets/en/{entry['name_hash']}/{_digest(payload)}.json")
        files[f'text/{name}.enc'] = payload
        served[entry['path']] = f'text/{name}.enc'
    encoded = _encrypt(_go_json(catalog))
    reference = '/language_catalogs/en/' + _digest(encoded) + '.json'
    files.update({'text/catalog.enc': encoded, 'x-master-language-path.txt': (reference + '\n').encode()})
    served[reference] = 'text/catalog.enc'
    return files, served


def _generated_language(live):
    """The language reference in live, when the last rebuild wrote it (an older text build), else None."""
    try:
        reference = (live / 'x-master-language-path.txt').read_text(encoding='utf-8').strip()
        served = json.loads((live / 'served_paths.json').read_text())
        return reference if served.get(reference) == 'text/catalog.enc' else None
    except (OSError, ValueError, AttributeError):
        return None


def _table_payload(rows):
    compressed = lz4.frame.compress(msgpack.packb(rows, use_bin_type=True),
        block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False,
        content_checksum=False, block_checksum=False, store_size=False)
    return _encrypt(compressed), _digest(compressed)


def _read_rows(path):
    try:
        rows = msgpack.unpackb(codec.decrypt_file(path.read_bytes()),
                              raw=False, strict_map_key=False)
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise ValueError('expected an array of table rows')
        return rows
    except (OSError, ValueError, TypeError, RuntimeError, msgpack.UnpackException) as error:
        raise ValueError(f'MasterData is incomplete or incompatible: {path.name}: {error}') from error


def _key(row, columns, table):
    try:
        return tuple(row[column] for column in columns)
    except IndexError as error:
        raise ValueError(f'Incompatible MasterData row in {table}: missing key column') from error


def _apply_cells(rows, table, spec):
    columns = spec['key_columns']
    lookup = {}
    for row in rows:
        key = _key(row, columns, table)
        if key in lookup:
            raise ValueError(f'Incompatible MasterData: duplicate row {key} in {table}')
        lookup[key] = row
    for key, column, originals, replacement in spec['cells']:
        row = lookup.get(tuple(key))
        if row is None or column >= len(row):
            raise ValueError(f'MasterData is incomplete or incompatible: {table}, row {key}, column {column} is missing')
        value = row[column]
        if value == replacement:
            continue
        if value not in originals:
            raise ValueError(f'MasterData version conflict: {table}, row {key}, column {column}: '
                             f'found {value!r}, expected {originals!r} or {replacement!r}')
        row[column] = replacement


def _add_offline_rows(tables, recipe, source):
    indexes = {table: {_key(row, recipe['tables'][table]['key_columns'], table): row
                       for row in rows} for table, rows in tables.items()}
    def add(table, row):
        if table in tables:
            key = _key(row, recipe['tables'][table]['key_columns'], table)
            existing = indexes[table].get(key)
            if existing is not None:
                if existing != row:
                    raise ValueError(f'MasterData version conflict: generated offline row {key} in {table} already differs')
            else:
                tables[table].append(row)
                indexes[table][key] = row

    for condition, group, end in ((10029999001, 620009999001, 1000),
                                   (10029999002, 620009999002, 32503593600000)):
        add('m_condition', [condition, 2, 0, 0, 0, 0, end] + [0] * 12)
        add('m_condition_set', [group, 1, 0])
        add('m_condition_set_rel', [group, condition])
    if 'm_gacha_appeal2' in tables:
        existing = {row[0] for row in tables['m_gacha_appeal2']}
        for row in source('m_gacha_appeal'):
            if row[0] not in existing:
                add('m_gacha_appeal2', [row[0], 0, row[0]])
                existing.add(row[0])
    if recipe.get('gear_exchange_costumes'):
        costumes = {row[0]: row for row in source('m_character_costume')}
        for position, costume_id in enumerate(recipe['gear_exchange_costumes']):
            costume = costumes.get(costume_id)
            if costume is None or len(costume) <= 9:
                raise ValueError(f'MasterData is incomplete or incompatible: costume {costume_id} is missing')
            item = 301001016 + position
            reward = 160000000000 + item
            name = recipe.get('gear_exchange_name_overrides', {}).get(str(costume_id), costume[9])
            add('m_shop_item', [item, 301001, 0, 1, 0, 0, reward, 0, 200000210001,
                0, 0, 1000 + position * 10, False, name, 0, 2, 0, 0, 0, 0, False, 0])
            add('m_reward_set', [reward, 1, 0, 0, 0])
            add('m_reward_set_reward_rel', [reward * 100 + 1, reward,
                                          1000000 + costume_id, 1, 1, 0, False])
    # A shop item copied from a neighbour in the same exchange ("like"), selling another reward. Its reward set
    # follows the game's own numbering for one reward (reward id * 10000 + 1); its name is the reward's text id.
    for added in recipe.get('shop_item_additions', []):
        like = next((row for row in tables.get('m_shop_item', []) if row[0] == added['like']), None)
        reward = next((row for row in source('m_reward') if row[0] == added['reward']), None)
        if like is None or reward is None or len(reward) <= 3:
            raise ValueError(f"MasterData is incomplete or incompatible: shop item {added['shop_item']} "
                             f"needs shop item {added['like']} and reward {added['reward']}")
        reward_set = added['reward'] * 10000 + 1
        row = list(like)
        row[0], row[6], row[11], row[13] = added['shop_item'], reward_set, added['order'], reward[3]
        add('m_shop_item', row)
        add('m_reward_set', [reward_set, 1, 0, 0, 0])
        add('m_reward_set_reward_rel', [reward_set * 100 + 1, reward_set, added['reward'], 1, added['count'], 0, False])


def _load_catalog(path):
    try:
        result = json.loads(codec.decrypt_file(path.read_bytes()))
        entries = result['tables']
        names = [entry['table'] for entry in entries]
        if len(set(names)) != len(names):
            raise ValueError('duplicate table names')
        for entry in entries:
            if not isinstance(entry['table'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', entry['table']):
                raise ValueError('invalid table name')
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise ValueError(f'MasterData catalog is incomplete or incompatible: {error}') from error


def _valid_live(live, stock_catalog, expected, text=({}, {})):
    """Check every generated table and text file and its references before retaining it."""
    try:
        files, text_served = text
        for filename, data in files.items():
            if (live / filename).read_bytes() != data:
                return False
        encoded = (live / 'catalog.enc').read_bytes()
        catalog = _load_catalog(live / 'catalog.enc')
        if catalog != json.loads((live / 'catalog.json').read_text(encoding='utf-8-sig')):
            return False
        reference = '/catalogs/' + _digest(encoded) + '.json'
        if (live / 'x-master-path.txt').read_text().strip() != reference:
            return False
        served = json.loads((live / 'served_paths.json').read_text())
        if served.get(reference) != 'catalog.enc':
            return False
        if any(served.get(path) != filename for path, filename in text_served.items()):
            return False
        if not files and _generated_language(live):
            return False
        for filename in served.values():
            if not isinstance(filename, str) or not _safe_path(live / filename, live.resolve()).is_file():
                return False
        current = {entry['table']: entry for entry in catalog['tables']}
        stock = {entry['table']: entry for entry in stock_catalog['tables']}
        if current.keys() != stock.keys():
            return False
        for table, entry in current.items():
            if table not in expected:
                if entry != stock[table]:
                    return False
                continue
            filename = f'tables/{table}.enc'
            payload = (live / filename).read_bytes()
            compressed = codec.aes_decrypt(payload, codec.FILE_KEY)
            if (entry['size'] != len(payload) or entry['hash'] != _digest(compressed)
                    or entry['path'] != f"/assets/{entry['table_hash']}/{_digest(payload)}.dat"
                    or served.get(entry['path']) != filename
                    or _read_rows(live / filename) != expected[table]):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _replace_directory(staged, destination, root):
    """Swap a complete tree, rolling back the previous tree if promotion fails."""
    backup = destination.with_name(destination.name + '.previous')
    for directory in (staged, destination, backup):
        _safe_tree(directory, root)
    if backup.exists():
        raise ValueError(f'Previous MasterData recovery folder exists: {backup}; restore it before retrying')
    moved = destination.exists()
    if moved:
        destination.rename(backup)
    try:
        staged.rename(destination)
    except OSError:
        if moved:
            backup.rename(destination)
        raise
    if moved:
        _safe_tree(backup, root)
        shutil.rmtree(backup)


def _load_recipe(root):
    recipe_file = root / 'engine/data/masterdata-patches.json'
    if not recipe_file.is_file():
        recipe_file = Path(__file__).parent / 'data/masterdata-patches.json'
    recipe = json.loads(recipe_file.read_text(encoding='utf-8-sig'))
    if recipe.get('version') != 1:
        raise ValueError('Unsupported offline MasterData patch recipe version')
    recipe['coop_as_solo'] = _settings(root).get('coop_as_solo', True) is not False   # engine/cards.py build_coop_solo
    return recipe


def _settings(root):
    """gameplay-settings.json, or {} when it is missing or unreadable (engine/rules.py reports that)."""
    try:
        settings = json.loads(Path(storage.path('res', 'settings.json', root=root)).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _expected(recipe, source):
    expected = {}
    for table, spec in recipe['tables'].items():
        expected[table] = copy.deepcopy(source(table))
        _apply_cells(expected[table], table, spec)
    _add_offline_rows(expected, recipe, source)
    cards.build(recipe, source, expected)
    for rows in expected.values():
        rows.sort(key=lambda row: row[0])
    return expected


def check_tables(root, files):
    """Raise ValueError unless the offline edits apply to these tables ({table: encrypted file}). Writes nothing."""
    recipe = _load_recipe(Path(root).resolve())
    cache = {}
    def source(table):
        if table not in files:
            raise ValueError(f'MasterData is incomplete or incompatible: catalog has no {table}')
        if table not in cache:
            cache[table] = _read_rows(Path(files[table]))
        return cache[table]
    _expected(recipe, source)


def ensure_live_data(root):
    """Return True if live data was rebuilt; False when valid live is retained.

    Stock files must already be ingested and validated. Version conflicts are
    reported before touching any installed live table or catalog.
    """
    root = Path(root).resolve()
    stock = _safe_path(Path(storage.path('gamedata', 'stock', root=root, legacy_fallback=False)), root)
    live = _safe_path(Path(storage.path('gamedata', 'live', root=root, legacy_fallback=False)), root)
    _safe_tree(live, root)
    recipe = _load_recipe(root)
    catalog = _load_catalog(_safe_path(stock / 'catalog.enc', root))
    entries = {entry['table']: entry for entry in catalog['tables']}
    cache = {}
    def source(table):
        if table not in entries:
            raise ValueError(f'MasterData is incomplete or incompatible: catalog has no {table}')
        if table not in cache:
            cache[table] = _read_rows(_safe_path(stock / 'tables' / (table + '.enc'), root))
        return cache[table]
    expected = _expected(recipe, source)
    language_reference = live / 'x-master-language-path.txt'
    manual = language_reference.is_file() and not _generated_language(live)
    text = ({}, {}) if manual else _language(root, recipe, source)  # a reference set by hand is left alone
    if _valid_live(live, catalog, expected, text):
        return False
    live.parent.mkdir(parents=True, exist_ok=True)
    staged = storage.new_folder(live.parent, '.live-')
    try:
        (staged / 'tables').mkdir()
        output_catalog = copy.deepcopy(catalog)
        output_entries = {entry['table']: entry for entry in output_catalog['tables']}
        served = {}
        for table, rows in expected.items():
            payload, compressed_hash = _table_payload(rows)
            entry = output_entries[table]
            entry.update(hash=compressed_hash, size=len(payload),
                         path=f"/assets/{entry['table_hash']}/{_digest(payload)}.dat")
            relative = f'tables/{table}.enc'
            (staged / relative).write_bytes(payload)
            served[entry['path']] = relative
        catalog_bytes = json.dumps(output_catalog, separators=(',', ':'), ensure_ascii=False).encode()
        encoded = _encrypt(catalog_bytes)
        reference = '/catalogs/' + _digest(encoded) + '.json'
        served[reference] = 'catalog.enc'
        (staged / 'catalog.enc').write_bytes(encoded)
        (staged / 'catalog.json').write_text(json.dumps(output_catalog, indent=1) + '\n', encoding='utf-8')
        (staged / 'x-master-path.txt').write_text(reference + '\n', encoding='utf-8')
        files, text_served = text
        if files:
            (staged / 'text').mkdir()
        for filename, data in files.items():
            (staged / filename).write_bytes(data)
        served.update(text_served)
        if manual:
            (staged / language_reference.name).write_bytes(language_reference.read_bytes())
        (staged / 'served_paths.json').write_text(json.dumps(served, indent=1) + '\n', encoding='utf-8')
        _replace_directory(staged, live, root)
    finally:
        if staged.exists():
            _safe_tree(staged, root)
            shutil.rmtree(staged)
    return True
