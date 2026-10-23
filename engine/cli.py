"""Command line for the launcher window. Every command prints one JSON line (except serve, which logs).

    python -m engine.cli serve [--parent <pid>]
                                          certificate + hosts entries, then answer the game until stopped; with
                                          --parent, also stops (and takes the hosts entries out) once that process ends
    python -m engine.cli unhost           take the hosts entries back out (after a stop)
    python -m engine.cli list             accounts and which one plays
    python -m engine.cli use <id>         play this account
    python -m engine.cli new              start a new account and play it
    python -m engine.cli import <file> [--replace]
    python -m engine.cli export <id> <file>
                                          write an account as an account export, the reverse of import
    python -m engine.cli remove <id>      take an account off the list; its folder moves to accounts/removed, not deleted
    python -m engine.cli prepare-client-data [--game <folder>]...
                                          find and import complete local MasterData and LocalizeText
    python -m engine.cli seasons          the season passes and old guild battles the window offers, and the ones picked
"""
import json, logging, logging.handlers, os, re, shutil, signal, sys, threading, time
from engine import edition, storage

HOME = os.environ.setdefault("EC_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(HOME)


def out(**d): print(json.dumps(d), flush=True)


def saves():
    from engine.net import Saves
    return Saves(None)


def game_dir():
    """The game folder in use: the protected copy named in game.json (the window's Protect), else Steam's, else None."""
    try:
        c = json.load(open(os.path.join(HOME, "game.json"), encoding="utf-8-sig")).get("copy") or ""
        if c and os.path.isfile(os.path.join(c, "FF7EC" + ".exe")): return c
    except (OSError, ValueError): pass
    return steam_game_dir()


def steam_game_dir():
    """Steam's FF7EC folder, or None."""
    roots = [os.path.join(os.environ.get(v, ""), "Steam") for v in ("ProgramFiles(x86)", "ProgramFiles")]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\"))
    except Exception: pass
    libs = []
    for r in roots:
        if not r or not os.path.isfile(os.path.join(r, "steam.exe")): continue
        libs.append(r)
        try: libs += [m.replace("\\\\", "\\") for m in re.findall(r'"path"\s*"([^"]+)"', open(os.path.join(r, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace").read())]
        except OSError: pass
    for lib in libs:
        g = os.path.join(lib, "steamapps", "common", "FF7EC")
        if os.path.isfile(os.path.join(g, "FF7EC" + ".exe")): return g
    return None


def last_run_game_dir():
    """The folder the game last ran from, as its own Player.log says ("Loading player data from <folder>/FF7EC_Data/..."),
    so a game kept outside Steam and the protected copy is found too. None if the log is missing or the folder is gone."""
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    logs = os.path.join(os.path.dirname(local), "LocalLow", "SquareEnix", "FF7EC")
    for name in ("Player.log", "Player-prev.log"):
        try:
            with open(os.path.join(logs, name), encoding="utf-8", errors="replace") as f: head = f.read(8192)
        except OSError: continue
        m = re.search(r"Loading player data from (.+?)[\\/]FF7EC_Data[\\/]data\.unity3d", head)
        if m and os.path.isfile(os.path.join(m[1], "FF7EC" + ".exe")): return os.path.normpath(m[1])
    return None


def installed_index(notes=None):
    """No Steam asset index yet (MasterLanguageData.zip not extracted): import the one the game itself keeps, from the
    game copy, Steam's folder and the folder the game last ran from (the newest revision is served). Never fails the
    caller; a file that will not read is skipped, and why goes in notes."""
    from engine.catalogs import ingest, select
    notes = [] if notes is None else notes
    try:
        select('steam', HOME); return []
    except ValueError: pass
    folders = list(dict.fromkeys(d for d in (game_dir(), steam_game_dir(), last_run_game_dir()) if d))
    if not folders: notes.append("game folder not found (no protected copy, no Steam FF7EC folder, and the game's "
                                 "Player.log names none)")
    imported = []
    for g in folders:
        f = os.path.join(g, "octo", "pdb", "3001", "323173179", "octocacheevai")
        if not os.path.isfile(f):
            notes.append(f"no asset index at {f}"); continue
        try: imported.append(dict(ingest('steam', f, HOME), source=f))
        except Exception as e: notes.append(f"could not read {f}: {e}")
    if imported: notes.clear()
    return imported


def saved_list(blob):
    """Write the game's saved copy of the asset list (plain form: one zero byte, then the list) so it matches what this
    server serves. The game's own encrypted copy fails to load on every start otherwise."""
    g = game_dir()
    if not g: return "game folder not found"
    f = os.path.join(g, "octo", "pdb", "3001", "323173179", "octocacheevai")
    want = b"\x00" + blob
    try:
        if open(f, "rb").read() == want: return "already current"
    except OSError: pass
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".part"
    with open(tmp, "wb") as h: h.write(want)
    os.replace(tmp, f)
    return f"written, {len(want)} bytes"


class Parent:
    """The window that started this server. gone() is True once that process has ended, however it ended (closed,
    killed, crashed), so the server never outlives its window and its hosts entries never outlive the server."""
    def __init__(self, pid):
        self.pid = pid; self.h = None; self.k = None
        if pid and os.name == "nt":
            import ctypes
            self.k = ctypes.WinDLL("kernel32", use_last_error=True)
            self.k.OpenProcess.restype = ctypes.c_void_p
            self.k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self.k.WaitForSingleObject.restype = ctypes.c_uint32
            self.k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self.h = self.k.OpenProcess(0x00100000, 0, pid) or None      # SYNCHRONIZE: enough to ask "has it ended?"

    def watching(self):
        return bool(self.pid) and (os.name != "nt" or self.h is not None)

    def gone(self):
        if not self.watching(): return False
        if os.name == "nt": return self.k.WaitForSingleObject(self.h, 0) == 0     # 0 = ended; 0x102 = still running
        try: os.kill(self.pid, 0); return False
        except ProcessLookupError: return True
        except OSError: return False


def cmd_serve(parent_pid=0, bind='0.0.0.0', port=443, system_setup=True):
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    sys.stderr = open(os.path.join(HOME, "logs", "console-errors.txt"), "w", encoding="utf-8", buffering=1)   # tracebacks the window shows
    root = logging.getLogger(); root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(os.path.join(HOME, "logs", "server.log"), maxBytes=5 << 20, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")); root.addHandler(fh)
    log = logging.getLogger("serve")

    class Printed:
        def write(self, s):
            s = s.strip()
            if s: logging.getLogger("rules").info(s)
        def flush(self): pass
    from engine import certs, net
    if net.port_taken(port):
        log.error("could not start: something on this PC already answers on 127.0.0.1 port %d (another copy of this "
                  "program, or another local server). Close it, then click Start again.", port)
        sys.exit(3)
    if system_setup:   # folders older versions left readable only by admins (storage.new_folder)
        for folder, ok, msg in storage.reset_permissions(HOME):
            if not ok: log.error('permissions: could not reset %s: %s', folder, msg)
    try:
        data = prepare_client_data()
        log.info('client data: %s', data.get('using') or data.get('source', 'ready'))
    except Exception as error:
        if getattr(error, 'using', None): log.error('client data: %s', error.using)
        log.error('could not start: %s', error)
        sys.exit(1)
    made = edition.call('certificates', os.path.join(HOME, "certs"), net.HOSTS, log, system_setup)
    if made:
        trusted, crt, key, sdk_folder = made
    else:
        ca, crt, key = certs.ensure(os.path.join(HOME, "certs"), net.HOSTS)
        sdk_ca, sdk_folder = certs.ensure_sdk(os.path.join(HOME, 'certs'))
        trusted = (ca, sdk_ca)
    if system_setup:
        for certificate in trusted:
            ok, msg = certs.trust(certificate)
            if not ok:
                log.error('could not trust the certificate: %s', msg); sys.exit(2)
        for thumbprint, ok, msg in certs.trust_shared_removed():
            if ok: log.info('certificates: no longer trusting the shared certificate older builds used (%s)', thumbprint)
            else: log.error('certificates: could not remove the shared certificate %s: %s', thumbprint, msg)
    from engine import localsave
    def local_save():   # the game's own save folder: this account's, not whatever was last played under this Steam id
        try:
            uid = saves().active()
            if uid: log.info(localsave.attach(HOME, saves().root, uid))
        except Exception as e: log.error("local save: not set up (%r); the game's own GameSave is used as it is", e)
    if system_setup:
        local_save()
        try:
            from engine.catalogs import select
            notes = []
            for c in installed_index(notes):    # the window imports at its start; this covers a start it skipped
                log.info('asset index: imported steam r%s from %s', c['revision'], c['source'])
            for n in notes: log.warning('asset index: %s', n)
            log.info('saved asset list: %s', saved_list(select('steam', HOME).data))
        except Exception as e: log.error("saved asset list: %r", e)
        try:    # tables the game kept from another server's changed copies: it downloads them again (engine\mastercache.py)
            from engine import localsave, mastercache
            for folder, dropped in mastercache.refresh_all(localsave._documents()).items():
                if isinstance(dropped, Exception): log.error("saved master data: %s not checked (%r)", folder, dropped)
                else: log.info("saved master data: the game downloads %s again (%s)", ", ".join(dropped), folder)
        except Exception as e: log.error("saved master data: not checked (%r)", e)
    stop = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if s is not None: signal.signal(s, lambda *_: stop.set())
    srv = None; hosts_attempted = firewall_attempted = False
    try:
        sys.stdout = Printed()
        srv = net.start(crt, key, port, bind, sdk_folder, integrate_steam=system_setup)
        # Bind succeeded before changing Windows routing or allowing LAN connections.
        if system_setup:
            from engine import network
            if bind != '127.0.0.1':
                firewall_attempted = True
                network.firewall(HOME, True, port)
            names = net.HOSTS + [net.SDK_HOST]
            hosts_attempted = True
            n = certs.hosts(True, names, blocked=net.BLOCKED)
            if n: log.info('hosts: %d conflicting lines; ours are above them', n)
            log.info('LAN addresses: %s', ', '.join(network.lan_addresses()) or 'none detected')
            edition.call('ready', log, network.lan_addresses())
        log.info("ready: start the game")
        flag = os.path.join(HOME, "logs", "stop.flag")
        if os.path.exists(flag): os.remove(flag)
        try: parent = Parent(parent_pid)
        except Exception as e: parent = Parent(0); log.error("could not watch the window (process %s): %r", parent_pid, e)
        if parent_pid: log.info("window: process %d, %s", parent_pid, "the server stops when it ends" if parent.watching() else "not watched")
        while not stop.is_set() and not os.path.exists(flag):
            if parent.gone(): log.info("the window that started this server is gone; stopping"); break
            time.sleep(0.5)
    except Exception as e:
        log.exception('could not start or run: %s', e)
        raise
    finally:
        if srv:
            srv.shutdown(); srv.server_close()
        try:
            m = localsave.detach(HOME, saves().root) if system_setup else None
            if m: log.info(m)
        except Exception as e: log.error("local save: not put back (%r); GameSave.steam next to it is the real one", e)
        if system_setup:
            from engine import network
            try:
                if hosts_attempted: certs.hosts(False, net.HOSTS + [net.SDK_HOST])
            finally:
                if firewall_attempted: network.firewall(HOME, False)
        log.info("stopped; hosts entries removed")


def cmd_unhost():
    from engine import certs, net
    from engine import network
    certs.hosts(False, net.HOSTS + [net.SDK_HOST]); network.firewall(HOME, False); out(ok=True)


def cmd_list():
    s = saves()
    out(ok=True, active=s.active(), accounts=[{"id": i, "name": n, "origin": o} for i, n, o in s.listing()])


def cmd_use(uid):
    s = saves()
    if not s.exists(uid): out(ok=False, error=f"no account {uid}"); return
    s.set_active(uid)
    try:   # a running server has the game's GameSave pointed at the account that was playing: point it at this one
        from engine import localsave
        if localsave.attached(HOME, s.root): localsave.attach(HOME, s.root, uid)
    except Exception: pass
    out(ok=True, active=uid)


def cmd_new():
    """A fresh account from the template, as the game gets on its first start with no account here."""
    import random
    from engine import vault
    s = saves(); uid = str(random.randint(10**17, 9 * 10**17))
    while s.exists(uid): uid = str(random.randint(10**17, 9 * 10**17))
    vault.create_account(os.path.join(s.root, uid), uid, "new"); s.set_active(uid)
    try:   # as on a switch: a running server points the game's GameSave at the new account, not the last one played
        from engine import localsave
        if localsave.attached(HOME, s.root): localsave.attach(HOME, s.root, uid)
    except Exception: pass
    out(ok=True, id=uid)


def cmd_import(path, replace):
    from engine import intake, vault
    try:
        c = intake.convert(json.loads(open(path, "rb").read().decode("utf-8-sig")), "file")
    except Exception as e:
        out(ok=False, error=f"That file isn't an account export ({e})."); return
    s = saves(); uid = c["uid"]; folder = os.path.join(s.root, uid)
    if s.exists(uid):
        if not replace: out(ok=False, exists=True, id=uid, name=c["name"]); return
        dst = os.path.join(s.root, "old", f"{uid}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copytree(folder, dst)
        vault.remove_play_state(folder)
    c["meta"]["name"] = c["name"]
    intake.write(folder, c); s.set_active(uid)
    out(ok=True, id=uid, name=c["name"], gifts=len(c["gifts"]))


def cmd_export(uid, path):
    from engine import intake
    s = saves()
    if not s.exists(uid): out(ok=False, error=f"No account {uid}."); return
    d = intake.export(os.path.join(s.root, uid))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    out(ok=True, id=uid, path=path)


def cmd_remove(uid):
    from engine.account_storage import remove_account
    out(**remove_account(saves(), uid))






def cmd_seasons():
    from engine import seasons
    out(ok=True, chosen=seasons.chosen(HOME), seasons=seasons.listing(HOME),
        guild_battle=seasons.chosen_guild_battle(HOME), guild_battles=seasons.guild_listing(HOME),
        farm_visitor=seasons.chosen_farm_visitor(HOME), farm_visitors=seasons.farm_listing(HOME))


def prepare_client_data(game_folders=(), documents=None):
    """Prepare user-owned data before gameplay modules are loaded; no network requests."""
    from engine.client_ingest import ensure_client_data
    from engine.masterdata_patches import check_tables, ensure_live_data
    folders = list(game_folders)
    try:
        config = json.load(open(os.path.join(HOME, 'game.json'), encoding='utf-8-sig'))
        folders.extend(config.get(key) for key in ('client_data_source', 'copy') if config.get(key))
    except (OSError, ValueError):
        pass
    detected = steam_game_dir()
    if detected:
        folders.append(detected)
    result = ensure_client_data(HOME, game_folders=folders, documents=documents,
                                check=lambda files: check_tables(HOME, files))
    source = result['source'] + (' (copied into gameassets)' if result.get('imported') else '')
    result['using'] = f'Using MasterData and LocalizeText from {source}'
    try:
        ensure_live_data(HOME)
    except (OSError, ValueError) as error:
        error.using = result['using']      # the launcher shows which folder was used before the error
        raise
    return result


def cmd_prepare_client_data(game_folders=(), documents=None):
    from engine import net
    if net.port_taken():
        return out(ok=False, error='Stop the server before finding or importing MasterData and LocalizeText.')
    try:
        return out(ok=True, **prepare_client_data(game_folders, documents))
    except Exception as error:
        return out(ok=False, error=str(error), using=getattr(error, 'using', None))


def main():
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(1)
    def option(name, default):
        return a[a.index(name) + 1] if name in a else default
    if edition.call('command', a, out, default=False): return
    if a[0] == 'migrate-data':
        from engine import net, localsave
        if net.port_taken(): raise RuntimeError('Stop the server before migrating data')
        old = os.path.join(HOME, 'saves')
        if os.path.exists(old): localsave.detach(HOME, old)
        return out(ok=True, moved=storage.migrate(HOME))
    if a[0] == 'ingest' and len(a) == 3:
        from engine.catalogs import ingest
        return out(ok=True, **ingest(a[1], a[2], HOME))
    if a[0] == 'network':
        from engine import network, net
        return out(ok=True, addresses=network.lan_addresses(), hosts=net.HOSTS + [net.SDK_HOST],
                   port=443, certificates=edition.call('certificate_files', default=['certs/ca.cer', 'certs/sdk-ca.cer']))
    if a[0] == "serve":
        return cmd_serve(int(option('--parent', '0')), option('--bind', '0.0.0.0'), int(option('--port', '443')), '--no-system-setup' not in a)
    if a[0] == 'prepare-client-data':
        folders = [a[index + 1] for index in range(1, len(a) - 1) if a[index] == '--game']
        return cmd_prepare_client_data(folders, option('--documents', None))
    if a[0] == 'ingest-folder':
        from engine.catalogs import ingest
        from pathlib import Path
        imported = []
        for platform in ('steam', 'android', 'ios'):
            folder = Path(HOME) / 'gameassets' / 'fileindex' / 'ingest' / platform
            for file in sorted(folder.glob('*')):
                if file.is_file() and (file.name == 'octocacheevai' or file.suffix.lower() == '.pb'):
                    result = ingest(platform, file, HOME, skip_unchanged=True)
                    if not result.get('unchanged'):
                        imported.append(result)
        notes = []
        return out(ok=True, imported=imported + installed_index(notes), notes=notes)
    if a[0] == 'fetch-client-data':
        return out(ok=False, error='MasterData and LocalizeText CDN downloading has been removed. '
                   'Use prepare-client-data to find complete files from your existing installation.')
    if a[0] == "seasons": return cmd_seasons()
    if a[0] == "unhost": return cmd_unhost()
    if a[0] == "list": return cmd_list()
    if a[0] == "use" and len(a) == 2: return cmd_use(a[1])
    if a[0] == "new": return cmd_new()
    if a[0] == "remove" and len(a) == 2: return cmd_remove(a[1])
    if a[0] == "export" and len(a) == 3: return cmd_export(a[1], a[2])
    if a[0] == "import" and len(a) >= 2: return cmd_import(a[1], "--replace" in a[2:])
    print(__doc__); sys.exit(1)


if __name__ == "__main__":
    try: main()
    except Exception as e:
        import traceback   # where it failed goes in the launcher log too: a bare "[Errno 22] Invalid argument" says too little
        f, cmd = traceback.extract_tb(e.__traceback__)[-1], sys.argv[1] if len(sys.argv) > 1 else "engine"
        out(ok=False, error=f"{e} ({cmd}: {os.path.basename(f.filename)} line {f.lineno})"); sys.exit(1)
