"""The game's own save folder on this PC, kept apart per account (2026-10-10).

Besides what the server holds, the game keeps a local save under Documents\\My Games\\FF7EC\\Steam\\<Steam id>\\GameSave
(the chapter the Story screen last showed, and the like). That folder is per Steam id, and the game runs here under the
player's real Steam id, so a new account here inherited the local save of whatever was last played under that id: the
Story screen opened on that account's chapter (9 or 11 for a Lv 10 account), with no episodes to pick, and the player was
stuck. Seen on two PCs on 10-10; the same account on a PC with no such leftover was fine, and setting the folder aside
fixed both.

While the server runs, GameSave is a junction to saves\\<account>\\GameSave (made as needed), so every account here has
its own and the player's real local save is never touched: that one waits as GameSave.steam and goes back when the
server stops. A junction left by a server that was killed is reused. Nothing is deleted.

A new account's GameSave starts empty, and with no sign-in files in it the game never signs in: it loads its text
and stops at the title (seen 10-14 on an account converted from NeverCrisis; any new or imported account is the same).
So an account's GameSave that lacks them gets a copy of them from another GameSave here (the player's real one first).
Any sign-in will do, as the server plays the account picked in the window whatever id the game remembers."""
import ctypes, json, os, re, shutil

CSIDL_PERSONAL = 5       # the Documents folder (the game's "My Games" lives there, wherever Documents is redirected)
PARKED = "GameSave.steam"
# The game's sign-in files in GameSave (names are hashes the game picks; present in every GameSave that signs in,
# missing from a new one).
SIGN_IN = ("1137adb9f5deb47d61e82a16ced3ca1038e1474be50dbe6a79dcb9d445ed7be1.bytes",
           "974e724af9c9b2fd0850bdaaea248fbec8becc5c4fa8f9316be7ffe900cdcdea.bytes")


def _documents():
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buf) != 0: raise OSError("no Documents folder")
    return buf.value


def steam_id(home):
    """The Steam id the game plays as: game.json (what the launcher saw), else the account signed in to Steam, else the only
    one Steam knows. The same choice as the launcher's Get-SteamId64."""
    try:
        s = json.load(open(os.path.join(home, "game.json"), encoding="utf-8-sig")).get("steamid") or ""
        if re.fullmatch(r"7656119\d{10}", str(s)): return str(s)
    except (OSError, ValueError): pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
            a = int(winreg.QueryValueEx(k, "ActiveUser")[0])
        if a > 0: return str(76561197960265728 + a)
    except Exception: pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            p = winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
        ids = sorted(set(re.findall(r'"(7656119\d{10})"', open(os.path.join(p, "config", "loginusers.vdf"), encoding="utf-8", errors="replace").read())))
        if len(ids) == 1: return ids[0]
    except Exception: pass
    return None


def game_save_dir(home):
    sid = steam_id(home)
    if not sid: return None
    return os.path.join(_documents(), "My Games", "FF7EC", "Steam", sid, "GameSave")


def _is_ours(path, saves_root):
    """A junction of ours: one that points inside saves\\."""
    if not os.path.isjunction(path): return False
    try: return os.path.realpath(path).lower().startswith(os.path.realpath(saves_root).lower() + os.sep)
    except OSError: return False


def attached(home, saves_root):
    """True while a server has the game's GameSave pointed into saves\\ (so a switch of account re-points it)."""
    try: gs = game_save_dir(home); return bool(gs) and _is_ours(gs, saves_root)
    except OSError: return False


def attach(home, saves_root, uid):
    """Point the game's GameSave at saves\\<uid>\\GameSave. -> a line for the log."""
    if os.name != "nt": return "local save: not Windows, left alone"
    gs = game_save_dir(home)
    if not gs: return "local save: Steam account unknown, the game's own GameSave is left as it is"
    target = os.path.join(saves_root, str(uid), "GameSave"); os.makedirs(target, exist_ok=True)
    parent = os.path.dirname(gs); os.makedirs(parent, exist_ok=True)
    parked = os.path.join(parent, PARKED)
    if _is_ours(gs, saves_root):
        if os.path.realpath(gs).lower() == os.path.realpath(target).lower():
            seeded = seed_sign_in(target, _donors(saves_root, parked))
            return f"local save: GameSave already this account's ({uid})" + (f"; sign-in copied from {seeded}" if seeded else "")
        _unlink_junction(gs)               # a junction: removes the link only, its folder stays
    elif os.path.lexists(gs):
        if os.path.lexists(parked): return f"local save: {parked} is already there, so GameSave was left as it is (move one of them away)"
        os.rename(gs, parked)
    import _winapi
    _winapi.CreateJunction(target, gs)
    seeded = seed_sign_in(target, _donors(saves_root, parked))
    return (f"local save: GameSave -> saves\\{uid}\\GameSave" + (f" (the real one waits as {PARKED})" if os.path.lexists(parked) else "")
            + (f"; sign-in copied from {seeded}" if seeded else ""))


def _donors(saves_root, parked):
    """Where a missing sign-in is copied from: the player's real GameSave, then every account's here."""
    try: names = sorted(os.listdir(saves_root))
    except OSError: names = []
    return [parked] + [os.path.join(saves_root, d, "GameSave") for d in names]


def seed_sign_in(target, donors):
    """Give target (an account's GameSave) the game's sign-in files when it has none, from the first donor folder that
    has them all. -> the donor used, or None (target already had them, or no donor has them)."""
    if all(os.path.isfile(os.path.join(target, f)) for f in SIGN_IN): return None
    for d in donors:
        try:
            if os.path.realpath(d).lower() == os.path.realpath(target).lower(): continue
            if not all(os.path.isfile(os.path.join(d, f)) for f in SIGN_IN): continue
            for f in SIGN_IN: shutil.copy2(os.path.join(d, f), os.path.join(target, f))
            return d
        except OSError: continue
    return None


def _attrs(path):
    try: return hex(os.stat(path, follow_symlinks=False).st_file_attributes)
    except OSError as e: return repr(e)


def _writable(path):
    """Clear the read-only mark on the link itself (SetFileAttributes does not follow it)."""
    k = ctypes.windll.kernel32; a = k.GetFileAttributesW(path)
    if a != 0xFFFFFFFF and a & 1: k.SetFileAttributesW(path, a & ~1)


def _unlink_junction(gs):
    """Take out the junction (the link only). Seen refused with Access denied on a Documents folder kept by OneDrive
    (10-11), so: plain, then without a read-only mark, then as a link, then by cmd, a few times over."""
    import subprocess, time
    first = None
    for n in range(4):
        for how in (lambda: os.rmdir(gs),
                    lambda: (_writable(gs), os.rmdir(gs)),
                    lambda: os.unlink(gs),
                    lambda: subprocess.run(["cmd", "/c", "rmdir", gs], check=True, capture_output=True)):
            try: how()
            except (OSError, NotImplementedError, subprocess.CalledProcessError) as e: first = first or e
            if not os.path.lexists(gs): return
        time.sleep(0.5 * (n + 1))
    raise OSError(f"GameSave link not removed ({first!r}; attributes {_attrs(gs)})")


def _rename(src, dst):
    import time
    for n in range(4):
        try: os.rename(src, dst); return
        except PermissionError:
            if n == 3: raise
            time.sleep(0.5 * (n + 1))


def detach(home, saves_root):
    """Take our junction out and put the real GameSave back. -> a line for the log, or None when there was nothing to do."""
    if os.name != "nt": return None
    gs = game_save_dir(home)
    if not gs or not _is_ours(gs, saves_root): return None
    _unlink_junction(gs)
    parked = os.path.join(os.path.dirname(gs), PARKED)
    if os.path.lexists(parked): _rename(parked, gs); return "local save: the real GameSave is back"
    return "local save: junction removed"
