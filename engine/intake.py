import sys
import os
import json
import time
import collections
HERE = os.environ.get('EC_HOME') or os.getcwd()
from engine import wire as P
from google.protobuf.json_format import ParseDict, MessageToDict
_maps = {}

def _fmap(desc):
    if desc.full_name not in _maps:
        _maps[desc.full_name] = {''.join((p[:1].upper() + p[1:] for p in f.name.split('_'))): f for f in desc.fields}
    return _maps[desc.full_name]

def _conv(desc, d, where, unknown):
    out = {}
    m = _fmap(desc)
    for k, v in d.items():
        if v is None:
            continue
        f = m.get(k)
        if f is None:
            unknown[f'{where}.{k}'] += 1
            continue
        if f.message_type is not None and (not f.message_type.GetOptions().map_entry):
            out[f.name] = [_conv(f.message_type, x, f'{where}.{k}', unknown) for x in v] if isinstance(v, list) else _conv(f.message_type, v, f'{where}.{k}', unknown)
        else:
            out[f.name] = v
    return out

def convert(d, source='upload'):
    if not isinstance(d, dict) or not isinstance(d.get('AccountInfo'), dict):
        raise ValueError('not an account export (no AccountInfo)')
    unknown = collections.Counter()
    resp = P.Command_ApiNetwork_Api_ApiResponse()
    user = resp.common.user
    ParseDict(_conv(user.update.DESCRIPTOR, d['AccountInfo'], 'AccountInfo', unknown), user.update)
    ParseDict(_conv(user.other_info.DESCRIPTOR, d.get('OtherInfo') or {}, 'OtherInfo', unknown), user.other_info)
    history = []
    for h in d.get('GiftHistoryInfo') or []:
        x = P.Command_ApiNetwork_Api_HistoryGiftInfo()
        ParseDict(_conv(x.DESCRIPTOR, h, 'GiftHistoryInfo', unknown), x)
        history.append(MessageToDict(x, preserving_proto_field_name=True))
    gifts = []
    for g in d.get('GiftsInfo') or []:
        ri = P.Command_Entity_RewardInfo()
        ParseDict(_conv(ri.DESCRIPTOR, g, 'GiftsInfo', unknown), ri)
        gifts.append(MessageToDict(ri, preserving_proto_field_name=True))
    uids = {r.user_id for f, rows in user.update.ListFields() for r in rows if hasattr(r, 'user_id') and r.user_id}
    if len(uids) != 1:
        raise ValueError(f'expected one player id in the export, found {len(uids)}')
    uid = str(uids.pop())
    prof = user.update.user_profile_list[0].name if user.update.user_profile_list else ''
    raw = resp.SerializeToString()
    now = time.time()
    meta = {'player_id': uid, 'origin': 'archived', 'calls': 0, 'endpoints': 0, 'first_seen': now, 'last_seen': now, 'sources': [f'json:{source}'], 'account_bytes': len(raw), 'exported': now}
    return {'uid': uid, 'name': prof, 'raw': raw, 'gifts': gifts, 'meta': meta, 'unknown': dict(unknown), 'tables': sum((1 for _ in user.update.ListFields())), 'rows': sum((len(v) for _, v in user.update.ListFields())), 'stones': [(s.stone_id, s.count) for s in user.other_info.user_stone_list], 'history': len(history), 'history_list': history}

def write(folder, c):
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, 'base.pb'), 'wb').write(c['raw'])
    json.dump(c['meta'], open(os.path.join(folder, 'info.json'), 'w'), indent=1)
    json.dump(c['gifts'], open(os.path.join(folder, 'inbox.json'), 'w'), indent=0)
    json.dump(c.get('history_list') or [], open(os.path.join(folder, 'inbox_history.json'), 'w'), indent=0)

def fill_history(folder, c):
    p = os.path.join(folder, 'inbox_history.json')
    meta = json.load(open(os.path.join(folder, 'info.json')))
    if os.path.exists(p) or str(meta.get('player_id')) != c['uid']:
        return False
    json.dump(c.get('history_list') or [], open(p, 'w'), indent=0)
    return True
