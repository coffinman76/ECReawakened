"""Readable tutorial progression; account identity and dates are generated at use.

The JSON files describe the known tutorial delta, not a captured player or clock.
They live in engine/account-templates alongside the fresh-account baseline.
No binary recording or protobuf byte replacement is involved.
"""
import json
from functools import lru_cache

from google.protobuf.json_format import ParseDict
from engine import storage, wire as P


@lru_cache(maxsize=8)
def _template(filename):
    with open(filename, encoding='utf-8') as source:
        document = json.load(source)
    if document.get('schema_version') != 1:
        raise ValueError(f'Unsupported tutorial template version: {filename}')
    return document


def _resolve(value, runtime):
    if isinstance(value, dict):
        return {key: _resolve(item, runtime) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, runtime) for item in value]
    if isinstance(value, str) and value.startswith('$'):
        if value not in runtime:
            raise ValueError(f'Unknown tutorial runtime field: {value}')
        return runtime[value]
    return value


def response(kind, st, query, now_ms, new_id, context=None):
    """Build an isolated response from a static delta and current request values."""
    if kind not in ('register', 'skip'):
        raise ValueError(f'Unknown tutorial template: {kind}')
    filename = storage.path('res', f'tutorial-{kind}.json')
    document = _template(str(filename))
    previous_entry = st.one('user_battle_entry_list')
    context = context or {}
    runtime = {
        '$user_id': str(st.user_id),
        '$now_ms': str(now_ms),
        '$user_name': query.user_name,
        '$message': query.message,
        '$app_version': context.get('app_version') or (previous_entry.app_version if previous_entry else ''),
        '$master_version': context.get('master_version') or (previous_entry.master_version if previous_entry else ''),
    }
    # Only skip has a new instance reward. Registering a name must not consume IDs.
    if kind == 'skip':
        runtime['$new_materia_id'] = str(new_id())
    result = P.Command_ApiNetwork_Api_ApiResponse()
    ParseDict(_resolve(document['tables'], runtime), result.common.user.update)
    # Every other reply carries user.delete, even empty; the game seems to stall after a reply without it.
    result.common.user.delete.SetInParent()
    if st.other_info is not None:
        result.common.user.other_info.CopyFrom(st.other_info)
    ParseDict(document.get('other_info', {}), result.common.user.other_info)
    getattr(result, f'post_pvt_tutorial_{"register_user_name" if kind == "register" else "skip"}').SetInParent()
    return result
