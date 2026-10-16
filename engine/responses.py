"""Construct protocol responses from local configuration and current account state.

No packet recording, captured identity, or captured server clock is loaded here.
The terms date identifies the client's terms revision, not the server's clock.
"""
import hashlib
import json
import os
import random
import uuid

from google.protobuf.json_format import ParseDict
from engine import wire as P

TRANSFER_PATHS = frozenset({
    '/api/auth/migrate/bridge/start', '/api/auth/migrate/bridge/end',
    '/api/pvt/bridge/backup', '/api/pvt/bridge/backup/end',
})


class ServiceResponses:
    def __init__(self, root, catalog_file):
        config_path = os.path.join(root, 'engine', 'service.json')
        with open(config_path, encoding='utf-8') as stream:
            self.config = json.load(stream)
        if self.config.get('schema_version') != 1:
            raise ValueError('Unsupported service response configuration version')
        # A stable, numeric cache version changes with local catalog content,
        # independently of when a packet was recorded or this server starts.
        with open(catalog_file, 'rb') as stream:
            digest = hashlib.sha256(stream.read()).digest()
        self.master_version = str(int.from_bytes(digest[:4], 'big') & 0x7fffffff or 1)
        self.master_base_url = self.config['master_base_url']
        check = P.Command_ApiNetwork_Api_GetCheckResponse()
        ParseDict(self.config['check'], check)
        self.check = check

    def public(self, path):
        if path == '/api/check':
            result = P.Command_ApiNetwork_Api_ApiResponse()
            result.get_check.CopyFrom(self.check)
            return result
        if path == '/api/auth/session':
            result = P.Command_ApiNetwork_Api_ApiResponse()
            result.post_auth_session.SetInParent()
            return result
        return None


def register(user_id, display_user_id):
    result = P.Command_ApiNetwork_Api_ApiResponse()
    body = result.post_auth_game_register
    body.device_uuid = str(uuid.uuid4())
    body.user_id = int(user_id)
    body.display_user_id = display_user_id
    body.login_token = uuid.uuid4().hex
    body.battle_user_id = random.randint(10**18, 9 * 10**18)
    return result


def error(message):
    result = P.Command_ApiNetwork_Api_ApiResponse()
    result.error.code = 1
    # Retry sends the game back to its home screen (None and HandlingExclusion go to the title screen), and the client
    # shows its own text 120003 "Under maintenance." under a "Network Error" title; it does not display `message`.
    # Both only take effect with a non-200 HTTP status. Tested 2026-10-11.
    result.error.control_type = P.Command_ApiNetwork_Enum_ErrorControlType_Retry
    result.error.client_language_id = 120003
    result.error.message = message
    result.error.is_display_code = False
    return result
