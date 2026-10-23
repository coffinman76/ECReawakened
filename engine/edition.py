"""Optional edition add-ons (the Mobile Edition's mobile/ folder). ECReawakened.zip ships none, so every hook here
is a no-op for it.

An edition's launcher sets EC_EDITION (for example "mobile"); the engine processes it starts inherit it. The edition's
module may define any of these, and the core calls them at the matching point:
  certificates(folder, hosts, log, system_setup) -> (cas_to_trust, crt, key, sdk_folder)   cli.py serve
  prepare(log)                           before the asset lists are loaded                       net.py
  ready(log, addresses)                  once the server listens                                 cli.py serve
  certificate_files() -> the CA files phones and other PCs install                               cli.py network
  command(args, out) -> True if handled  extra engine.cli commands                               cli.py
  route(handler, host, path, body) -> True if answered                                           net.py
  asset_list(handler, parts) -> True if answered (a platform with no asset list installed)      net.py
  missing_file(handler, obj, md5, target) -> True if answered (a file this PC lacks; target is
                                         where to keep it)                                         net.py
  patch_recipe(recipe), patch_tables(tables, source)                     masterdata_patches.py
"""
import importlib
import os
from pathlib import Path

EDITIONS = {'mobile': 'mobile.server'}
_loaded = {}


def name():
    """The edition this process runs as, or '' for plain Reawakened."""
    value = os.environ.get('EC_EDITION', '').strip().lower()
    return value if value in EDITIONS else ''


def module():
    """The edition's module, or None (no edition, or its folder isn't in this install)."""
    edition = name()
    if not edition:
        return None
    if edition not in _loaded:
        try:
            _loaded[edition] = importlib.import_module(EDITIONS[edition])
        except ImportError:
            _loaded[edition] = None
    return _loaded[edition]


def hook(attribute):
    """The edition's function of that name, or None."""
    found = module()
    return getattr(found, attribute, None) if found is not None else None


def call(attribute, *args, default=None):
    """Call the edition's hook if it has one; otherwise return default."""
    function = hook(attribute)
    return function(*args) if function is not None else default


def source():
    """Bytes identifying the edition's code (its name and its folder's .py files), for build caches."""
    found = module()
    if found is None:
        return b''
    folder = Path(found.__file__).parent
    return name().encode() + b''.join(p.read_bytes() for p in sorted(folder.glob('*.py')))
