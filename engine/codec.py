import base64
import gzip
import hashlib
import json
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    import lz4.frame
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False
CLIENT_API_KEY = base64.b64decode('Gs69+UiZDGBrjzj0uGq/m6mFs66bBUAP5ykHOROesZ4=')
SERVER_API_KEY = base64.b64decode('CMMnsenXvr7izAFborJvCZHwFrG40sykNgUSqgJ99+A=')
FILE_KEY = bytes([102, 213, 122, 113, 226, 89, 169, 26, 130, 132, 188, 167, 10, 45, 6, 6, 121, 122, 150, 83, 91, 70, 132, 153, 161, 61, 144, 105, 181, 62, 72, 144])

def _need():
    if not HAVE_CRYPTO:
        raise SystemExit('decryption needs: pip install pycryptodome lz4')

def aes_decrypt(payload, key):
    _need()
    return unpad(AES.new(key, AES.MODE_CBC, payload[:16]).decrypt(payload[16:]), 16)

def decompress_any(data):
    if data[:4] == b'\x04"M\x18':
        return lz4.frame.decompress(data)
    if data[:2] == b'\x1f\x8b':
        return gzip.decompress(data)
    return data

def decrypt_api(body, from_server=True):
    if not body:
        return b''
    return decompress_any(aes_decrypt(body, SERVER_API_KEY if from_server else CLIENT_API_KEY))

def decrypt_file(body):
    return decompress_any(aes_decrypt(body, FILE_KEY))

def aes_encrypt(plain, key):
    _need()
    import os
    from Crypto.Util.Padding import pad
    iv = os.urandom(16)
    return iv + AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, 16))

def encrypt_api(pb_bytes, from_server=True):
    _need()
    comp = lz4.frame.compress(pb_bytes, block_size=lz4.frame.BLOCKSIZE_MAX64KB, block_linked=False, content_checksum=False, block_checksum=False, store_size=False)
    h = base64.urlsafe_b64encode(hashlib.sha256(comp).digest()).decode()
    return (aes_encrypt(comp, SERVER_API_KEY if from_server else CLIENT_API_KEY), h)

def pb_field_bytes(field, payload):

    def varint(n):
        out = bytearray()
        while True:
            b = n & 127
            n >>= 7
            out.append(b | (128 if n else 0))
            if not n:
                return bytes(out)
    return varint(field << 3 | 2) + varint(len(payload)) + payload

def _varint(b, i):
    s = r = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 127) << s
        s += 7
        if not c & 128:
            return (r, i)

def pb_decode(b, depth=0, max_depth=6):
    i, out = (0, [])
    while i < len(b):
        k, i = _varint(b, i)
        f, w = (k >> 3, k & 7)
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            l, i = _varint(b, i)
            v = b[i:i + l]
            i += l
            v = _guess(v, depth, max_depth)
        elif w == 1:
            v = int.from_bytes(b[i:i + 8], 'little')
            i += 8
        elif w == 5:
            v = int.from_bytes(b[i:i + 4], 'little')
            i += 4
        else:
            raise ValueError(f'bad wire type {w} at {i}')
        out.append((f, v))
    return out

def _guess(v, depth, max_depth):
    if not v:
        return ''
    try:
        s = v.decode('utf-8')
        if all((c.isprintable() or c in '\n\t' for c in s)):
            return s
    except UnicodeDecodeError:
        pass
    if depth < max_depth:
        try:
            return pb_decode(v, depth + 1, max_depth)
        except Exception:
            pass
    return '0x' + v.hex()

def pb_to_json(fields):
    d = {}
    for f, v in fields:
        if isinstance(v, list) and v and isinstance(v[0], tuple):
            v = pb_to_json(v)
        d.setdefault(str(f), []).append(v)
    return {k: v[0] if len(v) == 1 else v for k, v in d.items()}

def headers(msg):
    return {k.decode() if isinstance(k, bytes) else k: v.decode(errors='replace') if isinstance(v, bytes) else v for k, v in msg['headers']}

def as_bytes(x):
    return bytes(x) if not isinstance(x, bytes) else x
