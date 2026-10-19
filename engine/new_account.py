"""Generate starter accounts from readable defaults, without a binary recording."""
import json
import os
import secrets
import time

from google.protobuf.json_format import ParseDict

from engine import storage, wire as P


def _resolve(value, runtime):
    if isinstance(value, dict):
        return {key: _resolve(item, runtime) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, runtime) for item in value]
    if isinstance(value, str) and value.startswith('$'):
        if value not in runtime:
            raise ValueError(f'Unknown starter-account placeholder: {value}')
        return runtime[value]
    return value


def build(user_id, now_ms=None):
    user_id = int(user_id)
    if not 0 < user_id < 2**63:
        raise ValueError('Account ID must be a positive signed 64-bit integer.')
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    path = storage.path('res', 'new-account.json', root=os.environ.get('EC_HOME') or os.getcwd())
    with open(path, encoding='utf-8') as stream:
        defaults = json.load(stream)
    if defaults.get('schema_version') != 1:
        raise ValueError('Unsupported starter-account defaults version.')
    # IDs are account-specific and reused consistently in equipment references.
    first_id = secrets.randbelow(2**59 - 4) + 1
    runtime = {'$user_id': str(user_id), '$now_ms': str(now_ms),
               **{f'$starter_materia_{i}': str(first_id + i) for i in range(3)},
               '$starter_chocobo_0': str(first_id + 3)}
    result = P.Command_ApiNetwork_Api_ApiResponse()
    ParseDict(_resolve(defaults['tables'], runtime), result.common.user.update)
    ParseDict(_resolve(defaults['other_info'], runtime), result.common.user.other_info)
    result.common.user.delete.SetInParent()
    return result
