"""HTTPS service for the game's hostnames, available to this PC and its LAN."""
import hashlib, json, logging, os, random, re, socket, ssl, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine import storage
from engine import responses
from engine.offline_sdk import SDK_HOST, initialization_response, tls_context

HOME = os.environ.get("EC_HOME") or os.getcwd()
log = logging.getLogger("svc")

HOSTS = ["game-q74z3cyn.app.gl.ffviiec.com", "resources-api-c9ps53g2.app.gl.ffviiec.com",
         "resources-data-w6d4k7cz.app.gl.ffviiec.com", "client-masterdata-c9ps53g2.app.gl.ffviiec.com",
         "chat-pd381ne.app.gl.ffviiec.com", "webview-w62j4u3y.app.gl.ffviiec.com"]
# Names the game calls that are not its servers: its built-in crash reporter (Bugsnag, on the publisher's account)
# asks for these on every launch. While the service runs they get a hosts line to 0.0.0.0, so the game cannot reach them.
BLOCKED = ["sessions.bugsnag.com", "notify.bugsnag.com"]
MAX_BODY = 8 << 20
NOT_HERE = "Co-op isn't available when playing locally."

# The game stops at "End of Service Notice" instead of logging in once its clock reaches a time fixed in its own code
# (1791352799000 = 2026-10-07 05:59:59 UTC). Its clock is the X-Server-Time of the last API reply plus the time
# since, and the reply it holds when it looks is the one to /api/check. That reply is given a time one day earlier;
# all the others get the real time.
GAME_STOPS_AT = 1791352799000
CHECK_CLOCK = GAME_STOPS_AT - 24 * 3600 * 1000

def reply_clock(path):
    real = int(time.time() * 1000)
    return min(real, CHECK_CLOCK) if path == "/api/check" else real


def p(*parts): return storage.path(*parts, root=HOME)


class Data:
    """Everything read from disk once at start."""
    def __init__(self):
        from engine import wire as P
        self.P = P
        live = p("gamedata", "live")
        with open(os.path.join(live, "x-master-path.txt"), encoding="utf-8") as source:
            self.catalog = source.read().strip()
        lp = os.path.join(live, "x-master-language-path.txt")
        with open(lp if os.path.isfile(lp) else p("gamedata", "language-path.txt"), encoding="utf-8") as source:
            self.language = source.read().strip()
        self.languages = {'en': self.language}
        self.paths = self._paths(live)
        try:    # an old season pass picked in the window: its tables and a catalog naming them (engine\seasons.py)
            from engine import seasons
            sid = seasons.chosen(HOME)
            if sid:
                self.catalog, extra = seasons.build(HOME, sid); self.paths.update(extra)
                log.info("season pass %d: open", sid)
        except Exception as e: log.error("season pass: not opened (%r); the game shows none", e)
        from engine.catalogs import load
        self.registry = load(HOME)
        if not self.registry.catalogs:
            log.info('No asset index imported; running without local Octo asset serving.')
        steam = next((c for c in self.registry.catalogs.values() if c.platform == 'steam'), None)
        self.assets = steam.data if steam else b''
        self.md5 = self.registry.objects
        self.rev, self.asset_url = (steam.revision, steam.url_format.encode()) if steam else (0, b'')
        try:
            with open(p("gamedata", "source.txt"), encoding="utf-8") as source:
                self.source = source.read().strip().rstrip("/") or None
        except OSError: self.source = None
        self.responses = responses.ServiceResponses(HOME, self.paths[self.catalog])
        log.info("data: generated API responses, %d table paths, catalog %s", len(self.paths), self.catalog)

    def _paths(self, live):
        stock = p("gamedata", "stock"); m = {}
        with open(p("gamedata", "catalog-path.txt"), encoding="utf-8") as source:
            m[source.read().strip()] = os.path.join(stock, "catalog.enc")
        with open(os.path.join(stock, "catalog.json"), encoding="utf-8") as source:
            tables = json.load(source)["tables"]
        for t in tables:
            m[t["path"]] = os.path.join(stock, "tables", t["table"] + ".enc")
        lc = p('gamedata', 'stock', 'language_catalog.json')
        if os.path.isfile(lc):
            with open(p("gamedata", "language-path.txt"), encoding="utf-8") as source:
                m[source.read().strip()] = p('gamedata', 'stock', 'language_catalog.enc')
            with open(lc, encoding="utf-8") as source:
                files = json.load(source)["files"]
            for t in files:
                m[t["path"]] = p('gamedata', 'stock', 'language', t["name"] + '.enc')
        japanese = p('gameassets', 'language', 'stock', 'ja')
        japanese_path = p('gameassets', 'language', 'language-path-ja.txt')
        if os.path.isfile(japanese_path) and os.path.isfile(os.path.join(japanese, 'language_catalog.json')):
            with open(japanese_path, encoding='utf-8') as source:
                reference = source.read().strip()
            self.languages['ja'] = reference
            m[reference] = os.path.join(japanese, 'language_catalog.enc')
            with open(os.path.join(japanese, 'language_catalog.json'), encoding='utf-8') as source:
                for entry in json.load(source)['files']:
                    m[entry['path']] = os.path.join(japanese, 'language', entry['name'] + '.enc')
        with open(os.path.join(live, "served_paths.json"), encoding="utf-8") as source:
            served_paths = json.load(source)
        for k, v in served_paths.items():
            m[k] = p('gamedata', 'live', *v.replace('\\', '/').split('/'))
        return m


def _pb_fields(b):
    """(field, value) pairs of one protobuf message: varints as int, length-delimited as bytes, fixed as bytes."""
    i, out = 0, []
    while i < len(b):
        k, i = _varint_at(b, i); f, w = k >> 3, k & 7
        if w == 0: v, i = _varint_at(b, i); out.append((f, v))
        elif w == 2: n, i = _varint_at(b, i); out.append((f, b[i:i + n])); i += n
        elif w == 1: out.append((f, b[i:i + 8])); i += 8
        elif w == 5: out.append((f, b[i:i + 4])); i += 4
        else: raise ValueError(f"wire type {w}")
    return out


def _varint_at(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1; r |= (c & 0x7F) << s; s += 7
        if not c & 0x80: return r, i


def asset_md5s(raw):
    """object name -> md5 for every entry of the asset list (fields 2 and 4 hold entries; 10 md5, 11 object)."""
    out = {}
    for f, v in _pb_fields(raw):
        if f in (2, 4):
            d = dict(_pb_fields(v))
            if 10 in d and 11 in d: out[d[11].decode()] = d[10].decode()
    return out


def asset_head(raw):
    """(revision, download address pattern) of the asset list: its fields 1 and 5."""
    rev, url = 0, b""
    for f, v in _pb_fields(raw):
        if f == 1 and isinstance(v, int): rev = v
        elif f == 5 and isinstance(v, bytes): url = v
    return rev, url


def _varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        if n: out += bytes([b | 0x80])
        else: return out + bytes([b])


class Saves:
    """Accounts in saves/<player id>/. One is 'active': the game plays that one, whatever id it holds."""
    def __init__(self, data):
        from engine import vault
        self.vault = vault; self.data = data
        self.root = p("saves"); os.makedirs(self.root, exist_ok=True)
        self.open = {}; self.lock = threading.RLock(); self.names = {}

    def active(self):
        try: return open(os.path.join(self.root, "active.txt")).read().strip() or None
        except FileNotFoundError: return None

    def set_active(self, uid):
        open(os.path.join(self.root, "active.txt"), "w").write(str(uid))

    def exists(self, uid): return os.path.isfile(os.path.join(self.root, str(uid), "info.json"))

    def listing(self):
        out = []
        for d in sorted(os.listdir(self.root)):
            f = os.path.join(self.root, d, "info.json")
            if os.path.isfile(f):
                i = json.load(open(f)); out.append((d, i.get("name") or "", i.get("origin") or ""))
        return out

    def get(self, uid):
        with self.lock:
            st = self.open.get(uid)
            if st is None:
                folder = os.path.join(self.root, uid)
                if not self.exists(uid):
                    self.vault.create_account(folder, uid, "new")
                    log.info("new account %s", uid)
                st = self.vault.State.load(folder); self.open[uid] = st
            return st

    def note_name(self, uid, st):
        """Keep info.json's name (the launcher's account list) in step with the name set in game."""
        rows = list(st.tables.get("user_profile_list", {}).values())
        name = rows[0].name if rows else ""
        if not name or self.names.get(uid) == name: return
        f = os.path.join(self.root, str(uid), "info.json")
        try:
            i = json.load(open(f))
            if i.get("name") != name:
                i["name"] = name; json.dump(i, open(f + ".tmp", "w"), indent=1); os.replace(f + ".tmp", f)
            self.names[uid] = name
        except Exception as e: log.warning("name for %s not saved: %r", uid, e)

    def forget(self, uid):
        with self.lock:
            st = self.open.pop(uid, None)
            if st is not None and getattr(st, "_conn", None) is not None:
                try: st._conn.close()
                except Exception: pass


# The game hung up before the answer went out (it left the screen, or stopped waiting on a call that holds, like the
# guild chat check). Nothing went wrong on this side, so it is noted and not treated as an error.
GONE = (ssl.SSLEOFError, ssl.SSLZeroReturnError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 300
    data: Data = None; saves: Saves = None; rules = None; codec = None; tokens = {}
    integrate_steam = True

    def log_message(self, *a): pass

    def _send(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _note(self, what, extra=""):
        log.info("%-8s %-20s %s %s [%.0f ms]", what, self.headers.get("Host", "?").split(".")[0],
                 self.path.split("?")[0], extra, (time.perf_counter() - self._t0) * 1000)

    # Calls this service could not really answer (refused co-op, no handler, a handler that failed, an
    # unknown host) are kept in logs/gaps, one file per call: how often, when, and the last few requests decoded, so
    # Save logs carries them and what the game asked for can be looked into later.
    GAP_SAMPLES = 5
    _gap_lock = threading.Lock()

    def _gap(self, kind, body=b"", detail=""):
        try:
            host = self.headers.get("Host", "?").split(".")[0]; path = self.path.split("?")[0]
            reqd = self._named(self._plain) if self._plain else None
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            s = {"time": now, "kind": kind}
            if self.path.partition("?")[2]: s["query"] = self.path.partition("?")[2]
            if detail: s["detail"] = detail
            if reqd: s["request"] = reqd
            elif body: s["body_hex"] = body[:4096].hex()
            d = p("logs", "gaps"); os.makedirs(d, exist_ok=True)
            f = os.path.join(d, re.sub(r"[^A-Za-z0-9]+", "_", f"{host}{path}").strip("_")[:120] + ".json")
            with self._gap_lock:
                try:
                    with open(f, encoding="utf-8") as fh: g = json.load(fh)
                except Exception: g = {"host": host, "method": self.command, "path": path, "count": 0, "first_seen": now, "samples": []}
                g["count"] += 1; g["last_seen"] = now; g["last_kind"] = kind
                g["samples"] = (g["samples"] + [s])[-self.GAP_SAMPLES:]
                with open(f + ".tmp", "w", encoding="utf-8") as fh: json.dump(g, fh, indent=1, default=str)
                os.replace(f + ".tmp", f)
        except Exception as e: log.warning("gap for %s not saved: %r", self.path.split("?")[0], e)

    def _named(self, plain):
        """A decrypted game request with its field names, without the common header; field numbers where the names fail."""
        try:
            from google.protobuf.json_format import MessageToDict
            m = self.data.P.Command_ApiNetwork_Api_ApiRequest(); m.ParseFromString(plain); m.ClearField("common")
            return MessageToDict(m, preserving_proto_field_name=True) or self._reqdict(plain)
        except Exception: return self._reqdict(plain)

    def do_GET(self): self._route()
    def do_POST(self): self._route()

    def _route(self):
        self._t0 = time.perf_counter(); self._plain = b""
        host = self.headers.get("Host", "").split(':', 1)[0].lower(); path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        log.info("<-       %-20s %s %s %dB %s", host.split(".")[0], self.command, path, n,
                 " ".join(f"{k}={v}" for k, v in self.headers.items() if k.lower() in ("transfer-encoding", "expect", "connection")))
        if n < 0 or n > MAX_BODY: self.close_connection = True; return self._send(413, {}, b"")
        body = self.rfile.read(n) if n else b""
        try:
            if host == SDK_HOST:
                status, headers, response = initialization_response(self.command, self.path)
                self._note('SDK', str(status)); return self._send(status, headers, response)
            if host not in HOSTS: return self._send(404, {}, b'')
            if host.startswith(("game-", "chat-")): return self._api(path, body)
            if host.startswith("resources-api"): return self._resources(path)
            if host.startswith("resources-data"): return self._object(path)
            if host.startswith("client-masterdata"): return self._tables(path)
            if host.startswith("webview-"): return self._web(path)
        except GONE:
            self._note("GONE", "the game closed this call before the answer went out"); self.close_connection = True; return
        except Exception as e:
            log.exception("error on %s", path); self._note("ERROR", repr(e)); self._gap("ERROR", body, repr(e))
            try:    # a game call gets an error the game handles (back to its home screen), anything else an empty 500
                if host.startswith(("game-", "chat-")):
                    try: return self._reply(path, responses.error("The local server could not process this request.").SerializeToString(), None, status=500)
                    except GONE: raise
                    except Exception: pass
                return self._send(500, {}, b"")
            except GONE: self.close_connection = True; return
        self._note("UNKNOWN"); self._gap("UNKNOWN", body); self._send(404, {}, b"")

    # ---- game calls ---------------------------------------------------------------------------------------------
    def _game_uid(self):
        m = re.search(r"user_id=(\d+)", self.path)
        return m.group(1) if m and m.group(1) != "0" else None

    def _api(self, path, body):
        C, P, R = self.codec, self.data.P, self.rules
        plain = self._plain = C.decrypt_api(body, False) if body else b""
        game_uid = self._game_uid(); uid = game_uid; swap = None
        if game_uid:    # the account picked in the window plays, whatever id the game remembers from an earlier run
            act = self.saves.active()
            if act and act != game_uid: uid = act; swap = (game_uid, act)
            elif not act: self.saves.set_active(game_uid)    # first start with no account here: this one is marked >
        if swap and plain: plain = self._swap(plain, *swap, request=True)
        if path == "/api/auth/game/register": return self._register(plain)
        if path in R.COOP_REFUSED: self._gap("NOT HERE"); return self._refuse(path)
        if path in responses.TRANSFER_PATHS:
            return self._reply(path, responses.error(
                "Square Enix account transfer is unavailable offline. Import an account export through ECLocal."
            ).SerializeToString(), uid, status=400)
        public = self.data.responses.public(path)
        if public is not None:
            self._note("GENERATED")
            return self._reply(path, public.SerializeToString(), uid)
        req = P.Command_ApiNetwork_Api_ApiRequest(); req.ParseFromString(plain)
        st = self.saves.get(uid) if uid else None
        fn = R.HANDLERS.get(path)
        out = None
        if st is not None and fn is not None:
            ctx = {"client_master": self.headers.get("x-master-path", ""), "server_master": self.data.catalog,
                   "app_version": self.headers.get("x-app-version", ""),
                   "master_version": self.data.responses.master_version}
            try:
                if getattr(fn, "unlocked", False): out = fn(st, req, dict(ctx, unlocked=True)).SerializeToString()
                else:
                    with st.lock:
                        out = fn(st, req, ctx).SerializeToString(); st.save()
                    self.saves.note_name(uid, st)
                self._note("OK", f"{len(out)}B")
            except Exception as e:
                log.exception("handler %s", path); self._note("FAILED", repr(e)); self._gap("FAILED", detail=repr(e))
                return self._reply(path, responses.error("The local server could not process this request.").SerializeToString(), uid, status=500)
        if out is None:
            self._note("UNSUPPORTED"); self._gap("UNSUPPORTED")
            return self._reply(path, responses.error("This request is not implemented by the local server.").SerializeToString(), uid, status=400)
        if swap and out: out = self._swap(out, swap[1], swap[0])
        return self._reply(path, out, uid)

    def _reqdict(self, plain):
        if not plain: return {}
        try:
            d = self.codec.pb_to_json(self.codec.pb_decode(plain)); d.pop("101", None); return d
        except Exception: return {}

    def _swap(self, plain, old, new, request=False):
        cls = (self.data.P.Command_ApiNetwork_Api_ApiRequest if request else
               self.data.P.Command_ApiNetwork_Api_ApiResponse)
        m = cls(); m.ParseFromString(plain)
        self.saves.vault.rewrite_ids(m, int(old), int(new)); return m.SerializeToString()

    def _register(self, plain):
        P = self.data.P
        uid = self.saves.active()
        if not uid:
            uid = str(random.randint(10**17, 9 * 10**17))
            while self.saves.exists(uid): uid = str(random.randint(10**17, 9 * 10**17))
            self.saves.get(uid); self.saves.set_active(uid)
            try:    # the first account here: the game's GameSave becomes its own, not what was last played under this Steam id
                from engine import localsave
                if self.integrate_steam: log.info(localsave.attach(HOME, self.saves.root, uid))
            except Exception as e: log.error("local save: not set up for %s (%r); the game's own GameSave is used as it is", uid, e)
        r = responses.register(uid, self.rules.display_id_for(uid))
        self._note("REGISTER", uid)
        return self._reply("/api/auth/game/register", r.SerializeToString(), uid)

    def _refuse(self, path):
        self._note("NOT HERE")
        return self._reply(path, responses.error(NOT_HERE).SerializeToString(), None,
                           status=int(os.environ.get("EC_REFUSE_STATUS", "400")))

    def _reply(self, path, plain, uid, status=200):
        enc, h = self.codec.encrypt_api(plain) if plain else (b"", "")
        locale = self.headers.get('x-language', 'en').strip().lower().replace('_', '-').split('-')[0]
        language = getattr(self.data, 'languages', {}).get(locale, self.data.language)
        out = {"Content-Type": "application/protobuf", "X-Content-Encoding-Secure": "1", "X-Content-Hash": h,
               "X-Server-Time": str(reply_clock(path)), "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
               "X-Master-Base-Url": self.data.responses.master_base_url, "X-Master-Path": self.data.catalog,
               "X-Master-Language-Path": language, "X-Server-Master-Version": self.data.responses.master_version,
               "X-Announcement-Update-Datetime": "0"}
        if path.endswith("/auth/session"):
            Handler.tokens[uid] = out["X-Token"] = str(uuid.uuid4()) + uuid.uuid4().hex[:8]
        self._send(status, out, enc)

    # ---- files ----------------------------------------------------------------------------------------------------
    def _resources(self, path):
        # The game asks which revision of the asset list is current, then for the list as of the revision it holds.
        # Both answers come from the list this service has: the revision is the list's own, a game holding that
        # revision is told it is up to date, and a game holding any other gets the whole list. (Recorded answers
        # name the revision of the day they were recorded, which a newer list no longer matches.)
        parts = path.strip("/").split("/"); pb = {"Content-Type": "application/x-protobuf"}
        if parts[:2] not in (['v1', 'revision'], ['v1', 'list']) or len(parts) < 3:
            return self._send(404, {}, b'')
        d = self.data.registry.get(parts[2])
        if d is None:
            # Completed installs still check Octo even when no local index is supplied.
            # Only acknowledge the known final revision; never invent a full index.
            final_revision = {'323173179': 368, '223173179': 493, '123173170': 492}.get(parts[2])
            if final_revision is not None:
                revision_check = parts[:2] == ['v1', 'revision'] and len(parts) == 3
                current_list = parts[:2] == ['v1', 'list'] and len(parts) == 4 and parts[3] == str(final_revision)
                if revision_check or current_list:
                    self._note('REVISION' if revision_check else 'LIST', f'installed client r{final_revision}; no local index')
                    return self._send(200, pb, b'\x08' + _varint(final_revision))
                self._note('MISSING', 'client needs an asset index; none supplied')
                return self._send(404, {}, b'')
            self._note('UNKNOWN', 'platform ' + parts[2]); return self._send(404, {}, b'')
        if parts[:2] == ["v1", "revision"] and len(parts) == 3:
            self._note("REVISION", f'{d.platform} r{d.revision}'); return self._send(200, pb, b"\x08" + _varint(d.revision))
        if parts[:2] == ["v1", "list"] and len(parts) == 4 and parts[3] == str(d.revision):
            self._note("LIST", "up to date")
            url = d.url_format.encode()
            return self._send(200, pb, b"\x08" + _varint(d.revision) + b"\x2a" + _varint(len(url)) + url)
        if parts[:2] == ["v1", "list"] and len(parts) == 4 and parts[3].isdecimal():
            self._note("LIST", f'{d.platform} full r{d.revision}'); return self._send(200, pb, d.data)
        self._note("MISSING"); self._send(404, {}, b"")

    _miss = {}      # object -> time the source last had nothing: quiet 404s for 10 min instead of a retry storm
    _verified = {}

    def _object(self, path):
        obj = path.strip("/")
        if obj.startswith('o/'): obj = obj[2:]
        if not re.fullmatch(r"[A-Za-z0-9]{1,16}", obj): return self._send(404, {}, b"")
        want = self.data.md5.get(obj)
        from engine.files import find_blob, blob_dir
        catalogs = self.data.registry.locations.get(obj, [])
        if not want: return self._send(404, {}, b'')
        f = os.path.join(blob_dir(catalogs[0]), want)
        for catalog in catalogs:
            g = find_blob(catalog, obj, want)
            if not g: continue
            with open(g, 'rb') as file:
                stat = os.fstat(file.fileno()); signature = (stat.st_mtime_ns, stat.st_size)
                if Handler._verified.get((g, want)) != signature:
                    if hashlib.file_digest(file, 'md5').hexdigest() != want: continue
                    Handler._verified[g, want] = signature
                file.seek(0)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(stat.st_size)); self.end_headers()
                import shutil
                shutil.copyfileobj(file, self.wfile, 256 * 1024)
                self._note('FILE', f'{stat.st_size}B'); return
        src = self.data.source
        if src and want and time.time() - Handler._miss.get(obj, 0) > 600:
            data = self._fetch(f"{src}/o/{obj}")
            if data is not None and hashlib.md5(data).hexdigest() == want:
                os.makedirs(os.path.dirname(f), exist_ok=True)
                tmp = f"{f}.{uuid.uuid4().hex}.part"
                with open(tmp, "wb") as h: h.write(data)
                os.replace(tmp, f)
                self._note("FETCHED", f"{len(data)}B, kept by MD5")
                return self._send(200, {"Content-Type": "application/octet-stream"}, data)
            Handler._miss[obj] = time.time()
            self._note("MISSING", f"{obj}: source gave {'nothing' if data is None else 'a file with the wrong md5'}")
            return self._send(404, {}, b"")
        self._note("MISSING", obj if want else f"{obj} (not in the asset list)"); self._send(404, {}, b"")

    @staticmethod
    def _fetch(url):
        """One file from the asset source, or None. Only another ECR on this network (a private IPv4 address, as the
        Android app's PC mirror sets): nothing is ever downloaded from the internet. Plain https with the system's
        roots (not our CA)."""
        import ipaddress, urllib.parse, urllib.request, urllib.error
        try: address = ipaddress.ip_address(urllib.parse.urlsplit(url).hostname or "")
        except ValueError: return None
        if not isinstance(address, ipaddress.IPv4Address) or not address.is_private or address.is_loopback: return None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "EC-Local/1.0"}), timeout=60) as r:
                d = r.read()
                return d if r.status == 200 and d else None
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.info("fetch %s: %r", url, e); return None

    def _tables(self, path):
        key = path.split("/prd", 1)[-1] if "/prd" in path else path
        f = self.data.paths.get(key)
        if f and os.path.isfile(f):
            with open(f, "rb") as source: data = source.read()
            self._note("TABLE", os.path.basename(f))
            return self._send(200, {"Content-Type": "application/octet-stream"}, data)
        self._note("MISSING"); self._send(404, {}, b"")

    _TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
              "json": "application/json", "html": "text/html", "css": "text/css", "js": "application/javascript"}

    def _web(self, path):
        rel = path.lstrip("/")
        if re.fullmatch(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.[A-Za-z0-9]{1,5}", rel):
            f = p("gamedata", "web", rel)
            if os.path.isfile(f):
                with open(f, "rb") as source: data = source.read()
                return self._send(200, {"Content-Type": self._TYPES.get(rel.rsplit(".", 1)[-1].lower(), "application/octet-stream")},
                                  data)
        self._note("MISSING"); self._send(404, {}, b"")


def port_taken(port=443, wait=1.0):
    """True when something on this PC already answers on 127.0.0.1:port (another copy of this program, or any other
    local server)."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=wait).close(); return True
    except OSError:
        return False


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    # On Windows the usual "reuse address" option lets a second program listen on a port another one already has, and
    # then either of them may get the game's calls. There the port is asked for exclusively instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt": self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, addr, ctx):
        self.tls = ctx; super().__init__(addr, Handler)

    def finish_request(self, request, client_address):
        try:
            request.settimeout(15); conn = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLEOFError, ConnectionResetError, ConnectionAbortedError):
            # connected and hung up without a word: another program looking whether a server answers here
            log.info("TLS      probe (connected, said nothing) from %s:%s", *client_address[:2])
            try: request.close()
            except OSError: pass
            return
        except (OSError, ValueError) as e:
            log.info("TLS      %s from %s:%s", repr(e)[:160], *client_address[:2])
            try: request.close()
            except OSError: pass
            return
        log.info("TLS      ok %s from %s:%s", conn.server_hostname or conn.version(), *client_address[:2])
        try: Handler(conn, client_address, self)
        finally:
            try: conn.close()
            except OSError: pass


def start(cert, key, port=443, bind_host='0.0.0.0', sdk_certificates=None, integrate_steam=True):
    """Load everything and listen on the selected address. Call shutdown() to stop."""
    # Complete local client data must be available before rules import/cache tables.
    # The desktop preflight also covers direct CLI starts; mobile uses its own home.
    from engine.client_ingest import ensure_client_data
    from engine.masterdata_patches import ensure_live_data
    ensure_client_data(HOME)
    ensure_live_data(HOME)
    from engine import codec, rules
    Handler.codec = codec; Handler.rules = rules
    Handler.integrate_steam = integrate_steam
    Handler.data = Data(); Handler.saves = Saves(Handler.data)
    ctx = tls_context(cert, key, sdk_certificates or p('certs'))
    srv = TLSServer((bind_host, port), ctx)
    threading.Thread(target=srv.serve_forever, name="https", daemon=True).start()
    log.info("listening on %s:%d, %d calls handled", bind_host, port, len(rules.HANDLERS))
    return srv
