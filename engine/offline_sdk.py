"""Offline initialization response for the LevelPlay 8.10.0 SDK in Android 4.0.10.

This only supplies an empty provider configuration. It does not serve ads or rewards.
The SDK's installed Java parser has accepted this configuration in an isolated probe.
"""
import base64
import json
import ssl
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

SDK_HOST = "i-sdk.mediation.unity3d.com"
SDK_PATH = "/sdk/v8.10.0"
# This is the SDK's protocol constant, not an account credential.
SDK_KEY = b"C38FB23A402222A0C17D34A92F971D1F"
CONFIG = {"providerOrder": {}, "providerSettings": {}, "configurations": {
    "adUnits": {}, "application": {name: {} for name in (
        "events", "loggers", "token", "segment", "auction", "crashReporter", "settings", "external")}}}


def initialization_response(method, target):
    """Return (status, headers, body), without recording device identifiers."""
    url = urlsplit(target)
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if url.path != SDK_PATH:
        return 404, headers, b"{}"
    if method != "POST":
        return 405, dict(headers, Allow="POST"), b"{}"
    query = parse_qs(url.query, keep_blank_values=True)
    # ServerURL.buildInitURL encrypts the entire parameter string into request=.
    # Inspect only routing fields; never record the decoded device identifiers.
    if "request" in query:
        if set(query) != {"request"} or len(query["request"]) != 1:
            return 400, headers, b"{}"
        try:
            ciphertext = base64.b64decode(query["request"][0], validate=True)
            decoded = unpad(AES.new(SDK_KEY, AES.MODE_CBC, iv=bytes(16)).decrypt(ciphertext), 16)
            query = parse_qs(decoded.decode("utf-8"), keep_blank_values=True)
        except (ValueError, UnicodeError):
            return 400, headers, b"{}"
    # Restrict this endpoint to the preserved game application.
    if query.get("applicationKey") != ["1a08eaaa5"]:
        return 400, headers, b"{}"
    mode = query.get("serr", ["1"])
    if mode not in (["0"], ["1"]):
        return 400, headers, b"{}"
    plain = json.dumps(CONFIG, separators=(",", ":")).encode()
    if mode == ["1"]:
        ciphertext = AES.new(SDK_KEY, AES.MODE_CBC, iv=bytes(16)).encrypt(pad(plain, 16))
        plain = json.dumps({"response": base64.b64encode(ciphertext).decode(), "compression": False},
                           separators=(",", ":")).encode()
    return 200, headers, plain


def tls_context(cert, key, certificates):
    """Preserve the game chain; use a separate SDK chain only for its exact SNI."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    folder = Path(certificates)
    sdk_cert, sdk_key = folder / "sdk-server.crt", folder / "sdk-server.key"
    if sdk_cert.exists() != sdk_key.exists():
        raise ValueError("SDK TLS configuration requires both sdk-server.crt and sdk-server.key")
    if sdk_cert.exists():
        sdk_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sdk_context.load_cert_chain(sdk_cert, sdk_key)

        def select(socket, hostname, initial_context):
            if hostname and hostname.lower() == SDK_HOST:
                socket.context = sdk_context

        context.set_servername_callback(select)
    return context
