"""Certificate and hosts-file setup for the local service.

Each PC makes its own two CAs, once: one for the game's ffviiec.com hosts and one for the mobile SDK host, each
limited to those names. Their private keys (ca.pem, sdk-ca.pem) stay in certs/ and are never shipped, so no two
installs share a key. The Android build keeps its fixed bundle in android/certs (its patched game trusts that CA)."""
import datetime, os, subprocess, sys, time

HOSTS_TAG = "# ec-local"
# SHA-1 thumbprints of the game and SDK CAs every Windows install shipped from 2026-10-08 to 2026-10-14. Their server
# keys were public, so trust_shared_removed() takes them out of Windows' trusted roots.
SHARED_CAS = ("25854DB775A952436ED757097288A0F9F7D9BADF", "B70C979071E7926EA18B9820411E182E7681E649")
RENEW = datetime.timedelta(days=30)   # a server certificate this close to its end is made again


def _x509():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    return x509, NameOID, hashes, serialization, rsa


def _current(crt, ca, hosts, now):
    """True when crt was signed by ca, names every host and does not end within RENEW."""
    x509 = _x509()[0]
    try:
        leaf = x509.load_pem_x509_certificate(open(crt, "rb").read())
        leaf.verify_directly_issued_by(ca)
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except Exception:   # missing, unreadable, another CA's or no names: made again
        return False
    return set(hosts) <= set(names) and leaf.not_valid_after_utc - now > RENEW


def _issue(folder, prefix, title, permitted, hosts):
    """<prefix>ca.pem (CA certificate and key) and its public <prefix>ca.cer, made once, limited to the `permitted`
    domains; and a server certificate for `hosts` signed by it, made again when the CA is new, a host is missing or
    it ends within RENEW. -> (<prefix>ca.cer, <prefix>server.crt, <prefix>server.key)"""
    x509, NameOID, hashes, serialization, rsa = _x509()
    os.makedirs(folder, exist_ok=True)
    ca_pem, ca_cer, crt, key = (os.path.join(folder, prefix + n) for n in ("ca.pem", "ca.cer", "server.crt", "server.key"))
    now = datetime.datetime.now(datetime.timezone.utc)
    if not os.path.isfile(ca_pem):   # first start, or certs/ still holds the fixed certificate older builds shipped
        k = rsa.generate_private_key(65537, 2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, title)])
        c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=36500))
             .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
             .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
             .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(d) for d in permitted], excluded_subtrees=None), critical=False)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
             .sign(k, hashes.SHA256()))
        pem = c.public_bytes(serialization.Encoding.PEM)
        for f in (crt, key):
            if os.path.isfile(f): os.remove(f)
        with open(ca_pem, "wb") as file:
            file.write(pem + k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                             serialization.NoEncryption()))
        with open(ca_cer, "wb") as file: file.write(pem)
    raw = open(ca_pem, "rb").read()
    ca = x509.load_pem_x509_certificate(raw)
    if not (os.path.isfile(key) and _current(crt, ca, hosts, now)):
        ck = serialization.load_pem_private_key(raw, password=None)
        k = rsa.generate_private_key(65537, 2048)
        c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
             .issuer_name(ca.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=800))
             .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
             .sign(ck, hashes.SHA256()))
        with open(crt, "wb") as file:
            file.write(c.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
        with open(key, "wb") as file:
            file.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
    return ca_cer, crt, key


def ensure(folder, hosts):
    """This PC's game CA (limited to ffviiec.com) and a server certificate for `hosts`. -> (ca.cer, server.crt, server.key)"""
    return _issue(folder, "", "Ever Crisis Reawakened (this PC)", ["ffviiec.com"], list(hosts) + ["*.app.gl.ffviiec.com"])


def ensure_sdk(folder):
    """This PC's SDK CA (limited to the SDK host) and its server certificate in `folder`. -> (sdk-ca.cer, folder)"""
    from engine.offline_sdk import SDK_HOST
    ca, _, _ = _issue(folder, "sdk-", "Ever Crisis Reawakened SDK (this PC)", [SDK_HOST], [SDK_HOST])
    return ca, folder


def trust_shared_removed():
    """Take the shared CAs (SHARED_CAS) out of Windows' trusted roots where present (machine store; needs admin).
    -> [(thumbprint, ok, output)] for each one that was there"""
    if os.name != "nt": return []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0); done = []
    for thumbprint in SHARED_CAS:
        if subprocess.run(["certutil", "-store", "Root", thumbprint], capture_output=True, creationflags=flags).returncode:
            continue   # not trusted on this PC
        r = subprocess.run(["certutil", "-delstore", "Root", thumbprint], capture_output=True, text=True, creationflags=flags)
        done.append((thumbprint, r.returncode == 0, (r.stdout + r.stderr).strip()))
    return done


def trust(ca_cer):
    """Add the CA to Windows' trusted roots (machine store; needs admin). -> (ok, output)"""
    if os.name != "nt": return True, "not Windows: skipped"
    r = subprocess.run(["certutil", "-addstore", "-f", "Root", ca_cer], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def hosts_file():
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts") if os.name == "nt" else "/etc/hosts"


def hosts(install, names, ip="127.0.0.1", blocked=()):
    """Point `names` at this PC and `blocked` at nowhere (install), or take our lines back out. Retries while Windows
    holds the file.
    -> how many lines of other programs name the same hosts. Windows uses the first line that names a host, so ours
    go at the top: put at the end they lose to such a line and the game never reaches this PC's service. Other
    programs' lines are left as they are, and count again once ours are out.
    Not on a Mac: there is no local game to redirect (a Mac serves phones only), and /etc/hosts needs root."""
    if sys.platform == "darwin": return 0
    f = hosts_file(); last = None; others = 0
    for _ in range(15):
        try:
            lines = [l for l in open(f, encoding="utf-8", errors="replace").read().splitlines() if HOSTS_TAG not in l]
            others = sum(1 for l in lines if not l.lstrip().startswith("#") and set(l.split("#")[0].split()[1:]) & set(names))
            if install: lines = [f"{ip} {h} {HOSTS_TAG}" for h in names] + [f"0.0.0.0 {h} {HOSTS_TAG}" for h in blocked] + lines
            with open(f, "r+", encoding="utf-8") as fh:
                fh.seek(0); fh.write("\n".join(lines) + "\n"); fh.truncate()
            break
        except PermissionError as e:
            last = e; time.sleep(0.4)
    else:
        raise last
    if os.name == "nt":
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return others
