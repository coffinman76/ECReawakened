"""Game assets live in gameassets; engine templates and personal saves are separate.

Legacy path translation keeps the engine's existing table and save logic intact.
Migration is explicit, restartable and refuses to overwrite existing files.
"""
import contextlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess

TEMPLATE_FILES = frozenset(('new-account.json', 'tutorial-register.json', 'tutorial-skip.json'))
ENGINE_DATA_FILES = frozenset(('tablekeys.json', 'materia.json', 'lineups.json', 'laps.json'))

def home():
    return Path(os.environ.get("EC_HOME") or os.getcwd())


def path(*parts, root=None, legacy_fallback=True):
    root = Path(root) if root is not None else home()
    old = root.joinpath(*parts)
    if not parts:
        return str(root)
    if parts[0] == "saves":
        new = root.joinpath("accounts", *parts[1:])
    elif parts[0] == "res":
        name = parts[1] if len(parts) > 1 else ""
        if name in ("replies.bin", "skip.pb"):
            new = root.joinpath("diagnostics", "recording-audit", *parts[1:])
        elif name in TEMPLATE_FILES:
            new = root.joinpath("engine", "account-templates", *parts[1:])
            if not old.exists():
                old = root.joinpath("gameassets", "account-templates", *parts[1:])
        elif name in ENGINE_DATA_FILES or name == "settings.json":
            new = (root / "gameplay-settings.json" if name == "settings.json" else
                   root.joinpath("engine", "data", *parts[1:]))
            if not old.exists():
                old = root.joinpath("gameassets", "game-data", "json", *parts[1:])
        else:
            new = root.joinpath("gameassets", "game-data", "json", *parts[1:])
    elif parts[0] == "gamedata":
        tail = parts[1:]
        if tail and tail[0] in ("live", "stock", "season"):
            if len(tail) > 1 and (tail[1] == "language" or tail[1].startswith("language_catalog")):
                new = root.joinpath("gameassets", "language", *tail)
            else:
                new = root.joinpath("gameassets", "masterdata", *tail)
        elif tail and tail[0] == "web":
            relative = Path(*tail[1:])
            new = root.joinpath("gameassets", "notice-media", relative.name)
            if not old.exists():
                old = root.joinpath("gameassets", "game-data", "images", "web", *tail[1:])
        elif tail and tail[0] == "language-path.txt":
            new = root.joinpath("gameassets", "language", *tail)
        else:
            new = root.joinpath("gameassets", "masterdata", *tail)
    else:
        return str(old)
    return str(new if not legacy_fallback or new.exists() or not old.exists() else old)


def migrate(root=None):
    """Move legacy data within this project; keep accounts and artifacts separate."""
    root = (Path(root) if root is not None else home()).resolve()
    if (root / "saves").exists() and (root / "accounts").exists():
        raise FileExistsError("Both saves and accounts exist; refusing to merge personal state")
    for name in ('saves', 'accounts', 'res', 'gamedata'):
        candidate = root / name
        if candidate.exists() and not candidate.resolve().is_relative_to(root):
            raise ValueError(f'Migration folder escapes the project: {name}')
    moves = []
    notices = root / "gameassets" / "game-data" / "images" / "web"
    if notices.is_dir():
        for item in notices.rglob('*'):
            if item.is_file():
                moves.append((item, root / "gameassets" / "notice-media" / item.name))
    nested_notices = root / "gameassets" / "notice-media" / "UploadOnlyRawAssets"
    if nested_notices.is_dir():
        for item in nested_notices.iterdir():
            moves.append((item, nested_notices.parent / item.name))
    templates = root / "gameassets" / "account-templates"
    if templates.is_dir():
        if not templates.resolve().is_relative_to(root):
            raise ValueError('Template folder escapes the project')
        for item in templates.iterdir():
            moves.append((item, root / "engine" / "account-templates" / item.name))
    gameplay = root / "gameassets" / "game-data" / "json"
    if gameplay.is_dir():
        if not gameplay.resolve().is_relative_to(root):
            raise ValueError('Gameplay data folder escapes the project')
        for item in gameplay.iterdir():
            if item.name in ENGINE_DATA_FILES or item.name == "settings.json":
                moves.append((item, Path(path('res', item.name, root=root, legacy_fallback=False))))
    for base in ("res", "gamedata"):
        source = root / base
        if not source.is_dir():
            continue
        for item in list(source.iterdir()):
            if base == "gamedata" and item.name in ("live", "stock", "season"):
                for child in list(item.iterdir()):
                    moves.append((child, Path(path(base, item.name, child.name, root=root, legacy_fallback=False))))
            else:
                moves.append((item, Path(path(base, item.name, root=root, legacy_fallback=False))))
    destinations = set()
    for source, dest in moves:
        if not source.resolve().is_relative_to(root) or not dest.resolve().is_relative_to(root):
            raise ValueError('Migration paths must remain inside the project')
        if dest.resolve() in destinations:
            raise FileExistsError(f'Multiple legacy files target {dest}; resolve them before retrying')
        destinations.add(dest.resolve())
        if dest.exists():
            raise FileExistsError(f"Migration would overwrite {dest}; resolve it before retrying")
    moved = 0
    for source, dest in moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        moved += 1
    if templates.is_dir() and not any(templates.iterdir()):
        templates.rmdir()
    if gameplay.is_dir() and not any(gameplay.iterdir()):
        gameplay.rmdir()
    if nested_notices.is_dir() and not any(nested_notices.iterdir()):
        nested_notices.rmdir()
    for base in ("res", "gamedata"):
        source = root / base
        if source.exists():
            for item in list(source.iterdir()):
                if item.is_dir() and not any(item.iterdir()):
                    item.rmdir()
            if not any(source.iterdir()):
                source.rmdir()
    if (root / "saves").exists():
        if (root / "accounts").exists():
            raise FileExistsError("Both saves and accounts exist; refusing to merge personal state")
        (root / "saves").rename(root / "accounts")
        moved += 1
    for folder in ("masterdata", "language", "notice-media"):
        (root / "gameassets" / folder).mkdir(parents=True, exist_ok=True)
    (root / "engine" / "account-templates").mkdir(parents=True, exist_ok=True)
    (root / "engine" / "data").mkdir(parents=True, exist_ok=True)
    return moved


def new_folder(parent, prefix):
    """A new empty folder in `parent` that takes `parent`'s permissions, like any folder made with mkdir.

    Not tempfile.mkdtemp: from Python 3.12.4 on Windows that gives its folder an access list of its own (SYSTEM,
    Administrators and the folder's owner only). Made by the elevated server, the owner is Administrators, so a folder
    staged that way and renamed into gameassets refuses everyone not running as admin, the account editor included.
    """
    parent = Path(parent)
    for _ in range(100):
        folder = parent / (prefix + secrets.token_hex(6))
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise FileExistsError(f'No free staging folder name in {parent}')


@contextlib.contextmanager
def staging(parent, prefix):
    """new_folder() for a with block, removed with its contents afterwards."""
    folder = new_folder(parent, prefix)
    try:
        yield str(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


ADMIN_ONLY = ('gameassets/masterdata', 'gameassets/language')
WINDOWS = os.name == 'nt'


def reset_permissions(root=None):
    """Windows: give the folders older versions staged with tempfile (see new_folder) their parent's permissions again,
    so users who do not run as admin can read and change them. Needs admin when such a folder is there; harmless
    otherwise. -> [(folder, ok, output)] for each folder present"""
    if not WINDOWS:
        return []
    root = Path(root) if root is not None else home()
    results = []
    for relative in ADMIN_ONLY:
        folder = root.joinpath(*relative.split('/'))
        if folder.is_dir():
            r = subprocess.run(['icacls', str(folder), '/reset', '/T', '/C', '/Q'], capture_output=True, text=True,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            results.append((relative, r.returncode == 0, (r.stdout + r.stderr).strip()))
    return results
