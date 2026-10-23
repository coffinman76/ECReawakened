"""The game's own copy of the master data on this PC, kept current (2026-10-21).

The game keeps every table it downloaded under Documents\\My Games\\FF7EC\\Steam\\<Steam id>\\MasterData (<Table>.bin,
the served file as it came) beside master_catalog.json, the catalog those files came from. At start it downloads a table
only when the new catalog's entry for it differs from the saved catalog's (MasterDataFileManager
CheckAndDownloadAllMasterDataAsync); it never looks at the file itself. So a file that doesn't match its saved entry is
kept for good. On a PC seen 10-21, BattleWave.bin (the REBIRTH crossover's wave movie names blanked) and
Interjection.bin were changed copies from Oct 6, from what was served then (an older Reawakened build, it seems), while
the saved catalog named the stock files, so the game kept them whatever this server sent: the REBIRTH crossover's
Story mark (the book on its stages) stayed grey and its wave movies never played there.

At start the server checks each saved file against its saved entry (the entry's file name is the file's checksum) and
drops the entries whose files don't match from the saved catalog, so the game downloads those tables again. Nothing else
in the folder changes, and a folder whose files all match is left as it is."""
import base64, hashlib, json, os
from pathlib import Path

from engine import codec

CATALOG = "master_catalog.json"


def _digest(data):
    return base64.b32encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def _file_name(table):
    """m_battle_wave -> BattleWave.bin (the game's DatabaseFileUtility names; ignore_m_* drops the prefix too)."""
    name = table.removeprefix("ignore_").removeprefix("m_")
    return "".join(part[:1].upper() + part[1:] for part in name.split("_")) + ".bin"


def stale(folder):
    """The saved catalog's tables whose file in folder doesn't match the entry (missing files don't count: the game
    handles those itself), as [(table, file name)], with the catalog -> ([...], catalog dict)."""
    catalog = json.loads(codec.decrypt_file(Path(folder, CATALOG).read_bytes()))
    out = []
    for entry in catalog.get("tables") or []:
        name = _file_name(entry.get("table", ""))
        try:
            data = Path(folder, name).read_bytes()
        except OSError:
            continue
        if _digest(data) != entry.get("path", "").rsplit("/", 1)[-1].removesuffix(".dat"):
            out.append((entry["table"], name))
    return out, catalog


def refresh(folder):
    """Drop the stale tables' entries from the saved catalog in folder (a MasterData folder) -> the tables dropped."""
    if not os.path.isfile(os.path.join(folder, CATALOG)):
        return []
    tables, catalog = stale(folder)
    if not tables:
        return []
    drop = {t for t, _ in tables}
    catalog["tables"] = [e for e in catalog["tables"] if e.get("table") not in drop]
    f = os.path.join(folder, CATALOG)
    tmp = f + ".part"
    with open(tmp, "wb") as h:
        h.write(codec.aes_encrypt(json.dumps(catalog, separators=(",", ":"), ensure_ascii=False).encode(), codec.FILE_KEY))
    os.replace(tmp, f)
    return sorted(drop)


def refresh_all(documents):
    """Every Steam id's MasterData folder under documents\\My Games\\FF7EC\\Steam -> {folder: tables dropped}, plus
    {folder: error} for a folder that couldn't be read."""
    base = os.path.join(documents, "My Games", "FF7EC", "Steam")
    out = {}
    try:
        ids = sorted(d.name for d in os.scandir(base) if d.is_dir())
    except OSError:
        return out
    for sid in ids:
        folder = os.path.join(base, sid, "MasterData")
        try:
            dropped = refresh(folder)
        except Exception as e:
            out[folder] = e; continue
        if dropped:
            out[folder] = dropped
    return out
