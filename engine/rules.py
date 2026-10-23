import time
import sys
import os
import json
HERE = os.environ.get('EC_HOME') or os.getcwd()
from engine import wire as P
from engine.vault import State
now_ms = lambda: int(time.time() * 1000)

def _announcements_heal(st):
    oi = getattr(st, 'other_info', None)
    if oi is None or not len(oi.mission_achieved_list):
        return 0
    n = len(oi.mission_achieved_list)
    del oi.mission_achieved_list[:]
    print(f'announcements heal: {st.user_id} {n} saved announcement(s) dropped')
    return n

def _common(tx):
    _announcements_heal(tx.st)
    c = tx.common_response()
    del c.user.other_info.mission_achieved_list[:]
    for mid, step in (getattr(tx, 'achieved', None) or {}).items():
        a = c.user.other_info.mission_achieved_list.add()
        a.mission_id = mid
        a.progress_count = step
    return c

def _resp(tx, body_field=None, body=None):
    r = P.Command_ApiNetwork_Api_ApiResponse()
    r.common.CopyFrom(_common(tx))
    if body_field:
        getattr(r, body_field).CopyFrom(body if body is not None else getattr(r, body_field))
    return r

CLEARED_MEMBER_KEEP = ('user_id', 'party_member_id', 'idx', 'updated_datetime')

# Tuning (Overspeed) slot: the battle shows the Tuning button only for a party member whose overaccel_skill_id is set.
# Square's accounts arrive with it set on every member; the client sends 0 for a character whose Tuning was unlocked
# locally, so the button never appeared (#68). An empty slot gets the character's owned Tuning (m_skill_overaccel
# col 1 CharacterId; lowest col 5 OrderNo if several); a slot already set is never changed.
def _oacc_default(st, extra=None):
    best = {}
    ids = [r.overaccel_skill_id for r in st.tables.get('user_skill_overaccel_list', {}).values()] + ([extra] if extra else [])
    for oid in ids:
        m = mdx('m_skill_overaccel', 0).get((oid,))
        if m and (m[1] not in best or m[5] < best[m[1]][1]):
            best[m[1]] = (m[0], m[5])
    return {c: v[0] for c, v in best.items()}

def _oacc_slot(st, row):
    if row.character_id and not row.overaccel_skill_id:
        row.overaccel_skill_id = _oacc_default(st).get(row.character_id, 0)

def _oacc_fill(st, tx, extra=None):
    """Set every party member's empty Tuning slot to the character's owned Tuning. -> rows changed."""
    d = _oacc_default(st, extra)
    n = 0
    for m in list(st.tables.get('user_party_member_list', {}).values()):
        if m.character_id and not m.overaccel_skill_id and d.get(m.character_id):
            m.overaccel_skill_id = d[m.character_id]
            tx.upsert('user_party_member_list', m)
            n += 1
    return n

def _oacc_heal(st):
    with st.transaction('title: tuning slots') as tx:
        n = _oacc_fill(st, tx)
    if n:
        print(f'tuning slots: {st.user_id} {n} party slot(s) given their Tuning')
    return n

def party_solo_set_upsert(st, req, ctx):
    q = req.post_pvt_party_solo_set_upsert
    t = now_ms()
    with st.transaction('party/solo/set/upsert') as tx:
        party = st.get('user_party_list', q.party_id) or st.new_row('user_party_list', party_id=q.party_id)
        party.combat_power = q.combat_power
        party.is_active = q.is_party_active
        if q.name:
            # The client sends the party's name with every save. Without this a rename only lived on screen and the
            # stored name (or "Solo N") came back after a battle or on reopening the party menu.
            party.name = q.name
        for f in party.DESCRIPTOR.fields:
            if f.name.startswith('memoria_id'):
                party.ClearField(f.name)
        if q.HasField('memoria_info'):
            for f, v in q.memoria_info.ListFields():
                if hasattr(party, f.name):
                    setattr(party, f.name, v)
        tx.upsert('user_party_list', party)
        for m in q.user_party_member_list:
            row = P.Command_Entity_UserPartyMember()
            row.CopyFrom(m)
            row.user_id = st.user_id
            row.updated_datetime = t
            if not m.is_active:
                keep = {f.name: v for f, v in row.ListFields() if f.name in CLEARED_MEMBER_KEEP}
                row = P.Command_Entity_UserPartyMember()
                for k, v in keep.items():
                    setattr(row, k, v)
            else:
                _oacc_slot(st, row)
            tx.upsert('user_party_member_list', row)
        mission_event(st, tx, 'SetDeckUpsert', 1, 1)
    return _resp(tx, 'post_pvt_party_solo_set_upsert')
DAY_MS = 86400000
RESET_OFFSET_MS = -14400000

def game_day(ts_ms):
    return (ts_ms - RESET_OFFSET_MS) // DAY_MS
UT = P.Command_Enums_UserTimeType

def _set_time(st, tx, tt, ts):
    row = st.get('user_time_list', tt) or st.new_row('user_time_list', user_time_type=tt)
    row.datetime = ts
    tx.upsert('user_time_list', row)

def _comeback_tiers(now):
    ids = {c[12] for c in md('m_campaign') if c[1] == 3 and c[12] and (c[9] <= now) and (c[10] == 0 or now <= c[10])}
    return [r for r in md('m_comeback') if r[0] in ids]

def comeback_check(st, tx, prev_login, now):
    if not prev_login:
        return None
    away = game_day(now) - game_day(prev_login)
    for r in _comeback_tiers(now):
        if away >= r[1] and (r[2] == 0 or away <= r[2]):
            row = st.get('user_comeback_list', r[0]) or st.new_row('user_comeback_list', comeback_id=r[0])
            row.comeback_datetime = row.previous_comeback_datetime = row.reset_comeback_datetime = now
            tx.upsert('user_comeback_list', row)
            return r[0]
    return None

def login_bookkeeping(st):
    new_day = _login_day(st)
    # "Log in during <hours>" daily missions (SetLoginAfterTime) are achieved by a login inside their window.
    # Nothing fired them, so they never completed.
    with st.transaction('missions: login time windows') as tx:
        mission_event(st, tx, 'SetLoginAfterTime')
    return new_day

def _login_day(st):
    t = now_ms()
    with st.transaction('user/title') as tx:
        prev = st.get('user_time_list', UT.Command_Enums_UserTimeType_Login)
        if st.get('user_time_list', UT.Command_Enums_UserTimeType_TutorialFinish) is not None:
            comeback_check(st, tx, prev.datetime if prev else 0, t)
        for tt in (UT.Command_Enums_UserTimeType_Login, UT.Command_Enums_UserTimeType_SteamInitialLoginDatetime):
            _set_time(st, tx, tt, t)
        dungeon_key_grant(st, tx, t)
        dq = st.get('user_time_list', UT.Command_Enums_UserTimeType_DailyQuestReset)
        if dq is None or game_day(t) > game_day(dq.datetime):
            week = dq is None or (game_day(t) + 3) // 7 != (game_day(dq.datetime) + 3) // 7
            _set_time(st, tx, UT.Command_Enums_UserTimeType_DailyQuestReset, t)
            mission_periodic_reset(st, tx, True, week)
            # The "Log in" daily/weekly missions (ProgressAutoDailyLogin) each name their own param (300002..300004),
            # so a call with param 0 matched none of them. One step per game day for each.
            for p in sorted({m[5] for m in _mt_index().get(MTYPE.get('ProgressAutoDailyLogin', -1), [])}):
                mission_event(st, tx, 'ProgressAutoDailyLogin', 1, p)
            dq_day_start(st, tx, t)          # a new day: the day's Mercenary Requests at the player's level, nothing won
            return True
    return False

def repair_lineups(st):
    have = mdx('m_shop_item', 0)
    bad = [r for r in st['user_shop_item_lineup_list'].values() if (r.shop_item_id,) not in have]
    if not bad:
        return 0
    with st.transaction('lineup/repair') as tx:
        for r in bad:
            opts = _shop_slots(r.shop_id).get(r.idx, [])
            old = r.shop_item_id
            if opts:
                r.shop_item_id = random.choice(opts)
                r.purchase_count = 0
                tx.upsert('user_shop_item_lineup_list', r)
            else:
                tx.delete('user_shop_item_lineup_list', r)
            print(f"lineup repair: {st.user_id} shop {r.shop_id} slot {r.idx}: item {old} not in master data -> {(r.shop_item_id if opts else 'removed')}")
    return len(bad)
LINK_DATA_MISSION = 2200001001
LINK_DATA_SET = 2200001      # "Link Data": link data (2200001001), link the mobile app and Steam (2200001002)

def _link_data_done(st):
    """Square's account-link missions need servers that are gone: they count as done and claimed, without their
    crystals, so the home screen stops offering them. They are set missions (m_mission col 3 = 3), kept as flags in
    user_mission_set_list; older builds marked only the first, in user_mission_list."""
    row = st.get('user_mission_list', LINK_DATA_MISSION)
    if row is None or row.received_progress_count < 1:
        row = row or st.new_row('user_mission_list', mission_id=LINK_DATA_MISSION)
        row.progress_count = max(row.progress_count, 1)
        row.received_progress_count = row.notified_progress_count = 1
        with st.transaction('link data: counts as linked') as tx:
            tx.upsert('user_mission_list', row)
    links = [m for m in md('m_mission') if m[1] == LINK_DATA_SET and m[3] == 3]
    s = st.get('user_mission_set_list', LINK_DATA_SET) or st.new_row('user_mission_set_list', mission_set_id=LINK_DATA_SET)
    if not links or all(_set_flag(s, 'received', m[2]) for m in links):
        return
    for m in links:
        for flag in ('achieved', 'notified', 'received'):
            _set_flag_on(s, flag, m[2])
    with st.transaction('link data: account links count as done') as tx:
        tx.upsert('user_mission_set_list', s)

def _tut_finish_heal(st):
    ini = st.get('user_tutorial_step_list', _TUT_INITIAL_T)
    if ini is None or ini.current_step < _TUT_INITIAL_DONE:
        return 0
    have = {t.user_time_type for t in st.tables['user_time_list'].values()}
    if _TUT_FINISH_TIMES[0] in have:
        return 0
    miss = [ty for ty in _TUT_FINISH_TIMES if ty not in have]
    if not miss:
        return 0
    ep = next(iter(st.tables['user_episode_list'].values()), None)
    when = (ep.story_party_mode_clear_datetime if ep is not None else 0) or now_ms()
    with st.transaction('title: finished intro without its finish times') as tx:
        for ty in miss:
            tx.upsert('user_time_list', st.new_row('user_time_list', user_time_type=ty, datetime=when))
    print(f'title heal: {st.user_id} had a finished intro without {len(miss)} finish time(s) - added')
    return len(miss)

def _stage_copies():
    """{copy stage id: original} for list-card copies (engine/cards.py "copies"). A win on either clears both."""
    if 'pairs' not in _STAGE_COPIES:
        from engine import cards
        try:
            _STAGE_COPIES['pairs'] = cards.copied_stages(_offline_recipe(), md('m_event_solo_area_group'),
                                                         md('m_event_solo_area'), md('m_event_solo_battle'))
        except Exception as e:
            print(f'stage copies unreadable: {e!r}')
            _STAGE_COPIES['pairs'] = {}
    return _STAGE_COPIES['pairs']
_STAGE_COPIES = {}

def _stage_partners(stage_id):
    """The other stages a win on this one clears: its original, or its copies."""
    pairs = _stage_copies()
    return [pairs[stage_id]] if stage_id in pairs else [c for c, o in pairs.items() if o == stage_id]
def _stage_copies_heal(st):
    """A list-card copy made after its original was won (Critical Threats' own cards came back with copies on the list)
    shows cleared, as a win on either would have made it; no reward is paid."""
    T = st.tables['user_event_solo_battle_list']
    miss = [(c, T[o,]) for c, o in _stage_copies().items()
            if (o,) in T and T[o,].total_win_count > 0 and not ((c,) in T and T[c,].total_win_count > 0)]
    if not miss:
        return 0
    with st.transaction('stage copies: cleared like their originals') as tx:
        for c, orow in miss:
            row = st.get('user_event_solo_battle_list', c) or st.new_row('user_event_solo_battle_list', event_solo_battle_id=c)
            row.total_win_count = 1
            row.high_rank_type = max(row.high_rank_type, orow.high_rank_type)
            tx.upsert('user_event_solo_battle_list', row)
    print(f'stage copies heal: {st.user_id} had {len(miss)} won stage(s) whose list-card copies showed unwon - marked won')
    return len(miss)
GIFT_SETS = (((1046,), 20025032003, 'Umbral Blade, 2025-03-07'), ((20037, 4045), 20025101001, 'Rousing Arc Sword + Rousing Wizer Staff, 2025-09-25 (2nd anniversary)'))

def _gift_weapons_heal(st):
    own = {w.weapon_id for w in st.tables['user_weapon_list'].values()}
    n = 0
    for weapons, set_id, what in GIFT_SETS:
        if all((w in own for w in weapons)):
            continue
        with st.transaction('gift weapons: ' + what) as tx:
            grant_reward_set(st, tx, set_id, 1)
        own |= set(weapons)
        n += 1
        print(f'gift weapons: {st.user_id} got {what}')
    return n
_AWARD_IX = None
_COOP_COPIES = {}

def _coop_copies():
    """{co-op fight (m_event_multi_battle id): the solo stage copying it}, for "coop_as_solo" (engine/cards.py
    coop_solo). The Crash Battle (Co-op) award (m_award_battle) lists co-op fights, which are only played as copies."""
    from engine import cards
    stages = md('m_event_solo_battle')
    if _COOP_COPIES.get('src') is not stages:
        fights = {(f[2], f[4]): f[0] for f in md('m_event_multi_battle')}
        found = {}
        for s in stages:
            if cards.COOP_STAGES < s[0] % 1000 < cards.COOP_STAGES + 100:
                f = fights.get((s[0] // 1000, s[5]))
                if f:
                    found.setdefault(f, s[0])
        _COOP_COPIES.clear()
        _COOP_COPIES.update(src=stages, map=found)
    return _COOP_COPIES['map']

def _award_sync(st, tx):
    global _AWARD_IX
    if _AWARD_IX is None:
        _AWARD_IX = {}
        for aw, bid, lvl in md('m_award_battle'):
            if lvl == 0:
                _AWARD_IX.setdefault(aw, []).append(bid)
    T = st.tables['user_event_solo_battle_list']
    copies = _coop_copies()
    n = 0
    for aw, battles in _AWARD_IX.items():
        cnt = sum((1 for b in battles if any((i,) in T and T[i,].total_win_count > 0 for i in (b, copies.get(b)))))
        row = st.get('user_award_list', aw)
        if cnt > (row.award_count if row else 0):
            row = row or st.new_row('user_award_list', award_id=aw)
            row.award_count = cnt
            tx.upsert('user_award_list', row)
            n += 1
    return n

def _weapon_upgrade_missions_heal(st):
    own = {w.weapon_id: w.upgrade_count for w in st.tables['user_weapon_list'].values() if w.upgrade_count}
    if not own:
        return 0
    t = now_ms()
    fix = []
    for m in _mt_index().get(MTYPE['ProgressWeaponUpgrade'], []):
        n = own.get(m[5])
        if not n or not _group_open(_set_group(m[1]), t):
            continue
        row = st.get('user_mission_list', m[0])
        if row is None or row.progress_count < n:
            fix.append((m[0], row, n))
    if not fix:
        return 0
    with st.transaction('missions: weapon upgrade catch-up') as tx:
        for mid, row, n in fix:
            row = row or st.new_row('user_mission_list', mission_id=mid)
            row.progress_count = n
            tx.upsert('user_mission_list', row)
    print(f'weapon upgrade missions heal: {st.user_id} {len(fix)} mission(s) caught up')
    return len(fix)

def _level_missions_heal(st):
    with st.transaction('missions: highwind parts / memoria analysis catch-up') as tx:
        if st.tables.get('user_highwind_parts_list'):
            _hw_parts_missions(st, tx)
        for m in st.tables.get('user_memoria_list', {}).values():
            if not m.analysis_point:
                continue
            mm = mdx('m_memoria', 0).get((m.memoria_id,))
            if mm:
                _memoria_analysis_missions(st, tx, m, mm)
        n = len(tx.up.user_mission_list)
    if n:
        print(f'level missions heal: {st.user_id} {n} mission(s) caught up')
    return n

def user_title(st, req, ctx):
    try:
        _sd_stale_run_heal(st)
    except Exception as e:
        print(f'stale score dungeon run heal failed for {st.user_id}: {e!r}')
    try:
        _sd_entry_heal(st)
    except Exception as e:
        print(f'score dungeon entry heal failed for {st.user_id}: {e!r}')
    try:
        _level_missions_heal(st)
    except Exception as e:
        print(f'level missions heal failed for {st.user_id}: {e!r}')
    try:
        _announcements_heal(st)
    except Exception as e:
        print(f'announcements heal failed for {st.user_id}: {e!r}')
    try:
        with st.transaction('awards: recount from clears') as tx:
            _award_sync(st, tx)
    except Exception as e:
        print(f'award sync failed for {st.user_id}: {e!r}')
    try:
        _materia_slot_heal(st)
    except Exception as e:
        print(f'slot heal failed for {st.user_id}: {e!r}')
    try:
        _materia_reveal_heal(st)
    except Exception as e:
        print(f'materia reveal heal failed for {st.user_id}: {e!r}')
    try:
        _materia_collection_heal(st)
    except Exception as e:
        print(f'materia collection heal failed for {st.user_id}: {e!r}')
    try:
        _materia_cap_heal(st)
    except Exception as e:
        print(f'materia cap heal failed for {st.user_id}: {e!r}')
    try:
        _gift_weapons_heal(st)
    except Exception as e:
        print(f'gift weapons heal failed for {st.user_id}: {e!r}')
    try:
        _stage_copies_heal(st)
    except Exception as e:
        print(f'stage copies heal failed for {st.user_id}: {e!r}')
    try:
        _weapon_upgrade_missions_heal(st)
    except Exception as e:
        print(f'weapon upgrade missions heal failed for {st.user_id}: {e!r}')
    try:
        _escalation_missions_heal(st)
    except Exception as e:
        print(f'escalation missions heal failed for {st.user_id}: {e!r}')
    try:
        _score_battle_missions_heal(st)
    except Exception as e:
        print(f'score battle missions heal failed for {st.user_id}: {e!r}')
    try:
        _stream_missions_heal(st)
    except Exception as e:
        print(f'stream missions heal failed for {st.user_id}: {e!r}')
    try:
        _oacc_heal(st)
    except Exception as e:
        print(f'tuning slots heal failed for {st.user_id}: {e!r}')
    try:
        with st.transaction('chocobo area missions') as tx:
            _chocobo_area_missions(st, tx)
    except Exception as e:
        print(f'chocobo area missions failed for {st.user_id}: {e!r}')
    try:
        _special_missions_received_heal(st)
    except Exception as e:
        print(f'special missions heal failed for {st.user_id}: {e!r}')
    try:
        _summon_pact_on_character_heal(st)
    except Exception as e:
        print(f'summon pact heal failed for {st.user_id}: {e!r}')
    try:
        _event_tower_heal(st)
    except Exception as e:
        print(f'event tower heal failed for {st.user_id}: {e!r}')
    try:
        _weapon_upgrade_type_heal(st)
    except Exception as e:
        print(f'weapon upgrade type heal failed for {st.user_id}: {e!r}')
    try:
        _weapon_evolve_type_heal(st)
    except Exception as e:
        print(f'weapon customization heal failed for {st.user_id}: {e!r}')
    try:
        _story_title_events_heal(st)
    except Exception as e:
        print(f'story title events heal failed for {st.user_id}: {e!r}')
    try:
        _story_entry_heal(st)
    except Exception as e:
        print(f'story entry heal failed for {st.user_id}: {e!r}')
    try:
        _event_story_missions_heal(st)
    except Exception as e:
        print(f'event story missions heal failed for {st.user_id}: {e!r}')
    try:
        _event_battle_episodes_heal(st)
    except Exception as e:
        print(f'event battle episodes heal failed for {st.user_id}: {e!r}')
    try:
        _panel_battle_missions_heal(st)
    except Exception as e:
        print(f'panel battle missions heal failed for {st.user_id}: {e!r}')
    try:
        _mission_reset_heal(st)
    except Exception as e:
        print(f'mission reset heal failed for {st.user_id}: {e!r}')
    try:
        _season_premium_heal(st)
    except Exception as e:
        print(f'season pass premium failed for {st.user_id}: {e!r}')
    try:
        _tut_finish_heal(st)
    except Exception as e:
        print(f'title heal failed for {st.user_id}: {e!r}')
    try:
        _guide_heal(st)
    except Exception as e:
        print(f'guide heal failed for {st.user_id}: {e!r}')
    try:
        _story_read_heal(st)
    except Exception as e:
        print(f'story read heal failed for {st.user_id}: {e!r}')
    try:
        _title_recount(st)
    except Exception as e:
        print(f'title recount failed for {st.user_id}: {e!r}')
    try:
        _gear_owned_heal(st)
    except Exception as e:
        print(f'gear exchange heal failed for {st.user_id}: {e!r}')
    try:
        _plate_owned_heal(st)
    except Exception as e:
        print(f'title plate heal failed for {st.user_id}: {e!r}')
    try:
        _craft_slot_heal(st)
    except Exception as e:
        print(f'craft slot heal failed for {st.user_id}: {e!r}')
    try:
        _gb_guild_heal(st)
    except Exception as e:
        print(f'guild battle guild failed for {st.user_id}: {e!r}')
    login_bookkeeping(st)
    _link_data_done(st)
    repair_lineups(st)
    r = P.Command_ApiNetwork_Api_ApiResponse()
    r.post_pvt_user_title.SetInParent()
    r.common.user.update.CopyFrom(st.full_tables())
    r.common.user.delete.Clear()
    try:
        _has_gift_refresh(st)
    except Exception as e:
        print(f'has_gift refresh failed for {st.user_id}: {e!r}')
    if st.other_info:
        r.common.user.other_info.CopyFrom(st.other_info)
    try:
        _title_guild_status(st, r)
    except Exception as e:
        print(f'title guild status failed for {st.user_id}: {e!r}')
    return r

def _title_guild_status(st, r):
    gb = st.one('user_guild_base_list')
    if gb is None:
        return
    b = r.post_pvt_user_title
    b.before_user_guild_status_type = gb.user_guild_status_type
    b.after_user_guild_status_type = gb.user_guild_status_type

def tutorial_register_user_name(st, req, ctx):
    q = req.post_pvt_tutorial_register_user_name
    from engine.tutorial_templates import response
    r = response('register', st, q, now_ms(), new_id, ctx)
    upd = r.common.user.update
    if not len(upd.user_profile_list):
        p = upd.user_profile_list.add()
        p.user_id = st.user_id
        p.is_show_character_rank = True
    for p in upd.user_profile_list:
        p.user_id = st.user_id
        p.name = q.user_name
        p.message = q.message
    with st.transaction('tutorial/register/user/name') as tx:
        for f, rows in upd.ListFields():
            for row in rows:
                cp = type(row)()
                cp.CopyFrom(row)
                tx.upsert(f.name, cp)
    ctx['new_name'] = q.user_name
    return r
HANDLERS = {'/api/pvt/party/solo/set/upsert': party_solo_set_upsert, '/api/pvt/user/title': user_title, '/api/pvt/tutorial/register/user/name': tutorial_register_user_name}

import msgpack
import random
import math
MD = {}

def _md_read(table):
    from engine import storage
    for d in (storage.path('gamedata', 'live', 'tables', root=HERE), storage.path('gamedata', 'stock', 'tables', root=HERE)):
        p = os.path.join(d, table + '.enc')
        if os.path.isfile(p):
            from engine import codec as C
            return msgpack.unpackb(C.decrypt_file(open(p, 'rb').read()), raw=False, strict_map_key=False)
    raise FileNotFoundError(table)

def md(table):
    if table not in MD:
        MD[table] = _md_read(table)
    return MD[table]
_MDX = {}

def mdx(table, *cols):
    rows = md(table)
    k = (table, cols)
    ent = _MDX.get(k)
    if ent is None or ent[0] is not rows:
        idx = {}
        for r in rows:
            idx.setdefault(tuple((r[c] for c in cols)), r)
        ent = _MDX[k] = (rows, idx)
    return ent[1]
from engine import storage
CFG_PATH = storage.path('res', 'settings.json', root=HERE)
CFG = {'materia': {'quality_up_chance': 0.15, 'extra_param_chance': 0.5, 'value_bias': 0.0}, 'drop_rolls': 2, 'exp_mult': 1, 'minor_guild_medals_per_stamina': 10, 'coop_medals_per_stamina': 1, 'crisis_medals_per_stamina': 1, 'guild_medals_per_stamina': 1, 'guild_rare_medals_per_stamina': 0.25, 'uncapped_items': [52, 13]}
if os.path.isfile(CFG_PATH):
    try:
        CFG.update(json.load(open(CFG_PATH)))
    except Exception as e:
        print('gameplay-settings.json ignored:', e)
POOLS = json.load(open(storage.path('res', 'materia.json', root=HERE)))
_id_seq = 0

def new_id():
    global _id_seq
    _id_seq += 1
    return now_ms() << 20 | (os.getpid() & 63) << 14 | _id_seq & 16383

def add_big_item(st, tx, big_item_id, significand, exponent):
    t = now_ms()
    row = st.get('user_big_item_list', big_item_id) or st.new_row('user_big_item_list', big_item_id=big_item_id, get_datetime=t)
    have = row.count_significand * 10 ** row.count_exponent if row.count_exponent >= 0 else row.count_significand / 10 ** (-row.count_exponent)
    add = significand * 10 ** exponent if exponent >= 0 else significand / 10 ** (-exponent)
    total = max(0.0, have + add)
    e = 0
    while total >= 1000000000000000.0:
        total /= 10
        e += 1
    row.count_significand = int(total)
    row.count_exponent = e
    row.last_get_datetime = t
    tx.upsert('user_big_item_list', row)
    return row

def add_item(st, tx, item_id, delta):
    t = now_ms()
    row = st.get('user_item_list', item_id) or st.new_row('user_item_list', item_id=item_id)
    if delta > 0:
        delta = _period_obtain(row, item_id, delta, t)
    if delta >= 0:
        over = max(0, row.count + delta - item_cap(item_id)) if delta else 0
        if over:
            _gift_overflow(st, item_id, min(over, delta))
            delta -= min(over, delta)
        row.count += delta
        row.total_obtain_count += delta
        row.last_get_datetime = t
        if not row.get_datetime:
            row.get_datetime = t
        if delta:
            mission_event(st, tx, 'ProgressItemGet', delta, item_id)
    else:
        row.count = max(0, row.count + delta)
        row.total_consumption_count += -delta
    return tx.upsert('user_item_list', row)

def _biased(lo, hi):
    b = CFG['materia']['value_bias']
    v = random.randint(lo, hi)
    return max(v, math.ceil(lo + (hi - lo) * b)) if b > 0 else v
GIL_ITEM = 1
CT_MATERIA = P.Command_Enums_CraftType.Command_Enums_CraftType_Materia

def guild_bonus_effects(st):
    out = {}
    gb = {r[0]: r for r in md('m_guild_bonus')}
    eff = {e[0]: e for e in md('m_guild_bonus_effect')}
    rel = md('m_guild_bonus_effect_group_guild_bonus_effect_rel')
    for ub in st['user_guild_bonus_list'].values():
        b = gb.get(ub.guild_bonus_id)
        if not b:
            continue
        for r in rel:
            if r[1] == b[3] and r[2] == ub.level and (r[4] in eff):
                e = eff[r[4]]
                out[e[1]] = max(out.get(e[1], 0), e[2])
    return out

def _craft_roll(recipe_id, option_item_map, st=None):
    rec = next((r for r in md('m_materia_recipe') if r[0] == recipe_id))
    gbe = guild_bonus_effects(st) if st is not None else {}
    pts = gbe.get(9, 0)
    for item_id, n in option_item_map.items():
        opt = next((o for o in md('m_item_craft_option') if o[0] == item_id), None)
        if opt:
            pts += opt[1] * min(n, opt[2])
    tiers = sorted((g for g in md('m_materia_craft_option_group') if g[0] == rec[9]), key=lambda g: g[1])
    tier = [g for g in tiers if g[1] <= pts][-1] if tiers else None
    og = next((o for o in md('m_materia_option_group') if o[0] == tier[2]), None) if tier else None
    lot = og[2] if og else None
    rar = gbe.get(12, 0)
    if og and rar:
        g = next((r[3] for r in md('m_materia_guild_bonus_quality_lot') if r[1] == og[3] and r[2] == rar), None)
        if g:
            lot = max(lot, g, key=lambda x: next((q[1] for q in md('m_materia_quality_lot_group') if q[0] == x), 0))
    min_q = next((q[1] for q in md('m_materia_quality_lot_group') if q[0] == lot), 1) if lot else 1
    min_n = next((c[1] for c in md('m_materia_parameter_count_group') if c[0] == og[1]), 1) if og else 1
    q = min_q
    while q < 5 and random.random() < CFG['materia']['quality_up_chance']:
        q += 1
    n = min_n
    while n < 4 and random.random() < CFG['materia']['extra_param_chance']:
        n += 1
    return (rec, q, n)

def _ec_pass_campaign_effects(st):
    now = now_ms()
    out = {}
    owned = set()
    for it in md('m_item_ec_pass'):
        row = st.get('user_item_list', it[0])
        if row and row.count > 0 and row.get_datetime and (now - row.get_datetime < it[1] * 60000):
            owned.add(it[0])
    if not owned:
        return out
    grps = set()
    for c in md('m_campaign'):
        if len(c) > 11 and c[11] in owned and (c[9] <= now) and (c[10] == 0 or now <= c[10]):
            grps.add(c[2])
    for e in md('m_campaign_effect'):
        if e[0] in grps:
            out[e[1]] = max(out.get(e[1], 0), e[2])
    return out

def _craft_start_one(st, tx, idx, recipe_id, option_item_map):
    rec, q, n = _craft_roll(recipe_id, option_item_map, st)
    t = now_ms()
    for g in md('m_materia_ingredient'):
        if g[0] == rec[4]:
            add_item(st, tx, g[1], -g[2])
    if rec[5]:
        add_item(st, tx, GIL_ITEM, -rec[5])
    for item_id, cnt in option_item_map.items():
        add_item(st, tx, item_id, -cnt)
        tx.upsert('user_craft_option_item_list', st.new_row('user_craft_option_item_list', idx=idx, item_id=item_id, count=cnt))
    dg = next((r[3] for r in md('m_materia_recipe_quality') if r[1] == recipe_id and r[2] == q), None)
    lot = mdx('m_materia_duration_lot', 0).get((dg,))
    minutes = random.randint(lot[1], lot[2]) if lot else 60
    _cm = _ec_pass_campaign_effects(st).get(16)
    calc_minutes = max(1, int(round(minutes * _cm / 100))) if _cm else minutes
    craft = st.get('user_craft_list', idx) or st.new_row('user_craft_list', idx=idx)
    craft.craft_type = CT_MATERIA
    craft.is_empty = False
    craft.total_start_count += 1
    craft.start_datetime = t
    craft.complete_datetime = t + minutes * 60000
    craft.calc_complete_datetime = t + calc_minutes * 60000
    craft.is_ec_pass_applied = bool(_cm)
    tx.upsert('user_craft_list', craft)
    mc = st.get('user_materia_craft_list', idx) or st.new_row('user_materia_craft_list', idx=idx)
    mc.materia_recipe_id = recipe_id
    mc.materia_id = rec[1]
    mc.quality_type = q
    mc.parameter_open_count = n
    mc.option_item_info = json.dumps({'option_items': [{'item_id': k, 'item_count': v} for k, v in option_item_map.items()]}) + '\n'
    tx.upsert('user_materia_craft_list', mc)
    ur = st.get('user_materia_recipe_list', recipe_id) or st.new_row('user_materia_recipe_list', materia_recipe_id=recipe_id)
    ur.craft_count += 1
    tx.upsert('user_materia_recipe_list', ur)

def craft_start(st, req, ctx):
    q = req.post_pvt_craft_start
    with st.transaction('craft/start') as tx:
        _craft_start_one(st, tx, q.index, q.recipe_id, dict(q.option_item_map))
    return _resp(tx, 'post_pvt_craft_start', P.Command_ApiNetwork_Api_PostPvtCraftStartResponse(index=q.index))

def materia_craft_start_bulk(st, req, ctx):
    q = req.post_pvt_materia_craft_start_bulk
    body = P.Command_ApiNetwork_Api_PostPvtMateriaCraftStartBulkResponse()
    with st.transaction('materia/craft/start/bulk') as tx:
        for info in q.craft_start_info_list:
            _craft_start_one(st, tx, info.user_craft_idx, info.recipe_id, dict(info.option_item_map))
            body.index_list.append(info.user_craft_idx)
    return _resp(tx, 'post_pvt_materia_craft_start_bulk', body)

def _shorten_minutes_per_item():
    return {r[0]: r[1] for r in md('m_item_craft_shorten')}

def craft_shorten(st, req, ctx):
    q = req.post_pvt_craft_shorten
    per = _shorten_minutes_per_item()
    body = P.Command_ApiNetwork_Api_PostPvtCraftShortenResponse()
    with st.transaction('craft/shorten') as tx:
        craft = st.get('user_craft_list', q.index)
        body.index = q.index
        body.before_complete_datetime = craft.calc_complete_datetime if craft else 0
        if craft:
            for item_id, n in q.shorten_item_map.items():
                add_item(st, tx, item_id, -n)
                craft.calc_complete_datetime -= per.get(item_id, 60) * 60000 * n
            craft.calc_complete_datetime = max(craft.calc_complete_datetime, now_ms())
            tx.upsert('user_craft_list', craft)
    return _resp(tx, 'post_pvt_craft_shorten', body)

def craft_shorten_bulk(st, req, ctx):
    per = _shorten_minutes_per_item()
    item_id, mins = next(iter(per.items()))
    body = P.Command_ApiNetwork_Api_PostPvtCraftShortenBulkResponse()
    t = now_ms()
    with st.transaction('craft/shorten/bulk') as tx:
        for craft in sorted(st['user_craft_list'].values(), key=lambda c: c.idx):
            if craft.is_empty or craft.calc_complete_datetime <= t:
                continue
            info = body.shorten_infos.add()
            info.index = craft.idx
            info.before_complete_datetime = craft.calc_complete_datetime
            need = math.ceil((craft.calc_complete_datetime - t) / (mins * 60000))
            add_item(st, tx, item_id, -need)
            craft.calc_complete_datetime = t
            tx.upsert('user_craft_list', craft)
    return _resp(tx, 'post_pvt_craft_shorten_bulk', body)
QV = {1: (13, 14), 2: (15, 16), 3: (17, 18), 4: (19, 20), 5: (21, 22)}

def _mint_materia(st, tx, mc):
    params = {r[0]: r for r in md('m_materia_parameter')}
    pool = [int(x) for x in POOLS.get(str(mc.materia_id), [])]
    if not pool:
        fam = str(mc.materia_id)[:4]
        pool = sorted({int(p) for k, v in POOLS.items() if k[:4] == fam for p in v}) or sorted(params)
    ids = random.sample(pool, min(mc.parameter_open_count, len(pool)))
    m = st.new_row('user_materia_list', user_materia_id=new_id(), materia_id=mc.materia_id, quality_type=mc.quality_type, get_datetime=now_ms(), parameter_open_count=mc.parameter_open_count)
    rec = next((r for r in md('m_materia_recipe') if r[0] == getattr(mc, 'materia_recipe_id', 0)), None)
    if rec:
        m.notes_set_id = rec[3]
    lo_i, hi_i = QV[mc.quality_type]
    for i, pid in enumerate(ids):
        setattr(m, f'parameter_id{i}', pid)
        setattr(m, f'parameter_value{i}', _biased(params[pid][lo_i], params[pid][hi_i]))
    _fill_open_slots(m)
    tx.upsert('user_materia_list', m)
    return m

def _pr_add(st, tx, name, n):
    t = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_' + name)
    pr = st.get('user_play_record_list', t) or st.new_row('user_play_record_list', play_record_type=t)
    pr.int_value += n
    tx.upsert('user_play_record_list', pr)

def _pr_max(st, tx, name, v):
    t = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_' + name)
    pr = st.get('user_play_record_list', t) or st.new_row('user_play_record_list', play_record_type=t)
    if v > pr.int_value:
        pr.int_value = v
        tx.upsert('user_play_record_list', pr)

def _obtained(st, tx, m):
    eid = _evolve_id(m)
    if not eid:
        return
    col = st.get('user_materia_collection_list', eid) or st.new_row('user_materia_collection_list', materia_evolve_id=eid)
    qn = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five'}.get(int(m.quality_type))
    if qn:
        setattr(col, f'quality_{qn}_obtain_count', getattr(col, f'quality_{qn}_obtain_count') + 1)
    col.max_level = max(col.max_level, 1)
    tx.upsert('user_materia_collection_list', col)

def craft_end(st, req, ctx):
    q = req.post_pvt_craft_end
    t = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtCraftEndResponse()
    body.craft_type = q.craft_type
    with st.transaction('craft/end') as tx:
        crafted = []
        for idx in q.user_craft_idx_list:
            craft = st.get('user_craft_list', idx)
            mc = st.get('user_materia_craft_list', idx)
            if not craft or craft.is_empty or (not mc) or (craft.calc_complete_datetime > t + 5000):
                continue
            m = _mint_materia(st, tx, mc)
            _obtained(st, tx, m)
            crafted.append(m)
            mission_event(st, tx, 'ProgressCraft')
            mission_event(st, tx, 'ProgressCraftMateria', 1, m.materia_id)
            mission_event(st, tx, 'ProgressCraftMateriaQuality', 1, m.quality_type, reached=m.quality_type)
            craft.is_empty = True
            tx.upsert('user_craft_list', craft)
            for row in [r for r in st['user_craft_option_item_list'].values() if r.idx == idx]:
                tx.delete('user_craft_option_item_list', row)
            res = body.craft_result_list.add()
            res.user_craft_idx = idx
            res.target_id = m.user_materia_id
        if crafted:
            _pr_add(st, tx, 'CraftCount', len(crafted))
            hq = sum((1 for m in crafted if int(m.quality_type) == 5))
            if hq:
                _pr_add(st, tx, 'CraftMateriaHighQualityCount', hq)
    return _resp(tx, 'post_pvt_craft_end', body)
HANDLERS.update({'/api/pvt/craft/start': craft_start, '/api/pvt/materia/craft/start/bulk': materia_craft_start_bulk, '/api/pvt/craft/shorten': craft_shorten, '/api/pvt/craft/shorten/bulk': craft_shorten_bulk, '/api/pvt/craft/end': craft_end})

RT_ITEM, RT_MATERIA, RT_ACCESSORY, RT_WEAPON, RT_STONE, RT_STAMINA, RT_EXP, RT_BIGITEM = (1, 3, 4, 5, 10, 12, 13, 31)
RT_MEMORIA_REWARD = 34
RT_HONOR, RT_GUILD_EXP, RT_GUILD_BONUS_EXP = (18, 25, 26)

def give_stone(st, tx, stone_id, delta):
    for row in st.other_info.user_stone_list:
        if row.stone_id == stone_id:
            row.count = max(0, row.count + delta)
            return
    r = st.other_info.user_stone_list.add()
    r.user_id = st.user_id
    r.stone_id = stone_id
    r.count = max(0, delta)

def stamina_max(status):
    r = mdx('m_user_rank', 0).get((_lvl_user(status.exp),))
    return r[1] if r else md('m_config')[0][32] or 999

def stamina_settle(status, now=None):
    now = now or now_ms()
    iv = (md('m_config')[0][33] or 120) * 1000
    mx = stamina_max(status)
    last = status.stamina_last_recovered_datetime
    if status.stamina_count >= mx or not last or last > now:
        status.stamina_last_recovered_datetime = now
        return
    n = (now - last) // iv
    if status.stamina_count + n >= mx:
        status.stamina_count = mx
        status.stamina_last_recovered_datetime = now
    else:
        status.stamina_count += n
        status.stamina_last_recovered_datetime = last + n * iv

def stamina_add(status, delta, now=None):
    stamina_settle(status, now)
    status.stamina_count = max(0, status.stamina_count + delta)

def grant_reward(st, tx, reward_id, count, infos=None):
    rw = mdx('m_reward', 0).get((reward_id,))
    if not rw:
        return
    rtype, target = (rw[1], rw[2])
    if rtype == RT_ITEM and target == 99001:
        give_stone(st, tx, CFG.get('paid_crystal_item_as_stone', 2), count)
    elif rtype == RT_ITEM:
        add_item(st, tx, target, count)
    elif rtype == RT_STONE:
        give_stone(st, tx, target, count)
    elif rtype == RT_STAMINA:
        s = st.one('user_status_list')
        if s:
            stamina_add(s, count)
            tx.upsert('user_status_list', s)
    elif rtype == RT_WEAPON:
        rwp = mdx('m_reward_weapon', 0).get((reward_id,))
        for _ in range(max(1, count)):
            ri = _grant_weapon(st, tx, target, rwp[1] if rwp and rwp[1] else 1, infos, from_gacha=False)
            if rwp and (rwp[2] or rwp[4]):
                _reward_weapon_state(st, tx, target, rwp, ri)
        return
    elif rtype == RT_MATERIA:
        spec = mdx('m_reward_materia', 0).get((reward_id,))
        mid = target if any((r[0] == target for r in md('m_materia'))) else target // 10
        for _ in range(max(1, count)):
            m = st.new_row('user_materia_list', user_materia_id=new_id(), materia_id=mid, quality_type=spec[1] if spec else 1, get_datetime=now_ms(), parameter_open_count=spec[3] if spec else 1)
            if spec:
                m.notes_set_id = spec[2]
                for i in range(4):
                    if spec[4 + i]:
                        setattr(m, f'parameter_id{i}', spec[4 + i])
                        setattr(m, f'parameter_value{i}', spec[8 + i])
            tx.upsert('user_materia_list', m)
            if infos is not None:
                ri = infos.add()
                ri.reward_type = rtype
                ri.target_id = target
                ri.count = 1
                mo = ri.reward_option.materia_option
                mo.user_materia_id = m.user_materia_id
                mo.quality_type = m.quality_type
                mo.parameter_open_count = m.parameter_open_count
                mo.notes_set_id = m.notes_set_id
                for i in range(4):
                    if getattr(m, f'parameter_id{i}'):
                        setattr(mo, f'parameter_id{i}', getattr(m, f'parameter_id{i}'))
                        setattr(mo, f'parameter_value{i}', getattr(m, f'parameter_value{i}'))
            eid = _evolve_id(m)
            if eid:
                col = st.get('user_materia_collection_list', eid) or st.new_row('user_materia_collection_list', materia_evolve_id=eid)
                qn = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five'}.get(m.quality_type)
                if qn:
                    setattr(col, f'quality_{qn}_obtain_count', getattr(col, f'quality_{qn}_obtain_count') + 1)
                col.max_level = max(col.max_level, 1)
                tx.upsert('user_materia_collection_list', col)
        return
    elif rtype == RT_ACCESSORY:
        spec = mdx('m_reward_accessory', 0).get((reward_id,))
        for _ in range(max(1, count)):
            row = st.new_row('user_accessory_list', user_accessory_id=new_id(), accessory_id=target, quality_type=spec[1] if spec else 1, passive_skill_point=spec[2] if spec else 0, get_datetime=now_ms())
            tx.upsert('user_accessory_list', row)
            if infos is not None:
                ri = infos.add()
                ri.reward_type = rtype
                ri.target_id = target
                ri.count = 1
        return
    elif rtype == RT_BIGITEM:
        exp = next((r[1] for r in md('m_reward_big_item') if r[0] == reward_id), 0)
        add_big_item(st, tx, target, count, exp)
        if infos is not None:
            ri = infos.add()
            ri.reward_type = rtype
            ri.target_id = target
            ri.count = count
            ri.reward_option.big_item_option.exponent = exp
        return
    elif rtype == RT_HONOR:
        if not st.get('user_honor_list', target):
            tx.upsert('user_honor_list', st.new_row('user_honor_list', honor_id=target, get_datetime=now_ms()))
        _plate_mark_owned(st, tx, (target,))
    elif rtype in (RT_GUILD_EXP, RT_GUILD_BONUS_EXP):
        _grant_guild_exp(st, tx, rtype, count)
    elif rtype == 2:
        grant_character(st, tx, target)
    elif rtype == RT_CHOCOBO:
        for _ in range(max(1, count)):
            grant_chocobo(st, tx, target)
    elif rtype == RT_HW_EFFECT:
        _grant_hw_effect(st, tx, target, infos)
        return
    elif _grant_reward_more(st, tx, rtype, target, count):
        pass
    else:
        print(f'grant_reward: RewardType {rtype} (target {target}) not implemented yet')
    if infos is not None:
        ri = infos.add()
        ri.reward_type = rtype
        ri.target_id = target
        ri.count = count
        if rtype == RT_MEMORIA_REWARD:
            # The game reads reward_option.memoria_option on every Memoria reward. Without it, the 3rd Anniversary
            # panel's Memoria chest throws in the client and the panel freezes. Analysis level 1 is assumed.
            um = st.get('user_memoria_list', target)
            mo = ri.reward_option.memoria_option
            for x in (mo.before_user_memoria, mo.after_user_memoria, mo.providing_memoria):
                x.analysis_point = um.analysis_point if um else 0
                x.analysis_level = 1
RT_CHOCOBO = P.Command_Enums_RewardType.Value('Command_Enums_RewardType_Chocobo')
import threading as _threading
_PRE = _threading.local()

def roll_chocobo_weights():
    s = random.randint(1000, 4000)
    i = random.randint(1000, 4000)
    return [s, 5000 - s, i, 5000 - i]

def grant_chocobo(st, tx, chocobo_id):
    m = mdx('m_chocobo', 0).get((chocobo_id,))
    pot = mdx('m_chocobo_rank_potential', 0).get((m[5],)) if m else None
    if not m or not pot:
        print(f'grant_chocobo: no m_chocobo / potential for {chocobo_id}')
        return None
    flags = 0
    for g, f in md('m_chocobo_area_type_group_rel'):
        if g == m[4]:
            flags |= f
    c = st.new_row('user_chocobo_list', user_chocobo_id=new_id(), chocobo_id=chocobo_id, chocobo_rank_group_id=m[7], chocobo_expedition_parameter_group_id=m[8], rank_type=pot[1], limit_rank_type=pot[2], area_type_flags=flags, chocobo_type=m[1], sex_type=m[2], get_datetime=now_ms())
    w = getattr(_PRE, 'w', None) or roll_chocobo_weights()
    _PRE.w = None
    c.speed_weight, c.stamina_weight, c.intelligence_weight, c.adaptability_weight = w
    tx.upsert('user_chocobo_list', c)
    return c

def grant_reward_set(st, tx, set_id, times=1, infos=None, depth=0):
    if depth > 4 or not set_id:
        return
    ix = _rs_index()
    rels = ix['rel'].get(set_id, [])
    rs = mdx('m_reward_set', 0).get((set_id,))
    if rs and rs[1] in (2, 3) and rels:
        rels_per_time = lambda: [random.choice(rels)]
    else:
        rels_per_time = lambda: rels
    for _ in range(times):
        for rel in rels_per_time():
            n = random.randint(rel[4], rel[5]) if rel[5] > rel[4] else rel[4]
            if n:
                grant_reward(st, tx, rel[2], n, infos)
    for sub in ix['nest'].get(set_id, ()):
        grant_reward_set(st, tx, sub, times, infos, depth + 1)
_RS_IDX = {}

def _rs_index():
    rel, nest = (md('m_reward_set_reward_rel'), md('m_reward_set_reward_set_rel'))
    if _RS_IDX.get('rel_src') is not rel or _RS_IDX.get('nest_src') is not nest:
        a, b = ({}, {})
        for r in rel:
            a.setdefault(r[1], []).append(r)
        for r in nest:
            b.setdefault(r[1], []).append(r[2])
        _RS_IDX.update(rel_src=rel, nest_src=nest, rel=a, nest=b)
    return _RS_IDX

def consume_set(st, tx, set_id, times=1):
    rw = {r[0]: r for r in md('m_reward')}
    for rel in md('m_consumption_set_consumption_rel'):
        if rel[1] != set_id:
            continue
        r = rw.get(rel[2])
        n = rel[3] * times
        if not r or not n:
            continue
        if r[1] == RT_STONE:
            stones = {s.stone_id: s for s in st.other_info.user_stone_list}
            free = stones.get(2)
            paid = stones.get(1)
            if r[2] == 1 and free:
                use = min(free.count, n)
                free.count -= use
                n -= use
            if n:
                give_stone(st, tx, r[2], -n)
        elif r[1] == RT_BIGITEM:
            exp = next((b[1] for b in md('m_reward_big_item') if b[0] == r[0]), 0)
            add_big_item(st, tx, r[2], -n, exp)
        else:
            add_item(st, tx, r[2], -n)
CHOCO_SHOPS = set()

def _pre_path(st):
    return os.path.join(st.folder, 'lineup_rolls.json') if st.folder else None

def _pre_load(st):
    p = _pre_path(st)
    try:
        return json.load(open(p)) if p and os.path.isfile(p) else {}
    except Exception:
        return {}

def _pre_save(st, d):
    p = _pre_path(st)
    if p:
        tmp = p + '.tmp'
        json.dump(d, open(tmp, 'w'), indent=1)
        os.replace(tmp, p)

def slot_weights(st, sid, idx, shop_item_id, d=None):
    us = st.get('user_shop_list', sid)
    gen = us.last_reset_datetime if us else 0
    own = d is None
    d = _pre_load(st) if own else d
    k = f'{sid}:{idx}'
    e = d.get(k)
    if not (e and e.get('item') == shop_item_id and (e.get('gen') == gen) and (len(e.get('w') or []) == 4)):
        e = d[k] = {'item': shop_item_id, 'gen': gen, 'w': roll_chocobo_weights()}
        if own:
            _pre_save(st, d)
    return list(e['w'])

def preroll_shop(st, sid):
    if sid not in CHOCO_SHOPS:
        return
    d = _pre_load(st)
    live = {f'{sid}:{r.idx}' for r in st['user_shop_item_lineup_list'].values() if r.shop_id == sid}
    for k in [k for k in d if k.startswith(f'{sid}:') and k not in live]:
        del d[k]
    for r in st['user_shop_item_lineup_list'].values():
        if r.shop_id == sid:
            slot_weights(st, sid, r.idx, r.shop_item_id, d)
    _pre_save(st, d)

def shop_exchange(st, req, ctx):
    q = req.post_pvt_shop_exchange
    item = mdx('m_shop_item', 0).get((q.shop_item_id,))
    body = P.Command_ApiNetwork_Api_PostPvtShopExchangeResponse()
    with st.transaction('shop/exchange') as tx:
        if item:
            times = max(1, q.purchase_count)
            cset = item[8]
            if item[9]:
                us = st.get('user_shop_item_list', q.shop_item_id)
                bought = us.purchase_count if us else 0
                steps = sorted((r for r in md('m_shop_item_repeat') if r[0] == item[9]), key=lambda r: r[1])
                for r in steps:
                    if bought + 1 >= r[1]:
                        cset = r[2]
            consume_set(st, tx, cset, times)
            pre = (ctx or {}).get('preroll')
            if pre is None and q.shop_id in CHOCO_SHOPS:
                lr0 = st.get('user_shop_item_lineup_list', q.shop_id, q.idx)
                if lr0 is not None and lr0.shop_item_id == q.shop_item_id:
                    pre = slot_weights(st, q.shop_id, q.idx, q.shop_item_id)
            try:
                for _ in range(times):
                    _PRE.w = pre
                    grant_reward_set(st, tx, item[6], 1, body.reward_infos)
            finally:
                _PRE.w = None
            if item[7]:
                grant_reward_set(st, tx, item[7], times, body.bonus_reward_infos)
            mission_event(st, tx, 'ProgressShopPurchase', times)
            lr = st.get('user_shop_item_lineup_list', q.shop_id, q.idx) if q.shop_id else None
            if lr is not None and lr.shop_item_id == q.shop_item_id:
                lr.purchase_count += times
                tx.upsert('user_shop_item_lineup_list', lr)
                body.is_limit = bool(item[3]) and lr.purchase_count >= item[3]
            else:
                us = st.get('user_shop_item_list', q.shop_item_id) or st.new_row('user_shop_item_list', shop_item_id=q.shop_item_id)
                # Stock that refills (m_shop_item col 17 = m_reset id): a purchase in a new period counts from 0.
                # A count carried over from an older period (an imported save) used to be added to, so one Stamina
                # Tonic sold out all 200 of a fresh month's stock (#25).
                rid = item[17] if len(item) > 17 else 0
                t = now_ms()
                p_now = reset_period(rid, t)
                if p_now is not None and us.last_purchase_datetime and reset_period(rid, us.last_purchase_datetime) < p_now:
                    us.purchase_count = 0
                us.purchase_count += times
                us.total_purchase_count += times
                us.last_purchase_datetime = t
                tx.upsert('user_shop_item_list', us)
                body.is_limit = bool(item[3]) and us.purchase_count >= item[3]
    return _resp(tx, 'post_pvt_shop_exchange', body)
HANDLERS['/api/pvt/shop/exchange'] = shop_exchange
SHOP_AUTO_RESET_MS = 24 * 3600 * 1000   # only for a shop with no m_reset row (shop_auto_reset)

def _shop_slots(sid):
    slots = {}
    have = mdx('m_shop_item', 0)
    for k, items in _lineup_pool().items():
        s, idx = map(int, k.split(':'))
        if s == sid:
            keep = [i for i in items if (i,) in have]
            if keep:
                slots[idx] = keep
    return slots

def shop_auto_reset(st, tx, sid, now):
    slots = _shop_slots(sid)
    if not slots:
        return False
    us = st.get('user_shop_list', sid) or st.new_row('user_shop_list', shop_id=sid)
    # A lineup lasts until its shop's own reset time (m_shop col 10 = ResetId -> m_reset). The Chocobo Exchange and
    # Gil shop reset on m_reset 3 (daily, 15:00 UTC); the client counts a lineup rolled before that time as expired
    # and refuses every purchase from it, so a fixed 24 h from the last roll left it stale for up to a day.
    rid = (mdx('m_shop', 0).get((sid,)) or [0] * 11)[10]
    last = us.last_reset_datetime
    p_last, p_now = (reset_period(rid, last), reset_period(rid, now)) if rid and last else (None, None)
    if p_last is not None and p_now is not None:
        fresh = p_last == p_now
    else:
        fresh = bool(last) and now - last < SHOP_AUTO_RESET_MS
    if fresh:
        return False
    for idx, items in sorted(slots.items()):
        lr = st.get('user_shop_item_lineup_list', sid, idx) or st.new_row('user_shop_item_lineup_list', shop_id=sid, idx=idx)
        lr.shop_item_id = random.choice(items)
        lr.purchase_count = 0
        tx.upsert('user_shop_item_lineup_list', lr)
    us.lineup_reset_count = 0
    us.last_reset_datetime = now
    tx.upsert('user_shop_list', us)
    preroll_shop(st, sid)
    return True

def shop_reset_check(st, req, ctx):
    sid = req.post_pvt_shop_reset_check.shop_id
    with st.transaction('shop/reset/check') as tx:
        shop_auto_reset(st, tx, sid, now_ms())
        for r in list(st['user_shop_list'].values()):
            if r.shop_id == sid:
                tx.touch('user_shop_list', r)
        for r in list(st['user_shop_item_lineup_list'].values()):
            if r.shop_id == sid:
                tx.touch('user_shop_item_lineup_list', r)
    return _resp(tx, 'post_pvt_shop_reset_check')
HANDLERS['/api/pvt/shop/reset/check'] = shop_reset_check

CHOCO_CFG = {'rare_chance': 0.15, 'gil_factor': 1.7, 'medal_factor': 2.4, 'max_hours': 48, 'shorten_minutes': {'15006': 180}, 'exploration_chance': 0.25}
CHOCO_CFG.update(CFG.get('chocobo', {}))
GIL_ITEM, MEDAL_ITEM = (1, 10)
PR_CHOCO = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_ChocoboExpeditionCount')

def _laps_path(st):
    return os.path.join(st.folder, 'laps_own.json') if st.folder else None

def _laps(st):
    p = _laps_path(st)
    if p and os.path.isfile(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {}
GLOBAL_LAPS = storage.path('res', 'laps.json', root=HERE)

def estimate_lap(choco):
    try:
        samples = json.load(open(GLOBAL_LAPS))
    except Exception:
        samples = []
    same = [s for s in samples if s[0] == choco.rank_type] or samples
    if not same:
        return 3000
    return min(same, key=lambda s: abs(s[1] - choco.speed_weight))[2]

def lap_for(st, choco):
    known = _laps(st).get(str(choco.user_chocobo_id))
    if known and known.get('lap'):
        return (int(known['lap']), known)
    try:
        t = choco_param(choco.chocobo_expedition_parameter_group_id, 1, choco_stat(choco, 'speed'))
        if t:
            return (int(t), {})
    except Exception as e:
        print(f'lap_for: table lookup failed for {choco.user_chocobo_id}: {e!r}')
    return (estimate_lap(choco), {})

def _deck_row(st, deck_id):
    return st.get('user_chocobo_expedition_deck_list', deck_id) or st.new_row('user_chocobo_expedition_deck_list', chocobo_expedition_deck_id=deck_id)

def _pick_reward(set_id):
    rels = [r for r in md('m_reward_set_reward_rel') if r[1] == set_id]
    if not rels:
        return None
    r = random.choice(rels)
    n = r[4] or 1
    return (r[2], n)

def _reward_info(infos, reward_id, count):
    rw = mdx('m_reward', 0).get((reward_id,))
    if not rw:
        return
    ri = infos.add()
    ri.reward_type = rw[1]
    ri.target_id = rw[2]
    ri.count = count

def choco_stat(ch, name):
    rk = mdx('m_chocobo_rank', 0, 1).get((ch.chocobo_rank_group_id, ch.rank_type))
    return getattr(ch, name) + getattr(ch, 'additional_' + name) + -(-(rk[2] if rk else 0) * getattr(ch, name + '_weight') // 10000)

def choco_param(group, ptype, value):
    best = None
    for r in md('m_chocobo_expedition_parameter'):
        if r[0] == group and r[1] == ptype and (r[2] <= value) and (best is None or r[2] > best[2]):
            best = r
    return best[3] if best else None

def guild_bonus(st, effect_type):
    total = 0
    for gb in st.tables.get('user_guild_bonus_list', {}).values():
        for rel in md('m_guild_bonus_effect_group_guild_bonus_effect_rel'):
            if rel[1] == gb.guild_bonus_id and rel[2] == gb.level:
                e = mdx('m_guild_bonus_effect', 0).get((rel[4],))
                if e and e[1] == effect_type:
                    total += e[2]
    return total

def choco_max_items(st, ch):
    base = choco_param(ch.chocobo_expedition_parameter_group_id, 2, choco_stat(ch, 'stamina'))
    return None if base is None else base + guild_bonus(st, 7)

CHOCO_EXPEDITION_ITEM_TYPE = 28

def _has_item(st, item_id):
    row = st.get('user_item_list', item_id)
    return bool(row and (row.count > 0 or row.total_obtain_count > 0))

def _exploration_rewards(exp):
    """Reward ids an area can turn up as Exploration items (Area Info's Exploration tiles: the flowers, feathers and
    so on). They sit under m_chocobo_expedition_reward col 3, a reward set whose lots are nested reward sets."""
    erw = mdx('m_chocobo_expedition_reward', 0).get((exp[4],))
    if not erw or not erw[3]:
        return []
    idx = _rs_index()
    out, pending, visited = [], [erw[3]], set()
    while pending:
        set_id = pending.pop()
        if set_id in visited:
            continue
        visited.add(set_id)
        out += [rel[2] for rel in idx['rel'].get(set_id, ())]
        pending += idx['nest'].get(set_id, ())
    return list(dict.fromkeys(out))

def _exploration_finds(st, tx, exp, grp, laps, res):
    """Each lap may turn up one Exploration item the player doesn't own yet (an account holds each one once).
    Square's rate isn't in the tables; 'exploration_chance' (0.25) matches how often recorded collects paid one."""
    chance = CHOCO_CFG.get('exploration_chance', 0.25)
    unowned = []
    for rid in _exploration_rewards(exp):
        rw = mdx('m_reward', 0).get((rid,))
        if rw and rw[1] == RT_ITEM and not _has_item(st, rw[2]):
            unowned.append(rw)
    found = []
    for _ in range(laps):
        if not unowned:
            break
        if random.random() < chance:
            found.append(unowned.pop(random.randrange(len(unowned))))
    slots = {r[2]: r[1] for r in md('m_chocobo_expedition_group_reward') if r[0] == exp[1]}
    for rw in found:
        add_item(st, tx, rw[2], 1)
        _reward_info(res.adventure_reward_infos, rw[0], 1)
        if rw[0] in slots:
            if slots[rw[0]] < 63:
                grp.collection_reward_idx_flags0 |= 1 << slots[rw[0]]
            else:
                grp.collection_reward_idx_flags1 |= 1 << slots[rw[0]] - 63
    return len(found)

def _chocobo_area_state(st):
    """(owned expedition items, owned per m_item_chocobo_expedition category, {area: (explored %, complete)})."""
    owned = {r[0] for r in md('m_item') if r[1] == CHOCO_EXPEDITION_ITEM_TYPE and _has_item(st, r[0])}
    per_category = collections.Counter(r[1] for r in md('m_item_chocobo_expedition') if r[0] in owned)
    slots = collections.defaultdict(list)
    for r in md('m_chocobo_expedition_group_reward'):
        slots[r[0]].append(r)
    areas = {}
    for row in st['user_chocobo_expedition_group_list'].values():
        rows = slots.get(row.chocobo_expedition_group_id)
        if not rows:
            continue
        bits = row.collection_reward_idx_flags0 | row.collection_reward_idx_flags1 << 63
        share = lambda rs: sum(bits >> r[1] & 1 for r in rs) / len(rs) if rs else None
        common, exploration = share([r for r in rows if r[3] in (1, 2)]), share([r for r in rows if r[3] == 3])
        # The client's Explored %: half for the normal + rare items, half for the Exploration items (matches both
        # readings we have: every normal + rare item and no Exploration item = 50%, everything = 100%).
        pct = 100 * share(rows) if common is None or exploration is None else 50 * common + 50 * exploration
        areas[row.chocobo_expedition_group_id] = (int(pct), share(rows) == 1)
    return owned, per_category, areas

def _chocobo_area_missions(st, tx):
    """Chocobo Missions and key-item missions that read the stable's state, set from what the save holds (never
    lowered): Exploration items obtained, areas fully explored, an area's Explored %, kinds of flower/feather/... found."""
    owned, per_category, areas = _chocobo_area_state(st)
    area_of = {r[0]: r[1] for r in md('m_chocobo_expedition')}
    reading = {
        'ProgressItemTypeGet': lambda m: len(owned) if m[5] == CHOCO_EXPEDITION_ITEM_TYPE else 0,
        'ProgressChocoboExpeditionAdventureRateCount': lambda m: sum(1 for _, done in areas.values() if done),
        'ProgressAbsoluteChocoboExpeditionAdventureRate': lambda m: areas.get(m[5], areas.get(area_of.get(m[5]), (0, False)))[0],
        'ProgressChocoboExpeditionItemCollect': lambda m: per_category.get(m[5], 0),
    }
    t = now_ms()
    raised = 0
    for kind, value_of in reading.items():
        for m in _mt_index().get(MTYPE.get(kind, -1), []):
            value = value_of(m)
            if not value or not _group_open(_set_group(m[1]), t):
                continue
            row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
            if row.progress_count < value:
                row.progress_count = value
                tx.upsert('user_mission_list', row)
                raised += 1
    return raised

def _collect_deck(st, tx, deck, now, res):
    exp = mdx('m_chocobo_expedition', 0).get((deck.chocobo_expedition_id,))
    if not exp or not deck.lap_time_sec:
        return 0
    _eff = _ec_pass_campaign_effects(st)
    lap_ms = deck.lap_time_sec * 1000
    if _eff.get(14):
        lap_ms = max(1000, int(lap_ms * _eff[14] / 100))
    choco = st.get('user_chocobo_list', deck.user_chocobo_id)
    cap = choco_max_items(st, choco) if choco else None
    elapsed = now - deck.calc_start_datetime
    if cap is None:
        elapsed = min(elapsed, CHOCO_CFG['max_hours'] * 3600000)
    laps = max(0, int(elapsed // lap_ms))
    full = cap is not None and laps >= cap
    if full:
        laps = cap
    known = _laps(st).get(str(deck.user_chocobo_id), {}) if choco else {}
    # Campaign coefficients are per mille (1300 = x1.3): 15 gil, 12 medals, 9 character exp.
    gil_per = int(round(exp[6] * known.get('gil_factor', CHOCO_CFG['gil_factor']) * _eff.get(15, 1000) / 1000))
    medal_per = int(round(exp[5] * known.get('medal_factor', CHOCO_CFG['medal_factor']) * _eff.get(12, 1000) / 1000))
    erw = mdx('m_chocobo_expedition_reward', 0).get((exp[4],))
    got = {}
    for _ in range(laps):
        ri = res.expedition_reward_infos.add()
        ri.reward_type = RT_ITEM
        ri.target_id = GIL_ITEM
        ri.count = gil_per
        ri = res.expedition_reward_infos.add()
        ri.reward_type = RT_ITEM
        ri.target_id = MEDAL_ITEM
        ri.count = medal_per
        if erw:
            _rc = CHOCO_CFG['rare_chance']
            if _eff.get(11):
                _rc = min(1.0, _rc * _eff[11] / 1000)
            rare = random.random() < _rc
            pick = _pick_reward(erw[2] if rare else erw[1])
            if pick is None and rare:
                rare = False
                pick = _pick_reward(erw[1])
            if pick:
                rid, n = pick
                _reward_info(res.rare_reward_infos if rare else res.reward_infos, rid, n)
                got[rid] = got.get(rid, 0) + n
    if laps:
        add_item(st, tx, GIL_ITEM, gil_per * laps)
        add_item(st, tx, MEDAL_ITEM, medal_per * laps)
        for rid, n in got.items():
            rw = mdx('m_reward', 0).get((rid,))
            if rw and rw[1] == RT_ITEM:
                add_item(st, tx, rw[2], n)
            elif rw and rw[1] == RT_STONE:
                give_stone(st, tx, rw[2], n)
        grp = st.get('user_chocobo_expedition_group_list', exp[1]) or st.new_row('user_chocobo_expedition_group_list', chocobo_expedition_group_id=exp[1])
        for gr in md('m_chocobo_expedition_group_reward'):
            if gr[0] == exp[1] and gr[2] in got:
                if gr[1] < 63:
                    grp.collection_reward_idx_flags0 |= 1 << gr[1]
                else:
                    grp.collection_reward_idx_flags1 |= 1 << gr[1] - 63
        _exploration_finds(st, tx, exp, grp, laps, res)
        tx.upsert('user_chocobo_expedition_group_list', grp)
        ch = st.get('user_character_list', deck.character_id)
        if ch:
            res.before_user_character.CopyFrom(ch)
            top = max((r[2] for r in md('m_character_level') if r[0] == deck.character_id), default=None)
            gain = exp[7] * laps * _eff.get(9, 1000) // 1000
            ch.exp = min(ch.exp + gain, top) if top else ch.exp + gain
            tx.upsert('user_character_list', ch)
            res.after_user_character.CopyFrom(ch)
        pr = st.get('user_play_record_list', PR_CHOCO) or st.new_row('user_play_record_list', play_record_type=PR_CHOCO)
        pr.int_value += laps
        tx.upsert('user_play_record_list', pr)
        mission_event(st, tx, 'ProgressChocoboExpeditionReceive', len(res.reward_infos))
        _chocobo_area_missions(st, tx)
    deck.start_datetime = now if full else deck.calc_start_datetime + laps * lap_ms
    deck.calc_start_datetime = deck.start_datetime
    tx.upsert('user_chocobo_expedition_deck_list', deck)
    return laps

def chocobo_expedition_result(st, req, ctx):
    q = req.post_pvt_chocobo_expedition_result
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtChocoboExpeditionResultResponse()
    with st.transaction('chocobo/expedition/result') as tx:
        for deck_id in q.expedition_deck_ids:
            deck = st.get('user_chocobo_expedition_deck_list', deck_id)
            if not deck or not deck.user_chocobo_id:
                continue
            _collect_deck(st, tx, deck, now, body.chocobo_expedition_result_map[deck_id])
    return _resp(tx, 'post_pvt_chocobo_expedition_result', body)

def chocobo_expedition_edit(st, req, ctx):
    q = req.post_pvt_chocobo_expedition_edit
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtChocoboExpeditionEditResponse()
    with st.transaction('chocobo/expedition/edit') as tx:
        deck = _deck_row(st, q.expedition_deck_id)
        if deck.user_chocobo_id and deck.lap_time_sec:
            _collect_deck(st, tx, deck, now, body.chocobo_expedition_result)
        if q.user_chocobo_id:
            choco = st.get('user_chocobo_list', q.user_chocobo_id)
            lap, _ = lap_for(st, choco) if choco else (3000, {})
            deck.user_chocobo_id = q.user_chocobo_id
            deck.chocobo_expedition_id = q.expedition_id
            deck.character_id = q.character_id
            deck.start_datetime = now
            deck.calc_start_datetime = now
            deck.lap_time_sec = lap
        else:
            keep = deck.chocobo_expedition_deck_id
            deck = st.new_row('user_chocobo_expedition_deck_list', chocobo_expedition_deck_id=keep)
        tx.upsert('user_chocobo_expedition_deck_list', deck)
    return _resp(tx, 'post_pvt_chocobo_expedition_edit', body)

def chocobo_expedition_shorten(st, req, ctx):
    q = req.post_pvt_chocobo_expedition_shorten
    now = now_ms()
    with st.transaction('chocobo/expedition/shorten') as tx:
        deck = st.get('user_chocobo_expedition_deck_list', q.expedition_deck_id)
        total_min = 0
        n_items = 0
        for item_id, n in q.shorten_item_map.items():
            mins = CHOCO_CFG['shorten_minutes'].get(str(item_id), 180)
            add_item(st, tx, item_id, -n)
            total_min += mins * n
            n_items += n
        if deck and deck.user_chocobo_id:
            deck.calc_start_datetime -= total_min * 60000
            tx.upsert('user_chocobo_expedition_deck_list', deck)
        sh = st.one('user_chocobo_expedition_shorten_list') or st.new_row('user_chocobo_expedition_shorten_list')
        if not sh.last_shorten_datetime or game_day(now) > game_day(sh.last_shorten_datetime):
            sh.shorten_count = 0
        sh.shorten_count += n_items
        sh.last_shorten_datetime = now
        tx.upsert('user_chocobo_expedition_shorten_list', sh)
    return _resp(tx, 'post_pvt_chocobo_expedition_shorten')

def chocobo_expedition_extend(st, req, ctx):
    q = req.post_pvt_chocobo_expedition_extend
    with st.transaction('chocobo/expedition/extend') as tx:
        d = mdx('m_chocobo_expedition_deck', 0).get((q.expedition_deck_id,))
        if d and d[3]:
            give_stone(st, tx, 2, -d[3])
        tx.upsert('user_chocobo_expedition_deck_list', _deck_row(st, q.expedition_deck_id))
    return _resp(tx, 'post_pvt_chocobo_expedition_extend')

def _treasure_taken(reset_id, last, now):
    # A farm treasure refills on its term's m_reset period (m_chocobo_farm_term / m_chocobo_event_farm_term ResetId, daily
    # in every row); the client shows it as taken while last_open_treasure_datetime is in the current period, so opening it
    # again in that period pays nothing. A ResetId this server cannot decode falls back to the game day.
    if not last:
        return False
    a, b = reset_period(reset_id, last), reset_period(reset_id, now)
    if a is None or b is None:
        return game_day(last) == game_day(now)
    return a == b

def chocobo_farm_dungeon_treasure(st, req, ctx):
    q = req.post_pvt_chocobo_farm_dungeon_treasure
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtChocoboFarmDungeonTreasureResponse()
    with st.transaction('chocobo/farm/dungeon/treasure') as tx:
        t = mdx('m_chocobo_farm_treasure', 0).get((q.dungeon_treasure_id,))
        farm = st.one('user_chocobo_farm_list')
        term = mdx('m_chocobo_farm_term', 0).get((t[1],)) if t else None
        if farm and _treasure_taken(term[2] if term else 0, farm.last_open_treasure_datetime, now):
            return _resp(tx, 'post_pvt_chocobo_farm_dungeon_treasure', body)
        if t:
            grant_reward_set(st, tx, t[3], 1, body.reward_infos)
        farm = farm or st.new_row('user_chocobo_farm_list')
        farm.last_open_treasure_datetime = now
        tx.upsert('user_chocobo_farm_list', farm)
    return _resp(tx, 'post_pvt_chocobo_farm_dungeon_treasure', body)

def _sale_medals(choco):
    m = mdx('m_chocobo', 0).get((choco.chocobo_id,))
    grp = m[9] if m else 1
    return next((r[3] for r in md('m_chocobo_rank_sale') if r[1] == grp and r[2] == choco.rank_type), 0)

def chocobo_sell(st, req, ctx):
    q = req.post_pvt_chocobo_sell
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtChocoboSellResponse()
    with st.transaction('chocobo/sell') as tx:
        total = 0
        for uid in q.user_chocobo_id_list:
            choco = st.get('user_chocobo_list', uid)
            if not choco or choco.is_lock:
                continue
            for deck in list(st.tables.get('user_chocobo_expedition_deck_list', {}).values()):
                if deck.user_chocobo_id != uid:
                    continue
                if deck.lap_time_sec:
                    _collect_deck(st, tx, deck, now, body.chocobo_expedition_result_map[deck.chocobo_expedition_deck_id])
                tx.upsert('user_chocobo_expedition_deck_list', st.new_row('user_chocobo_expedition_deck_list', chocobo_expedition_deck_id=deck.chocobo_expedition_deck_id))
            total += _sale_medals(choco)
            tx.delete('user_chocobo_list', choco)
        if total:
            add_item(st, tx, MEDAL_ITEM, total)
        body.sales_medal = total
    return _resp(tx, 'post_pvt_chocobo_sell', body)

def chocobo_lock(st, req, ctx):
    q = req.post_pvt_chocobo_lock
    with st.transaction('chocobo/lock') as tx:
        for uid, locked in q.chocobo_lock_map.items():
            choco = st.get('user_chocobo_list', uid)
            if choco:
                choco.is_lock = bool(locked)
                tx.touch('user_chocobo_list', choco)
    return _resp(tx, 'post_pvt_chocobo_lock')

def chocobo_name_update(st, req, ctx):
    q = req.post_pvt_chocobo_name_update
    with st.transaction('chocobo/name/update') as tx:
        choco = st.get('user_chocobo_list', q.user_chocobo_id)
        if choco:
            choco.name = q.chocobo_name
            tx.touch('user_chocobo_list', choco)
    return _resp(tx, 'post_pvt_chocobo_name_update')
HANDLERS.update({'/api/pvt/chocobo/sell': chocobo_sell, '/api/pvt/chocobo/lock': chocobo_lock, '/api/pvt/chocobo/name/update': chocobo_name_update, '/api/pvt/chocobo/expedition/result': chocobo_expedition_result, '/api/pvt/chocobo/expedition/edit': chocobo_expedition_edit, '/api/pvt/chocobo/expedition/shorten': chocobo_expedition_shorten, '/api/pvt/chocobo/expedition/extend': chocobo_expedition_extend, '/api/pvt/chocobo/farm/dungeon/treasure': chocobo_farm_dungeon_treasure})

def _scores_sync(st):
    mine = {str(r.damage_challenge_battle_id): r.high_score for r in st['user_damage_challenge_battle_list'].values()}
    if not st.folder:
        return {str(st.user_id): mine}
    from engine.vault import board_all, board_put
    acc = os.path.dirname(st.folder)
    board = board_all(acc, 'damage')
    if board.get(str(st.user_id)) != mine:
        board_put(acc, 'damage', st.user_id, mine)
        board[str(st.user_id)] = mine
    return board

def damage_challenge_top(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtDamageChallengeTopResponse()
    with st.transaction('damage/challenge/top') as tx:
        board = _scores_sync(st)
        for r in st['user_damage_challenge_battle_list'].values():
            bid = str(r.damage_challenge_battle_id)
            better = sum((1 for uid, sc in board.items() if uid != str(st.user_id) and sc.get(bid, 0) > r.high_score))
            hi = body.high_score_info_list.add()
            hi.damage_challenge_battle_id = r.damage_challenge_battle_id
            hi.high_score = r.high_score
            hi.rank = better + 1
    return _resp(tx, 'post_pvt_damage_challenge_top', body)

def damage_challenge_battle_start(st, req, ctx):
    q = req.post_pvt_damage_challenge_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtDamageChallengeBattleStartResponse()
    with st.transaction('damage/challenge/battle/start') as tx:
        ch = st.get('user_damage_challenge_battle_list', q.damage_challenge_battle_id) or st.new_row('user_damage_challenge_battle_list', damage_challenge_battle_id=q.damage_challenge_battle_id)
        ch.total_entry_count += 1
        tx.upsert('user_damage_challenge_battle_list', ch)
        prev = st.one('user_battle_entry_list')
        e = st.new_row('user_battle_entry_list')
        e.entry_count = (prev.entry_count if prev else 0) + 1
        e.battle_id = e.content_id = q.damage_challenge_battle_id
        e.battle_type = P.Command_Enums_BattleType.Value('Command_Enums_BattleType_DamageChallenge')
        e.battle_progress_type = P.Command_Enums_BattleProgressType.Value('Command_Enums_BattleProgressType_Start')
        _entry_versions(e, prev, ctx)
        e.start_datetime = now
        tx.upsert('user_battle_entry_list', e)
        _scores_sync(st)
        body.entry_count = e.entry_count
    return _resp(tx, 'post_pvt_damage_challenge_battle_start', body)
DC_ELEMENT = {1: 'No', 2: 'Fire', 3: 'Ice', 4: 'Thunder', 5: 'Earth', 6: 'Water', 7: 'Wind'}
DC_FIRST = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_DamageChallenge')
DC_FIRST_MSG = 991000000000090
DC_SCORE_SHIFT = 20

def _dc_record(st, tx, ctx, bid, score, first_infos):
    """A Max Damage Challenge fight of fight id bid did score damage: a better score is the new high score, the
    element's play record, and the fight's first-clear reward on its first score."""
    if score <= 0:
        return
    ch = st.get('user_damage_challenge_battle_list', bid) or st.new_row('user_damage_challenge_battle_list', damage_challenge_battle_id=bid)
    first = ch.high_score == 0
    if score > ch.high_score:
        ch.high_score = score
        ch.last_update_score_datetime = now_ms()
        tx.upsert('user_damage_challenge_battle_list', ch)
    dcb = mdx('m_damage_challenge_battle', 0).get((bid,))
    if dcb:
        el = DC_ELEMENT.get(dcb[6])
        if el:
            _pr_max(st, tx, f'DamageChallenge{el}ElementStageHighScore', score)
        if first and dcb[3]:
            _first_rewards(st, tx, ctx, dcb[3], 1, first_infos, DC_FIRST, DC_FIRST_MSG)

def damage_challenge_battle_end(st, req, ctx):
    q = req.post_pvt_damage_challenge_battle_end
    raw = q.damage_challenge_battle_id
    bid, score = (raw & (1 << DC_SCORE_SHIFT) - 1, raw >> DC_SCORE_SHIFT)
    q.damage_challenge_battle_id = bid
    body = P.Command_ApiNetwork_Api_PostPvtDamageChallengeBattleEndResponse()
    with st.transaction('damage/challenge/battle/end') as tx:
        e = st.one('user_battle_entry_list')
        if e and e.battle_id == bid:
            e.battle_progress_type = P.Command_Enums_BattleProgressType.Value('Command_Enums_BattleProgressType_End')
            tx.touch('user_battle_entry_list', e)
        _dc_record(st, tx, ctx, bid, score, body.battle_result.first_reward_infos)
        # "Attempt the Max. Damage Challenge N time(s)" (m_mission col 5 = the fight, 0 = any) and "Obtain a score of N
        # or higher in Damage Challenge" (col 5 = the fight, steps are scores, AutoMissionCountType 2 = absolute)
        mission_event(st, tx, 'ProgressDamageChallengeBattleChallenge', 1, bid)
        ch = st.get('user_damage_challenge_battle_list', bid)
        if ch is not None:
            mission_event(st, tx, 'ProgressDamageChallengeBattleScore', ch.high_score, bid)
        body.battle_result.SetInParent()
        body.score_result.final_score = score
        body.battle_input.total_damage = score
        _scores_sync(st)
    return _resp(tx, 'post_pvt_damage_challenge_battle_end', body)
HANDLERS.update({'/api/pvt/damage/challenge/top': damage_challenge_top, '/api/pvt/damage/challenge/battle/start': damage_challenge_battle_start, '/api/pvt/damage/challenge/battle/end': damage_challenge_battle_end})
DAMAGE_EVENT = 9890
WIN = P.Command_Enums_BattleResultType.Value('Command_Enums_BattleResultType_Win')
RANK_S = P.Command_Enums_BattleRankType.Value('Command_Enums_BattleRankType_S')
BOOST_COST = {0: 1, 1: 3, 2: 3, 3: 6, 4: 6, 5: 9, 6: 9}
BOOST_MULT = {0: 1, 1: 3, 2: 4, 3: 6, 4: 8, 5: 9, 6: 12}
LEVEL_CAPPED_CAMPAIGNS = set()
_CAMP_LV_CAP = {853000000000027: 10, 853000000000028: 10, 853000000000029: 10, 853000000000030: 30, 853000000000032: 20}
_CAMP = {}

def _camp_index():
    if 'built' in _CAMP:
        return
    _CAMP['area'] = {r[0]: r[1] for r in md('m_solo_area')}
    _CAMP['gtype'] = {r[0]: r[1] for r in md('m_solo_area_group')}
    _CAMP['premium'] = {x for g, t, x in md('m_campaign_target_group_rel') if t == 9}
    _CAMP['ttypes'] = collections.defaultdict(set)
    for g, t in md('m_campaign_target_type_rel'):
        _CAMP['ttypes'][g].add(t)
    _CAMP['built'] = True

def _area_target_types(group_id):
    _camp_index()
    types = {_CAMP['gtype'].get(group_id)} & {1, 2, 3}
    if group_id in _CAMP['premium']:
        types.add(8)
    return types

def _comeback_active(st, now):
    days = {r[0]: r[6] for r in md('m_comeback')}
    return {r.comeback_id for r in st['user_comeback_list'].values() if r.comeback_datetime and now < r.comeback_datetime + days.get(r.comeback_id, 7) * DAY_MS}

def campaign_fx(st, solo_area_id, now, applied=None):
    _camp_index()
    types = _area_target_types(_CAMP['area'].get(solo_area_id, 0))
    fx = {1: 1.0, 2: 1.0, 3: 1.0}
    if not types:
        return fx
    cb = None
    plv = None
    for c in md('m_campaign'):
        if c[1] not in (1, 3) or c[0] in LEVEL_CAPPED_CAMPAIGNS:
            continue
        if not (c[9] <= now and (c[10] == 0 or now <= c[10])):
            continue
        if c[1] == 3:
            if cb is None:
                cb = _comeback_active(st, now)
            if c[12] not in cb:
                continue
        if not _CAMP['ttypes'].get(c[6], set()) & types:
            continue
        for e in md('m_campaign_effect'):
            if e[0] != c[2] or e[1] not in fx:
                continue
            cap = _CAMP_LV_CAP.get(e[4]) if len(e) > 4 else None
            if cap:
                if plv is None:
                    s0 = st.one('user_status_list')
                    plv = _lvl_user(s0.exp if s0 else 0)
                if plv > cap:
                    continue
            fx[e[1]] = max(fx[e[1]], e[2] / 1000)
            if applied is not None:
                applied.append((c[0], e[1]))
    return fx

def _stage(event_battle_id):
    return mdx('m_event_solo_battle', 0).get((event_battle_id,))

def _battle(battle_id):
    return mdx('m_battle', 0).get((battle_id,))
def _entry_versions(e, prev, ctx=None):
    # New network entries use this client's version and the currently served data.
    # Existing entry metadata is useful only to internal callers without a context.
    for field in ('app_version', 'master_version'):
        value = ctx.get(field, '') if ctx is not None else getattr(prev, field, '')
        setattr(e, field, value or '')

def _new_entry(st, tx, battle_id, content_id, btype, party_id, boost, now, ctx=None):
    prev = st.one('user_battle_entry_list')
    e = st.new_row('user_battle_entry_list')
    e.entry_count = (prev.entry_count if prev else 0) + 1
    e.battle_id = battle_id
    e.content_id = content_id
    e.battle_type = P.Command_Enums_BattleType.Value(btype)
    e.battle_progress_type = P.Command_Enums_BattleProgressType.Value('Command_Enums_BattleProgressType_Start')
    if party_id:
        e.party_id = party_id
    if boost:
        e.stamina_boost_type = boost
    _entry_versions(e, prev, ctx)
    e.start_datetime = now
    tx.upsert('user_battle_entry_list', e)
    return e

def _end_entry(st, tx, battle_id):
    e = st.one('user_battle_entry_list')
    if e and e.battle_id == battle_id:
        e.battle_progress_type = P.Command_Enums_BattleProgressType.Value('Command_Enums_BattleProgressType_End')
        tx.touch('user_battle_entry_list', e)
    return e

def _party_characters(st, party_id):
    members = sorted((m for m in st['user_party_member_list'].values() if m.party_member_id // 10 == party_id), key=lambda m: m.idx)
    out = []
    for m in members:
        if not m.character_id:
            continue
        c = st.get('user_character_list', m.character_id)
        if c is None:
            print(f'party {party_id}: member character {m.character_id} not on the account - skipped')
            continue
        out.append(c)
    return out

def _waves(battle_id):
    bt = _battle(battle_id)
    return sorted((w for w in md('m_battle_wave') if w[0] == bt[2]), key=lambda w: w[1]) if bt else []

def _start_common(st, tx, body, battle_id, content_id, btype, party_id, boost, now, rare=False, ctx=None):
    e = _new_entry(st, tx, battle_id, content_id, btype, party_id, boost, now, ctx=ctx)
    party = st.get('user_party_list', party_id)
    if party:
        tx.touch('user_party_list', party)
    for m in st['user_party_member_list'].values():
        if m.party_member_id // 10 == party_id:
            tx.touch('user_party_member_list', m)
    for w in _waves(battle_id):
        body.battle_wave_infos.add().enemy_group_id = w[2]
    if rare:
        _rare_roll(body, battle_id, now)
    body.entry_count = e.entry_count
    return e
_RARE = {}

def _rare_tables():
    src = (md('m_battle_rare_wave2'), md('m_battle_rare_wave'), md('m_battle_rare_wave_rel'), md('m_battle_rare_wave_encounter'))
    if _RARE.get('src') is not None and all((a is b for a, b in zip(_RARE['src'], src))):
        return _RARE
    v2, v1g, t1, rel = ({}, {}, {}, {})
    bt = {r[0] for r in md('m_battle')}
    for r in src[0]:
        b = _rare2_battle(r[0], bt)
        if b:
            v2.setdefault(b, []).append((r[0], _grp(r[1]), 0))
    for r in src[1]:
        if r[5] == 1:
            t1.setdefault(r[0] % 10 ** 10 // 100, []).append((r[0], _grp(r[1]), r[2]))
        else:
            v1g.setdefault(r[0] // 10, []).append((r[0], _grp(r[1]), r[2]))
    for r in src[2]:
        rel.setdefault(r[1], []).append((r[0], r[2]))
    grps = {g for d in (v2, v1g, t1) for L in d.values() for _, g, _ in L}
    ens = {}
    for r in md('m_battle_enemy'):
        if r[0] in grps and r[6]:
            ens.setdefault(r[0], []).append((r[1], r[6]))
    _RARE.clear()
    _RARE.update(src=src, v2=v2, v1g=v1g, t1=t1, rel=rel, enc={r[1]: r[3] for r in src[3]}, ens=ens)
    return _RARE

def _grp(x):
    return (x[0] if x else 0) if isinstance(x, (list, tuple)) else x

def _rare2_battle(rid, battles):
    for d in (10, 1, 1000):
        if rid // d in battles:
            return rid // d
    return 0

def _condset_ok(cs, now):
    if not cs:
        return True
    s = mdx('m_condition_set', 0).get((cs,))
    rows = _cond_rows(cs)
    if not rows:
        return True
    ok = [(not c[5] or now >= c[5]) and (not c[6] or now <= c[6]) for c in rows if c[1] == 2]
    if not ok:
        return True
    return any(ok) if s and s[1] == 2 else all(ok)

def _rare_pool(battle_id, now):
    t = _rare_tables()
    if battle_id in t['v2']:
        return (2, t['v2'][battle_id])
    out = []
    for sid, g in t['rel'].get(battle_id, ()):
        if _condset_ok(t['enc'].get(sid, 0), now):
            out += t['v1g'].get(g, [])
    out += t['t1'].get(battle_id, [])
    return (1, out) if out else (0, [])

def _rare_roll(body, battle_id, now):
    ver, pool = _rare_pool(battle_id, now)
    if not pool or random.random() >= CFG.get('rare_wave_chance', 0.25):
        return None
    rid, grp, _ = random.choice(pool)
    waves = [w.enemy_group_id for w in body.battle_wave_infos]
    pos = random.randrange(len(waves)) if ver == 1 and rid // 10 ** 10 != 8000 and waves else 0
    del body.battle_wave_infos[:]
    for i, g in enumerate(waves[:pos] + [None] + waves[pos:]):
        w = body.battle_wave_infos.add()
        if g is None:
            w.enemy_group_id = grp
            w.is_rare_enemy = True
        else:
            w.enemy_group_id = g
    ri = body.rare_wave_info
    ri.battle_rare_wave_id = rid
    if pos:
        ri.insert_idx = pos
    ri.rare_wave_version = P.Command_Enums_BattleRareWaveVersion.Value(f'Command_Enums_BattleRareWaveVersion_Version{ver}')
    return rid

def _rare_drops(st, tx, battle_id, enemy_results, mult, infos, now):
    ver, pool = _rare_pool(battle_id, now)
    by_grp = {grp: (rid, rs) for rid, grp, rs in pool}
    paid = []
    for er in enemy_results:
        hit = by_grp.get(er.enemy_group_id)
        if not hit or not er.win_enemy_idxs:
            continue
        won = set(er.win_enemy_idxs)
        for idx, rs in _rare_tables()['ens'].get(er.enemy_group_id, ()):
            if idx in won:
                grant_reward_set(st, tx, rs, mult, infos)
        if hit[1]:
            grant_reward_set(st, tx, hit[1], mult, infos)
        paid.append(hit[0])
    return paid

def _drops(st, tx, battle_id, enemy_results, mult, infos):
    for w in _waves(battle_id):
        er = next((x for x in enemy_results if x.enemy_group_id == w[2]), None)
        if er is None:
            continue
        won = set(er.win_enemy_idxs)
        for en in md('m_battle_enemy'):
            if en[0] == w[2] and en[1] in won and en[6]:
                grant_reward_set(st, tx, en[6], mult, infos)
        if w[3]:
            grant_reward_set(st, tx, w[3], mult, infos)

def _rank_drops(st, tx, rank_group, mult, infos, chars, items=1.0):
    for _ in range(mult):
        for c in chars:
            infos.add().reward_type = RT_EXP
    rows = [r for r in md('m_battle_rank') if r[0] == rank_group]
    if not rows:
        return
    best = max(rows, key=lambda r: r[1])
    times = max(1, best[3]) * mult * CFG.get('drop_rolls_per_boost', 1)
    if items != 1.0:
        times = max(times, round(times * items))
    if best[2]:
        grant_reward_set(st, tx, best[2], times, infos)
# #104 event bonuses: an event's m_event_base col 21 EventBonusEffectGroupId -> m_event_bonus_effect rows [0 Id, 1 group,
# 2 ItemId, 3 MaxEventBonusEffectCoefficientPermil, 4 weapon group, 5 costume group, 6 LockConditionSetId, 7 Priority].
# The game shows the party's bonus but leaves the extra drops to the server (event_bonus_reward_infos), worked out as the
# party screen does: every active member's six weapon slots and three outfits (not the look), each adding its row's
# per-mille; a weapon's row is the highest tier it has reached (m_event_bonus_effect_weapon_bonus). Capped at the row's
# maximum, the bonus is that share of the stage's drops of the row's item, rounded down.
_WEAPON_SLOTS = ('weapon_id0', 'weapon_id1', 'sub_weapon_id0', 'sub_weapon_id1', 'sub_weapon_id2', 'legendary_weapon_id0')
_OUTFIT_SLOTS = ('costume_id', 'sub_costume_id0', 'sub_costume_id1')
LEGENDARY = 101

def _event_bonus_effects(event_id, now):
    eb = mdx('m_event_base', 0).get((event_id,))
    group = eb[21] if eb and len(eb) > 21 else 0
    return [r for r in md('m_event_bonus_effect') if r[1] == group and _condset_ok(r[6], now)] if group else []

def _weapon_bonus(effect, w, now):
    """The per-mille an owned weapon adds to one bonus row: its tier row with the highest rarity, upgrade type and count
    that the weapon has reached (Rank counts its upgrades, Limit counts 10 more)."""
    row = next((r for r in md('m_event_bonus_effect_weapon') if r[2] == effect[4] and r[1] == w.weapon_id
                and _condset_ok(r[4], now)), None)
    if row is None:
        return 0
    mw = mdx('m_weapon', 0).get((w.weapon_id,))
    legendary = bool(mw and len(mw) > 21 and mw[21])
    tiers = [t for t in md('m_event_bonus_effect_weapon_bonus') if t[1] == row[3] and (t[2] == LEGENDARY) == legendary]
    rank = P.Command_Enums_WeaponUpgradeType.Value('Command_Enums_WeaponUpgradeType_Rank')
    score = w.upgrade_count if w.weapon_upgrade_type == rank else 10 + w.upgrade_count
    for t in sorted(tiers, key=lambda t: (t[2], t[3], t[4]), reverse=True):
        if t[2] <= w.rarity_type and (0 if t[3] == rank else 10) + t[4] <= score:
            return t[5]
    return 0

def _party_event_bonus(st, party_id, effect, now):
    total = 0
    for m in st['user_party_member_list'].values():
        if m.party_member_id // 10 != party_id or not m.character_id or not m.is_active:
            continue
        for f in _WEAPON_SLOTS:
            w = st.get('user_weapon_list', getattr(m, f)) if getattr(m, f) else None
            if w is not None:
                total += _weapon_bonus(effect, w, now)
        for f in _OUTFIT_SLOTS:
            cid = getattr(m, f)
            if cid:
                total += next((r[3] for r in md('m_event_bonus_effect_costume') if r[1] == effect[5] and r[2] == cid
                               and _condset_ok(r[4], now)), 0)
    return min(total, effect[3])

def _event_bonus_drops(st, tx, event_id, party_id, drops, infos, now):
    """The bonus share of the stage's item drops (drops: the battle's reward_infos) for the stage's own event, so a
    stage on a list card pays its event's bonus."""
    counts = {}
    for d in drops:
        if d.reward_type == RT_ITEM:
            counts[d.target_id] = counts.get(d.target_id, 0) + d.count
    for effect in _event_bonus_effects(event_id, now):
        n = counts[effect[2]] * _party_event_bonus(st, party_id, effect, now) // 1000 if effect[2] in counts else 0
        if n > 0:
            add_item(st, tx, effect[2], n)
            ri = infos.add()
            ri.reward_type, ri.target_id, ri.count = RT_ITEM, effect[2], n
import threading as _thr
_SKIP_ALL_CHARS = _thr.local()

def _end_common(st, tx, body, q, battle_id, stage_stamina=0, win_exp=True, all_chars=False, kinds=(), fx=None):
    e = _end_entry(st, tx, battle_id)
    bt = _battle(battle_id)
    status = st.one('user_status_list')
    chars = list(st['user_character_list'].values()) if all_chars else _party_characters(st, e.party_id) if e else []
    xchars = list(st['user_character_list'].values()) if getattr(_SKIP_ALL_CHARS, 'on', False) else chars
    if hasattr(body, 'battle_result'):
        # sent even when it would be empty: the game waits on it after every fight, and a replayed story fight (guest
        # party, so no characters, and nothing but its first-clear reward) otherwise ends on a black screen (10-21)
        body.battle_result.SetInParent()
    for c in xchars:
        body.battle_result.before_user_characters.add().CopyFrom(c)
    if hasattr(body, 'before_exp'):
        body.before_exp = status.exp if status else 0
    win = q.battle_result_type == WIN
    mult = BOOST_MULT.get(e.stamina_boost_type if e else 0, 1)
    cost = BOOST_COST.get(e.stamina_boost_type if e else 0, 1)
    if win:
        dmg = q.battle_input.total_damage
        if hasattr(body, 'score_result'):
            sr = body.score_result
            sr.final_score = dmg
            sr.max_blow_damage_score = q.battle_input.max_blow_damage
            for f in ('max_blow_damage_score_battle_rank_type', 'battle_time_score_battle_rank_type', 'taken_damage_score_battle_rank_type', 'death_count_score_battle_rank_type', 'final_score_battle_rank_type'):
                setattr(sr, f, RANK_S)
        if bt and win_exp:
            for c in xchars:
                if bt[13]:
                    cap = max((r[2] for r in md('m_character_level') if r[0] == c.character_id), default=c.exp)
                    new_exp = min(cap, c.exp + round(bt[13] * mult * CFG.get('exp_mult', 1) * (fx or {}).get(2, 1.0)))
                    if new_exp != c.exp:
                        c.exp = new_exp
                        tx.touch('user_character_list', c)
            if status and bt[14]:
                cap = max((r[2] for r in md('m_user_rank')))
                status.exp = min(cap, status.exp + round(bt[14] * mult * CFG.get('exp_mult', 1) * (fx or {}).get(1, 1.0)))
        if status and stage_stamina:
            stamina_add(status, -stage_stamina * cost)
            tx.touch('user_status_list', status)
        elif status and bt and bt[14] and win_exp:
            tx.touch('user_status_list', status)
        stam = stage_stamina * cost
        _battle_medals(st, tx, body, stam)
        mission_event(st, tx, 'ProgressBattleWin', 1, battle_id)
        mission_event(st, tx, 'ProgressConsumptionStaminaBattle', stam)
        mission_event(st, tx, 'ProgressConsumptionStaminaSoloBattleAll', stam)
        mission_event(st, tx, 'ProgressConsumptionStaminaMultiBattleAll', stam)   # no co-op offline: solo stamina counts for the co-op stamina missions
        mission_event(st, tx, 'ProgressAbsoluteBattleMaxBlowDamage', q.battle_input.max_blow_damage)
        fg = sum(r.break_count for er in getattr(q, 'battle_enemy_results', ()) for r in er.skill_force_gauge_results)
        if fg:    # 153 ProgressSkillEnemyForceGaugeBreak ("Purge data N times"): its target is the mission's own id, not a gauge, so any break on a win counts
            for m in _mt_index().get(MTYPE['ProgressSkillEnemyForceGaugeBreak'], ()):
                mission_event(st, tx, 'ProgressSkillEnemyForceGaugeBreak', fg, m[5])
        for c in chars:
            # col 6 = the battle ("Win battle X with Y"); missions without one count any battle with that character
            mission_event(st, tx, 'ProgressBattleWinWithCharacter', 1, c.character_id, sub=battle_id)
            mission_event(st, tx, 'ProgressConsumptionStaminaBattleWithCharacter', stam, c.character_id)
        for k, prm in kinds:
            mission_event(st, tx, k, stam if 'Stamina' in k else 1, prm)
    else:
        if hasattr(body, 'score_result'):
            body.score_result.SetInParent()
        if status and stage_stamina:
            stamina_add(status, -1)
            tx.touch('user_status_list', status)
            mission_event(st, tx, 'ProgressConsumptionStaminaBattle', 1)
            mission_event(st, tx, 'ProgressConsumptionStaminaSoloBattleAll', 1)
            mission_event(st, tx, 'ProgressConsumptionStaminaMultiBattleAll', 1)
    for c in xchars:
        body.battle_result.after_user_characters.add().CopyFrom(c)
    if hasattr(body, 'after_exp'):
        body.after_exp = status.exp if status else 0
    if hasattr(body, 'battle_input'):
        body.battle_input.CopyFrom(q.battle_input)
    return (e, win, mult, chars, status)
MINOR_GUILD_MEDAL = 52
COOP_MEDAL = 13
CRISIS_MEDAL = 12
GUILD_MEDAL, GUILD_RARE_MEDAL = 49, 50
_BATTLE_MEDALS = ((MINOR_GUILD_MEDAL, 'minor_guild_medals_per_stamina'), (COOP_MEDAL, 'coop_medals_per_stamina'),
                  (CRISIS_MEDAL, 'crisis_medals_per_stamina'), (GUILD_MEDAL, 'guild_medals_per_stamina'),
                  (GUILD_RARE_MEDAL, 'guild_rare_medals_per_stamina'))

def _battle_medals(st, tx, body, stamina):
    """Square's servers gave Minor Guild Medals and Co-op Medals for co-op, which this server can't host. Solo wins that
    cost stamina give CFG['minor_guild_medals_per_stamina'] and CFG['coop_medals_per_stamina'] per stamina point
    instead, shown with the battle's rewards. Crisis Medals came only once each (first story clears, treasure,
    missions), so CFG['crisis_medals_per_stamina'] keeps the Crisis Medal exchange buyable (#95). Guild Medals and Guild
    Rare Medals came only from guild battles, which need Square's servers: CFG['guild_medals_per_stamina'] and
    CFG['guild_rare_medals_per_stamina']. A rate can be a fraction: the part below one medal is a chance of one more
    (0.25 on a 10-stamina win: 2, or 3 one time in two), so short fights still earn it at the same rate. add_item applies each item's per-period limit (Co-op Medal: 9,999 a week)."""
    for item_id, key in _BATTLE_MEDALS:
        earned = stamina * CFG.get(key, 0)
        n = int(earned)
        if random.random() < earned - n:
            n += 1
        if n <= 0:
            continue
        row = st.get('user_item_list', item_id)
        before = row.count if row else 0
        add_item(st, tx, item_id, n)
        got = st.get('user_item_list', item_id).count - before
        if got > 0:
            ri = body.battle_result.reward_infos.add()
            ri.reward_type = RT_ITEM
            ri.target_id = item_id
            ri.count = got
BOSS_FIRST = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_BossChallengeFirst')

def _boss_challenge_clear(st, tx, ctx, stage, e, body):
    cid = stage[22] if stage and len(stage) > 22 else 0
    lv = e.boss_challenge_level if e else 0
    if not cid or not lv:
        return
    levels = {r[1]: r for r in md('m_boss_challenge_level') if r[0] == cid}
    if lv not in levels:
        print(f'escalation: challenge {cid} has no level {lv} - clear not saved')
        return
    row = st.get('user_boss_challenge_list', cid) or st.new_row('user_boss_challenge_list', boss_challenge_id=cid)
    old = row.clear_level
    if lv <= old:
        return
    res = body.boss_challenge_result
    for L in range(old + 1, lv + 1):
        r = levels.get(L)
        if not r:
            continue
        lr = res.level_result_list.add()
        lr.level = L
        if r[5]:
            _first_rewards(st, tx, ctx, r[5], 1, lr.first_reward_info_list, BOSS_FIRST, BATTLE_FIRST_MSG)
            # the result screen lists the result's own first_reward_info_list (every level newly cleared), not the
            # per-level lists
            res.first_reward_info_list.extend(lr.first_reward_info_list)
    row.clear_level = lv
    tx.upsert('user_boss_challenge_list', row)
    mission_event(st, tx, 'ProgressBossChallengeClear', 1, cid, reached=lv)

def event_solo_battle_start(st, req, ctx):
    q = req.post_pvt_event_solo_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtEventSoloBattleStartResponse()
    with st.transaction('event/solo/battle/start') as tx:
        stage = _stage(q.event_battle_id)
        e = _start_common(st, tx, body, stage[5] if stage else q.event_battle_id, q.event_battle_id, 'Command_Enums_BattleType_EventSolo', q.party_id, q.stamina_boost_type, now, rare=True, ctx=ctx)
        if q.boss_challenge_level:
            e.boss_challenge_level = q.boss_challenge_level
            tx.upsert('user_battle_entry_list', e)
        row = st.get('user_event_solo_battle_list', q.event_battle_id) or st.new_row('user_event_solo_battle_list', event_solo_battle_id=q.event_battle_id)
        tx.upsert('user_event_solo_battle_list', row)
        # "Attempt a Solo battle in <event>": m_mission col 5 names the original stage, so a list-card copy counts as it
        mission_event(st, tx, 'ProgressEventBattleEntry', 1, _stage_copies().get(q.event_battle_id, q.event_battle_id))
    return _resp(tx, 'post_pvt_event_solo_battle_start', body)

def _coop_fight(stage_id, battle_id):
    """The co-op fight (m_event_multi_battle: 2 EventBaseId, 4 BattleId, 23 EventMultiAreaId) that a co-op-as-solo
    stage copies (engine/cards.py coop_solo: stage event * 1000 + 801.., the fight's own battle), else None."""
    from engine import cards
    if stage_id % 1000 <= cards.COOP_STAGES:
        return None
    return next((f for f in md('m_event_multi_battle') if f[2] == stage_id // 1000 and f[4] == battle_id), None)

def event_solo_battle_end(st, req, ctx):
    q = req.post_pvt_event_solo_battle_end
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtEventSoloBattleEndResponse()
    with st.transaction('event/solo/battle/end') as tx:
        stage = _stage(q.event_battle_id)
        bid = stage[5] if stage else q.event_battle_id
        o_sid = _stage_copies().get(q.event_battle_id, q.event_battle_id)
        o_stage = _stage(o_sid) if o_sid != q.event_battle_id else stage
        ev = o_stage[2] if o_stage else 0
        ev_id = o_sid // 1000
        sb = _score_battle(o_stage or stage)
        if sb:   # the game ends a score battle when its time runs out, not with a win: any score that wasn't a retire counts
            print(f'score battle {q.event_battle_id}: ended as {P.Command_Enums_BattleResultType.Name(q.battle_result_type)}, damage {q.battle_input.total_damage}')
            if q.battle_result_type not in (WIN, RETIRE) and q.battle_input.total_damage > 0:
                q.battle_result_type = WIN
        kinds = (('ProgressEventBattleWin', ev_id), ('ProgressBattleEventSoloAreaWin', ev), ('ProgressConsumptionStaminaEventBattle', ev_id), ('ProgressConsumptionStaminaEventSoloAreaBattle', ev))
        coop = _coop_fight(o_sid, bid)
        if coop:    # a co-op fight played as solo also counts for its event's co-op stamina missions
            kinds += (('ProgressConsumptionStaminaEventMultiAreaBattle', coop[23]),)
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, stage[8] if stage else 0, kinds=kinds)
        dmg = q.battle_input.total_damage
        if win:
            row = st.get('user_event_solo_battle_list', q.event_battle_id) or st.new_row('user_event_solo_battle_list', event_solo_battle_id=q.event_battle_id)
            o = _stage_copies().get(q.event_battle_id)
            orow = st.get('user_event_solo_battle_list', o) if o else None
            first = row.total_win_count == 0 and (not (orow and orow.total_win_count > 0))
            partners = _stage_partners(q.event_battle_id)
            for p in partners:    # #102: a list-card copy and its original share the day's tries (the later one counts)
                prow = st.get('user_event_solo_battle_list', p)
                if prow and prow.last_challenge_reset_datetime > row.last_challenge_reset_datetime:
                    row.challenge_count, row.last_challenge_reset_datetime = prow.challenge_count, prow.last_challenge_reset_datetime
            _daily_count(row, 'challenge_count', 'last_challenge_reset_datetime', now, (o_stage or stage)[12] if o_stage or stage else 1)
            row.total_win_count += 1
            old_high = row.high_score if row.total_win_count > 1 else -1
            rank = _score_battle_rank(sb, dmg) if sb else RANK_S
            if dmg > row.high_score:
                row.high_score = dmg
            row.high_rank_type = max(row.high_rank_type, rank)
            tx.upsert('user_event_solo_battle_list', row)
            for p in partners:    # a list-card copy and its original clear together, and use the same tries
                prow = st.get('user_event_solo_battle_list', p) or st.new_row('user_event_solo_battle_list', event_solo_battle_id=p)
                if prow.total_win_count == 0:
                    prow.total_win_count = 1
                    prow.high_rank_type = max(prow.high_rank_type, rank)
                prow.challenge_count, prow.last_challenge_reset_datetime = row.challenge_count, row.last_challenge_reset_datetime
                tx.upsert('user_event_solo_battle_list', prow)
            _score_battle_missions(st, tx, q.event_battle_id, rank, dmg)
            if sb:
                _score_battle_result(st, tx, sb, old_high, dmg, rank, body)
            # #76: a "[1/Day]" stage (Golden Bomb Rush) pays nothing but its FirstRewardSetId, so the reward comes with
            # the first win of every reset period, not only the first win ever
            if (first or _daily_first(o_stage or stage, row)) and stage and stage[7]:
                grant_reward_set(st, tx, stage[7], 1, body.battle_result.first_reward_infos)
                _battle_first_to_gifts(st, tx, ctx, body.battle_result.first_reward_infos)
            if first:
                _award_sync(st, tx)
            if stage:
                _rank_drops(st, tx, stage[6], mult, body.battle_result.reward_infos, chars)
                if e:
                    _event_bonus_drops(st, tx, ev_id, e.party_id, body.battle_result.reward_infos, body.event_bonus_reward_infos, now)
            _rare_drops(st, tx, bid, q.battle_enemy_results, mult, body.battle_result.rare_enemy_reward_infos, now)
            if stage and len(stage) > 26 and e:
                _memoria_win(st, tx, e.party_id, stage[8] * mult, stage[25], stage[26], stage[24], q.event_battle_id, mult, now, br=body.battle_result)
            _boss_challenge_clear(st, tx, ctx, o_stage or stage, e, body)
            ep_id = _battle_episode(q.event_battle_id) or _battle_episode(o_sid) or _battle_episode_req(q.episode_id)
            if ep_id:
                _episode_clear(st, tx, ep_id, body.episode_reward_infos)
        body.boost_result.add()
        body.score_battle_score_result.SetInParent()
        _crisis_score(st, tx, q, body, dmg, chars, win, now)
        if stage and stage[2] == DAMAGE_EVENT * 1000 + 1 and (dmg > 0):
            dc = mdx('m_damage_challenge_battle', 2).get((stage[5],))
            if dc:
                ch = st.get('user_damage_challenge_battle_list', dc[0]) or st.new_row('user_damage_challenge_battle_list', damage_challenge_battle_id=dc[0])
                ch.total_entry_count += 1
                if dmg > ch.high_score:
                    ch.high_score = dmg
                    ch.last_update_score_datetime = now
                tx.upsert('user_damage_challenge_battle_list', ch)
                _scores_sync(st)
    return _resp(tx, 'post_pvt_event_solo_battle_end', body)

def _floor(fid):
    return mdx('m_tower_floor', 0).get((fid,))

def _area_battle(aid):
    return mdx('m_solo_area_battle', 0).get((aid,))

def _story_battle(sid, mode):
    r = mdx('m_story_battle', 0).get((sid,))
    return (r[2] if mode == 2 and r[2] else r[1]) if r else sid

def tower_battle_start(st, req, ctx):
    q = req.post_pvt_tower_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtTowerBattleStartResponse()
    with st.transaction('tower/battle/start') as tx:
        f = _floor(q.tower_floor_id)
        _start_common(st, tx, body, f[5] if f else q.tower_floor_id, q.tower_floor_id, 'Command_Enums_BattleType_Tower', q.party_id, 0, now, ctx=ctx)
    return _resp(tx, 'post_pvt_tower_battle_start', body)

def tower_battle_end(st, req, ctx):
    q = req.post_pvt_tower_battle_end
    body = P.Command_ApiNetwork_Api_PostPvtTowerBattleEndResponse()
    with st.transaction('tower/battle/end') as tx:
        f = _floor(q.tower_floor_id)
        bid = f[5] if f else q.tower_floor_id
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, kinds=(('ProgressTowerBattleWin', 0),))
        if win and f:
            t = st.get('user_tower_list', f[1]) or st.new_row('user_tower_list', tower_id=f[1])
            first = t.clear_floor_seq < f[2]
            if first:
                t.clear_floor_seq = f[2]
                tx.upsert('user_tower_list', t)
                if f[6]:
                    grant_reward_set(st, tx, f[6], 1, body.battle_result.first_reward_infos)
                    _battle_first_to_gifts(st, tx, ctx, body.battle_result.first_reward_infos, TOWER_FIRST_OBTAIN, TOWER_FIRST_MSG)
                if f[7]:
                    grant_reward_set(st, tx, f[7], 1, body.floor_reward_infos)
    return _resp(tx, 'post_pvt_tower_battle_end', body)

def area_solo_battle_start(st, req, ctx):
    q = req.post_pvt_area_solo_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtAreaSoloBattleStartResponse()
    with st.transaction('area/solo/battle/start') as tx:
        a = _area_battle(q.area_battle_id)
        _start_common(st, tx, body, a[4] if a else q.area_battle_id, q.area_battle_id, 'Command_Enums_BattleType_AreaSolo', q.party_id, q.stamina_boost_type, now, rare=True, ctx=ctx)
        row = st.get('user_solo_area_battle_list', q.area_battle_id) or st.new_row('user_solo_area_battle_list', solo_area_battle_id=q.area_battle_id)
        row.total_entry_count += 1
        tx.upsert('user_solo_area_battle_list', row)
    return _resp(tx, 'post_pvt_area_solo_battle_start', body)

def _area_score(sr, bi):
    fin = sr.final_score
    n = bi.weak_element_hit_count
    sr.weak_element_hit_count_score = 75 + 25 * n if n else 0
    sr.max_blow_damage_score = 1850
    sr.battle_time_score = 4860
    sr.taken_damage_score = 3365
    sr.death_count_score = max(0, 1500 - 500 * bi.death_count)
    sr.final_score = sr.weak_element_hit_count_score + sr.max_blow_damage_score + sr.battle_time_score + sr.taken_damage_score + sr.death_count_score or fin

def area_solo_battle_end(st, req, ctx):
    q = req.post_pvt_area_solo_battle_end
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtAreaSoloBattleEndResponse()
    with st.transaction('area/solo/battle/end') as tx:
        a = _area_battle(q.area_battle_id)
        bid = a[4] if a else q.area_battle_id
        camps = []
        fx = campaign_fx(st, a[1], now, camps) if a else None
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, a[7] if a else 0, fx=fx, kinds=(('ProgressBattleAreaGroupCategoryWin', _area_category(a[1]) if a else 0), ('ProgressBattleAreaWin', q.area_battle_id), ('ProgressConsumptionStaminaAreaBattle', a[1] if a else 0), ('ProgressConsumptionStaminaArea', a[1] if a else 0)))
        if win:
            _area_score(body.score_result, q.battle_input)
        if win:
            row = st.get('user_solo_area_battle_list', q.area_battle_id) or st.new_row('user_solo_area_battle_list', solo_area_battle_id=q.area_battle_id)
            first = row.total_win_count == 0
            row.total_win_count += 1
            _daily_count(row, 'reset_win_count', 'last_win_reset_datetime', now)
            dmg = q.battle_input.total_damage
            if dmg > row.high_score:
                row.high_score = dmg
                row.high_rank_type = RANK_S
            tx.upsert('user_solo_area_battle_list', row)
            if a:
                ar = st.get('user_solo_area_list', a[1]) or st.new_row('user_solo_area_list', solo_area_id=a[1])
                ar.total_win_count += 1
                _daily_count(ar, 'reset_win_count', 'last_win_reset_datetime', now)
                tx.upsert('user_solo_area_list', ar)
                _area_category_win(st, tx, a[1], now)
                if first and a[8]:
                    grant_reward_set(st, tx, a[8], 1, body.battle_result.first_reward_infos)
                    _battle_first_to_gifts(st, tx, ctx, body.battle_result.first_reward_infos)
                _rank_drops(st, tx, a[5], mult, body.battle_result.reward_infos, chars, (fx or {}).get(3, 1.0))
            _rare_drops(st, tx, bid, q.battle_enemy_results, mult, body.battle_result.rare_enemy_reward_infos, now)
            for cid, kind in sorted(camps, key=lambda x: (-x[1], x[0])):
                for _ in chars if kind == 2 else (None,):
                    ci = body.battle_result.normal_campaign_infos.add()
                    ci.campaign_id = cid
                    ci.campaign_effect_type = kind
                if len(a) > 17 and e:
                    _memoria_win(st, tx, e.party_id, a[7] * mult, a[17], 1000, 0, 0, 0, now, br=body.battle_result)
        body.boost_result.add()
    return _resp(tx, 'post_pvt_area_solo_battle_end', body)

def story_battle_start(st, req, ctx):
    q = req.post_pvt_story_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtStoryBattleStartResponse()
    with st.transaction('story/battle/start') as tx:
        if q.HasField('party_info'):
            _save_party_info(st, tx, q.party_info, now)
        bid = _story_battle(q.story_battle_id, q.story_mode_type)
        _start_common(st, tx, body, bid, q.episode_id, 'Command_Enums_BattleType_Story', q.party_id, 0, now, ctx=ctx)
        ee = st.one('user_episode_entry_list') or st.new_row('user_episode_entry_list')
        ee.party_id = q.party_id
        ee.episode_id = q.episode_id
        ee.story_mode_type = q.story_mode_type
        tx.upsert('user_episode_entry_list', ee)
    return _resp(tx, 'post_pvt_story_battle_start', body)

def story_battle_end(st, req, ctx):
    q = req.post_pvt_story_battle_end
    body = P.Command_ApiNetwork_Api_PostPvtStoryBattleEndResponse()
    with st.transaction('story/battle/end') as tx:
        bid = _story_battle(q.story_battle_id, q.story_mode_type)
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, all_chars=True, kinds=(('ProgressCharacterStoryBattleWin', 0),))
        if win and (not st.get('user_episode_battle_list', q.episode_id, q.story_battle_id)):
            tx.upsert('user_episode_battle_list', st.new_row('user_episode_battle_list', story_episode_id=q.episode_id, story_battle_id=q.story_battle_id))
            _drops(st, tx, bid, q.battle_enemy_results, 1, body.battle_result.first_reward_infos)
            _battle_first_to_gifts(st, tx, ctx, body.battle_result.first_reward_infos)
        if win:
            _ste_credit(st, tx, q.episode_id)
    return _resp(tx, 'post_pvt_story_battle_end', body)
HANDLERS.update({'/api/pvt/event/solo/battle/start': event_solo_battle_start, '/api/pvt/event/solo/battle/end': event_solo_battle_end, '/api/pvt/tower/battle/start': tower_battle_start, '/api/pvt/tower/battle/end': tower_battle_end, '/api/pvt/area/solo/battle/start': area_solo_battle_start, '/api/pvt/area/solo/battle/end': area_solo_battle_end, '/api/pvt/story/battle/start': story_battle_start, '/api/pvt/story/battle/end': story_battle_end})

MEDALS = {3: 200, 2: 10, 1: 1}
GACHA_NORMAL = 1
WGT_NEW, WGT_UPDATE, WGT_OVERLAP = (1, 2, 3)
DIRECTION_SSR = {1001: 5, 1002: 9, 1003: 9, 1004: 1, 1005: 2}

def _grant_weapon(st, tx, weapon_id, rarity, infos=None, now=None, from_gacha=True, ri=None):
    now = now or now_ms()
    w = st.get('user_weapon_list', weapon_id)
    mw = mdx('m_weapon', 0).get((weapon_id,))
    if ri is None:
        ri = infos.add() if infos is not None else P.Command_Entity_RewardInfo()
    ri.reward_type = RT_WEAPON
    ri.target_id = weapon_id
    wo = ri.reward_option.weapon_option
    wo.rarity_type = rarity
    count = from_gacha and rarity >= 3
    if w:
        old = w.rarity_type
        if rarity > old:
            n = _medals(old)
            w.rarity_type = rarity
            w.get_datetime = now
            wo.weapon_get_type = WGT_UPDATE
            wo.last_rarity_type = old
        else:
            n = _medals(rarity)
            wo.weapon_get_type = WGT_OVERLAP
            wo.last_rarity_type = rarity
        wo.medal_count = n
        if mw and mw[18]:
            add_item(st, tx, mw[18], n)
        if count:
            w.obtain_count_from_gacha += 1
        tx.touch('user_weapon_list', w)
    else:
        # Square: Rank from obtain. Type 0 sends the Rarity tab to Overboost+ "Unable to overboost+ any further".
        w = st.new_row('user_weapon_list', weapon_id=weapon_id, rarity_type=rarity, get_datetime=now, obtain_count_from_gacha=1 if count else 0,
                       weapon_upgrade_type=P.Command_Enums_WeaponUpgradeType.Value('Command_Enums_WeaponUpgradeType_Rank'))
        tx.upsert('user_weapon_list', w)
        wo.weapon_get_type = WGT_NEW
    return ri

def _reward_weapon_state(st, tx, weapon_id, rwp, ri):
    """m_reward_weapon [id, rarity, level, upgrade type, upgrade count]: a reward can come already levelled and
    overboosted (login bonus 3245: Lv. 120 OB6). An owned weapon is only ever raised to it."""
    w = st.get('user_weapon_list', weapon_id)
    mw = mdx('m_weapon', 0).get((weapon_id,))
    if not w or not mw:
        return
    wo = ri.reward_option.weapon_option
    wo.last_level = _lvl_weapon(weapon_id, w.exp)
    wo.last_weapon_upgrade_type = w.weapon_upgrade_type
    wo.last_upgrade_count = w.upgrade_count
    lv = mdx('m_weapon_level', 0, 1).get((mw[3], rwp[2])) if rwp[2] else None
    if lv and lv[2] > w.exp:
        need = [r[1] for r in md('m_weapon_release_setting') if r[0] == mw[12] and r[2] >= rwp[2]]
        if need:
            w.release_count = max(w.release_count, min(need))
        w.exp = lv[2]
    # A weapon on the Limit track is already past the top Rank overboost.
    if rwp[3] and rwp[4] and w.weapon_upgrade_type != P.Command_Enums_WeaponUpgradeType.Value('Command_Enums_WeaponUpgradeType_Limit') and rwp[4] > w.upgrade_count:
        w.weapon_upgrade_type = rwp[3]
        w.upgrade_count = rwp[4]
    wo.level = _lvl_weapon(weapon_id, w.exp)
    wo.weapon_upgrade_type = w.weapon_upgrade_type
    wo.upgrade_count = w.upgrade_count
    tx.upsert('user_weapon_list', w)

def _grant_batch(st, tx, picks, infos, now=None):
    slots = [infos.add() for _ in picks]
    for i in sorted(range(len(picks)), key=lambda i: -picks[i][1]):
        _grant_weapon(st, tx, picks[i][0], picks[i][1], now=now, ri=slots[i])
    return slots

def _roll(rows, weight_idx):
    total = sum((r[weight_idx] for r in rows))
    x = random.randrange(total) if total > 0 else 0
    for r in rows:
        x -= r[weight_idx]
        if x < 0:
            return r
    return rows[-1]
FIX2LOT = {1: 2, 2: 3, 3: 5, 4: 6, 5: 7}
PICKUP_SHARE = {1: 0.1429, 2: 0.2222}
LEGENDARY_FEATURED = 0.3 / 0.31
_WSET, _WR, _FB, _FEAT = ({}, {}, {}, {})

def _medals(rarity):
    try:
        c = md('m_config')[0]
        n = {1: c[48], 2: c[49], 3: c[50], 101: c[124]}.get(rarity)
        return n or MEDALS.get(rarity, 1)
    except Exception:
        return MEDALS.get(rarity, 1)

def _lot_rows(table, lot, *groups):
    for g in groups:
        if g:
            rows = [r for r in md(table) if r[0] == g and r[1] == lot]
            if rows:
                return rows
    return []

def _wset(sid):
    if not _WSET:
        for r in md('m_auto_gacha_weapon_probability'):
            _WSET.setdefault(r[0], []).append(r)
    return _WSET.get(sid, []) if sid else []

def _wr():
    if not _WR:
        for r in md('m_weapon_rarity'):
            _WR.setdefault(r[2], set()).add(r[1])
    return _WR

GACHA_TICKET_EXCHANGE = 106    # m_gacha col 1: the draw-ticket banners, whose lists hold event and limited weapons

def _ticket_only_sets():
    """Weapon lists that only draw-ticket banners use (m_gacha col 1 = 106). Festival of Gratitude's exchange ticket
    lists hold event-exchange weapons (Holiday Cheer, Oven Mitts, ...) and one-banner limited ones (Erdrick's Sword)."""
    gachas = mdx('m_gacha', 0)
    groups = mdx('m_gacha_step_group', 0)
    kinds = {}
    for g in gachas.values():
        for sid in (g[26], g[28]):
            if sid:
                kinds.setdefault(sid, set()).add(g[1])
    for s in md('m_gacha_step'):
        grp = groups.get((s[3],))
        g = gachas.get((grp[1],)) if grp else None
        for sid in (s[16], s[18]):
            if sid:
                kinds.setdefault(sid, set()).add(g[1] if g else 0)
    return {sid for sid, k in kinds.items() if k == {GACHA_TICKET_EXCHANGE}}

def _fb():
    """The pool for a banner whose own weapon list the game data no longer has: every weapon on the lists that do
    remain, by rarity, except the draw-ticket lists (their event and limited weapons were never on a normal banner)."""
    if not _FB:
        skip = _ticket_only_sets()
        for r in md('m_auto_gacha_weapon_probability'):
            if r[1] == GACHA_NORMAL and r[0] not in skip:
                _FB.setdefault(r[2], set()).add(r[3])
        for k in list(_FB):
            _FB[k] = sorted(_FB[k])
    return _FB

def _featured(gacha_id):
    if not _FEAT:
        rw = {r[0]: r for r in md('m_reward')}
        for r in sorted(md('m_auto_gacha_featured_reward'), key=lambda r: r[3]):
            x = rw.get(r[2])
            if r[1] == 1 and x and (x[1] == 5):
                _FEAT.setdefault(r[0], []).append(x[2])
        _FEAT.setdefault(None, [])
    return _FEAT.get(gacha_id, [])

def _pick_weapon(rarity, wrows, prows, featured):
    return _pick_weapon_square(rarity, wrows, prows, featured)

def _pick_weapon_square(rarity, wrows, prows, featured):
    if wrows:
        if rarity == 3 and prows:
            pool = [r for r in prows if r[2] == 3]
            if pool:
                return _roll(pool, 4)[3]
        pool = [r for r in wrows if r[2] == rarity and r[1] == GACHA_NORMAL] or [r for r in wrows if r[2] == rarity]
        if pool:
            return _roll(pool, 4)[3]
        # The banner's rates give this rarity but its list has none (the Festival of Gratitude Limit Break and
        # Crossover draws list 3★, 4★ and Legendary only, yet roll a 5★ 7.5% of the time): any of the banner's own
        # weapons that comes in this rarity, not the pool of every banner.
        if rarity != 101:
            own = sorted({r[3] for r in wrows if r[2] != 101} & _wr().get(rarity, set()))
            if own:
                return random.choice(own)
    F = [w for w in featured if w in _wr().get(rarity, ())]
    if F:
        if rarity == 3:
            return random.choice(F)
        if rarity == 101 and random.random() < LEGENDARY_FEATURED:
            return random.choice(F)
        if rarity in PICKUP_SHARE and random.random() < min(0.9, PICKUP_SHARE[rarity] * len(F)):
            return random.choice(F)
    pool = _fb().get(rarity) or _fb().get(1)
    return random.choice(pool)
_WISH = {}

def _wish_tables():
    if not _WISH:
        import collections
        _WISH['target'] = {(r[0], r[2]): r[3] for r in md('m_auto_gacha_wish_target_probability')}
        ch = collections.defaultdict(list)
        for r in md('m_gacha_wish_choice'):
            ch[r[1]].append(r)
        _WISH['choice'] = ch
        _WISH['wish'] = {r[0]: r for r in md('m_gacha_wish')}
    return _WISH

def _wish_picks(st, wish_id):
    W = _wish_tables()
    seen = set()
    while wish_id and wish_id not in seen and (not W['choice'].get(wish_id)):
        seen.add(wish_id)
        wish_id = (W['wish'].get(wish_id) or [0, 0])[1]
    out = []
    for r in sorted(W['choice'].get(wish_id, []), key=lambda r: r[2]):
        mine = st.get('user_gacha_wish_choice_list', r[0])
        w = mine.weapon_id if mine and mine.weapon_id else r[3]
        if w:
            out.append(w)
    return out

def _wish_hit(gacha_id, lot, rows):
    t = _wish_tables()['target'].get((gacha_id, lot), 0)
    p5 = sum((r[3] for r in rows if r[2] == 3))
    return t > 0 and p5 > 0 and (random.random() < min(1.0, t / p5))
_STAMP = {}

def _stamp_tables():
    if not _STAMP:
        import collections
        _STAMP['group'] = {r[0]: r for r in md('m_gacha_stamp_sheet_group')}
        rel = collections.defaultdict(dict)
        for r in md('m_gacha_stamp_sheet_group_rel'):
            rel[r[0]][r[1]] = r
        cr = collections.defaultdict(dict)
        for r in md('m_gacha_stamp_sheet_cell_rel'):
            cr[r[0]][r[1]] = r[2]
        _STAMP.update(rel=rel, cellrel=cr, cell={r[0]: r for r in md('m_gacha_stamp_sheet_cell')}, choice_group={(r[1], r[2], r[3]): r for r in md('m_gacha_stamp_sheet_cell_choice_group')}, choice={r[0]: r for r in md('m_gacha_stamp_sheet_cell_choice')}, lot=collections.defaultdict(list), direction=collections.defaultdict(list), reward={r[0]: r for r in md('m_reward')}, rs=collections.defaultdict(list))
        for r in md('m_auto_gacha_stamp_probability'):
            _STAMP['lot'][r[0]].append(r)
        for r in md('m_gacha_stamp_sheet_direction_lot'):
            _STAMP['direction'][r[2]].append(r[0])
        for r in md('m_reward_set_reward_rel'):
            _STAMP['rs'][r[1]].append(r)
    return _STAMP

def _set_weapon(set_id):
    S = _stamp_tables()
    for r in sorted(S['rs'].get(set_id, []), key=lambda r: r[3]):
        rw = S['reward'].get(r[2])
        if rw and rw[1] == 5:
            return rw[2]
    return None

def _stamp_advance(st, tx, gid, body, progress=None):
    S = _stamp_tables()
    grp = S['group'].get(gid)
    rels = S['rel'].get(gid)
    if not grp or not rels:
        return []
    row = st.get('user_gacha_stamp_sheet_group_list', gid) or st.new_row('user_gacha_stamp_sheet_group_list', gacha_stamp_sheet_group_id=gid, gacha_stamp_sheet_group_rel_seq=min(rels))
    if grp[1] and row.complete_sheet_count >= grp[1]:
        return []
    if progress is None:
        lot = S['lot'].get(grp[3]) or [[0, 1, 1]]
        progress = _roll(lot, 2)[1]
    tenth = []
    for _ in range(progress):
        rel = rels.get(row.gacha_stamp_sheet_group_rel_seq) or rels[min(rels)]
        sheet = rel[2]
        row.stamp_count += 1
        row.total_stamp_count += 1
        cell = S['cell'].get(S['cellrel'][sheet].get(row.stamp_count))
        if cell:
            kind = cell[1]
            if kind == 2 and cell[4]:
                grant_reward_set(st, tx, cell[4], 1, body.stamp_sheet_reward_info_list)
            elif kind == 1 and cell[2]:
                tenth.append(('lot', cell[2]))
            elif kind == 3:
                tenth.append(('lot', 2))
            elif kind == 5:
                w = _set_weapon(cell[4])
                if w:
                    tenth.append(('weapon', w))
            elif kind == 4:
                cg = S['choice_group'].get((gid, rel[1], row.stamp_count))
                if cg:
                    mine = st.get('user_gacha_stamp_sheet_cell_choice_group_list', cg[0])
                    ch = S['choice'].get(mine.gacha_stamp_sheet_cell_choice_id if mine else cg[4])
                    if ch:
                        w = _set_weapon(ch[3])
                        if w:
                            tenth.append(('weapon', w))
                        elif ch[3]:
                            grant_reward_set(st, tx, ch[3], 1, body.stamp_sheet_reward_info_list)
                    extra = S['choice'].get(cg[5])
                    if extra and extra[3] and (not _set_weapon(extra[3])):
                        grant_reward_set(st, tx, extra[3], 1, body.stamp_sheet_reward_info_list)
        if row.stamp_count >= max(S['cellrel'][sheet] or [12]):
            row.complete_sheet_count += 1
            row.stamp_count = 0
            row.gacha_stamp_sheet_group_rel_seq = rel[3] or rel[1]
            if grp[1] and row.complete_sheet_count >= grp[1]:
                break
    tx.upsert('user_gacha_stamp_sheet_group_list', row)
    body.stamp_sheet_progress_count = progress
    ids = S['direction'].get(progress) or S['direction'].get(max(S['direction'] or [1]), [])
    if ids:
        body.gacha_stamp_sheet_direction_lot_id = random.choice(ids)
    return tenth

def gacha_stamp_choice(st, req, ctx):
    q = req.post_pvt_gacha_stamp_sheet_choice
    body = P.Command_ApiNetwork_Api_PostPvtGachaStampSheetChoiceResponse()
    with st.transaction('gacha/stamp/sheet/choice') as tx:
        for c in q.gacha_stamp_sheet_choice_info_list:
            r = st.get('user_gacha_stamp_sheet_cell_choice_group_list', c.gacha_stamp_sheet_cell_choice_group_id) or st.new_row('user_gacha_stamp_sheet_cell_choice_group_list', gacha_stamp_sheet_cell_choice_group_id=c.gacha_stamp_sheet_cell_choice_group_id)
            r.gacha_stamp_sheet_cell_choice_id = c.gacha_stamp_sheet_cell_choice_id
            tx.upsert('user_gacha_stamp_sheet_cell_choice_group_list', r)
    return _resp(tx, 'post_pvt_gacha_stamp_sheet_choice', body)
HANDLERS['/api/pvt/gacha/stamp/sheet/choice'] = gacha_stamp_choice

def gacha_draw(st, req, ctx):
    q = req.post_pvt_gacha_draw
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtGachaDrawResponse()
    with st.transaction('gacha/draw') as tx:
        step = mdx('m_gacha_step', 0).get((q.gacha_step_id,))
        if not step:
            raise ValueError(f'unknown gacha step {q.gacha_step_id}')
        grp = mdx('m_gacha_step_group', 0).get((step[3],))
        gacha_id = grp[1] if grp else 0
        gacha = mdx('m_gacha', 0).get((gacha_id,))
        draws = q.draw_count or step[6]
        ctype = step[4]
        execs = draws if ctype == 0 else max(1, draws // max(1, step[6]))
        cost = (1 if ctype == 0 else step[5]) * execs
        # A crystal banner that also takes a draw ticket (m_gacha col 12 = the ticket item, col 13 = tickets per draw;
        # Premium Draw Tickets on the Limit Break draws). The request names only the step, so tickets are spent first,
        # as the game offers them, and crystals only when the player holds too few. Charging crystals here left the
        # tickets in place, and with no crystals to take the draws were free.
        tix = gacha[12] if gacha and ctype != 0 and len(gacha) > 13 and gacha[12] else 0
        need = (gacha[13] or 1) * draws if tix else 0
        held = st.get('user_item_list', tix) if tix else None
        if need and held is not None and held.count >= need:
            add_item(st, tx, tix, -need)
            cost = 0
        # Crystals: blue (stone 2, free) first, then red (stone 1, paid), as every crystal banner's notes say. The
        # exception is the "Red Crystals only" banners: cost type 1 with step col 11 = 0 (all 19 whose notes say so;
        # cost type 1 steps with col 11 = 2 include the ones whose notes say blue first). Cost types 2 and 5 took only
        # blue before, so a player with only red crystals drew for free.
        if ctype in (1, 2, 4, 5) and cost:
            red_only = ctype == 1 and not (len(step) > 11 and step[11])
            free = None if red_only else next((s for s in st.other_info.user_stone_list if s.stone_id == 2), None)
            n = cost
            if free:
                use = min(free.count, n)
                free.count -= use
                n -= use
            if n:
                give_stone(st, tx, 1, -n)
        elif ctype == 0 and cost and gacha and gacha[12]:
            add_item(st, tx, gacha[12], -cost)
        groups = (step[15], gacha[25] if gacha else 0)
        rar_normal = _lot_rows('m_auto_gacha_rarity_lot_probability', GACHA_NORMAL, *groups)
        if not rar_normal:
            raise ValueError(f'gacha step {q.gacha_step_id}: no rarity lot (groups {groups})')
        fixed_lot = FIX2LOT.get(step[7])
        rar_fixed = _lot_rows('m_auto_gacha_rarity_lot_probability', fixed_lot, *groups) if fixed_lot else []
        wset = step[16] or (gacha[26] if gacha else 0)
        wrows = _wset(wset)
        prows = _wset(step[18] or (gacha[28] if gacha else 0))
        featured = _featured(gacha_id)
        if not wrows:
            print(f'gacha step {q.gacha_step_id}: weapon set {wset} missing (expired banner) - {len(featured)} featured weapons + rarity-matched pool')
        best = 0
        per = max(1, step[6])
        stamp_gid = gacha[24] if gacha and grp and grp[5] else 0
        tenth = []
        picks = []
        wishes = _wish_picks(st, gacha[32]) if gacha and len(gacha) > 32 and gacha[32] else []
        for i in range(draws):
            if stamp_gid and per >= 10 and (i % per == 0):
                tenth = _stamp_advance(st, tx, stamp_gid, body)
            slot10 = per >= 10 and i % per == per - 1
            back = per - 1 - i % per
            g = tenth[back] if per >= 10 and back < len(tenth) else None
            fixed = bool(rar_fixed) and slot10
            rows = rar_fixed if fixed else rar_normal
            lot = fixed_lot if fixed else GACHA_NORMAL
            if g and g[0] == 'lot':
                lot = FIX2LOT.get(g[1], 3)
                rows = _lot_rows('m_auto_gacha_rarity_lot_probability', lot, *groups) or rows
            rarity = _roll(rows, 3)[2]
            wid = _pick_weapon(rarity, wrows, prows, featured)
            if rarity == 3 and wishes and (not (g and g[0] == 'weapon')) and _wish_hit(gacha_id, lot, rows):
                wid = random.choice(wishes)
            if g and g[0] == 'weapon':
                wid = g[1]
                rarity = 3 if wid in _wr().get(3, ()) else 101
            picks.append((wid, rarity))
            best = max(best, rarity)
        _grant_batch(st, tx, picks, body.gacha_result_list, now)
        if step[8]:
            grant_reward_set(st, tx, step[8], execs, body.bonus_reward_info_list)
        sg = st.get('user_gacha_step_group_list', step[3]) or st.new_row('user_gacha_step_group_list', gacha_step_group_id=step[3])
        sg.total_exec_count += execs
        sg.exec_count += execs
        sg.last_exec_datetime = now
        sg.total_draw_count += draws
        mission_event(st, tx, 'ProgressGachaDraw', draws, gacha_id)
        tx.upsert('user_gacha_step_group_list', sg)
        body.gacha_direction_lot_id = _roll([[k, v] for k, v in DIRECTION_SSR.items()], 1)[0] if best >= 3 else 1005
    return _resp(tx, 'post_pvt_gacha_draw', body)
HANDLERS['/api/pvt/gacha/draw'] = gacha_draw

DUNGEON_START, DUNGEON_END = (1, 2)

def _another_bid(aid, stage):
    rows = [r for r in md('m_another_battle_rel') if r[0] == aid]
    r = next((x for x in rows if x[1] == stage), None) or (rows[0] if rows else None)
    return r[2] if r else aid

DUNGEON_KEY = 5                 # m_item 5 "Dungeon Key" (holds 6); item 9 is the Dungeon Key Voucher, traded for one
RECOVER_KEY = P.Command_Enums_RecoveryType.Value('Command_Enums_RecoveryType_AnotherDungeonKey')

def dungeon_keys_add(st, tx, n):
    """Add up to n Dungeon Keys, never past what the player can hold (the client refuses more; nothing goes to the
    gift box). -> keys added."""
    row = st.get('user_item_list', DUNGEON_KEY)
    n = min(n, max(0, item_cap(DUNGEON_KEY) - (row.count if row else 0)))
    if n > 0:
        add_item(st, tx, DUNGEON_KEY, n)
    return max(0, n)

def dungeon_key_grant(st, tx, t):
    """m_config AnotherDungeonKeyGrantCountPerReset (col 25) keys once in each period of AnotherDungeonKeyResetId
    (col 24): Square's 3 keys every week. user_time AnotherDungeonKey is when the last grant was."""
    cfg = md('m_config')[0]
    row = st.get('user_time_list', UT.Command_Enums_UserTimeType_AnotherDungeonKey)
    if row is not None and row.datetime and reset_period(cfg[24], row.datetime) == reset_period(cfg[24], t):
        return
    dungeon_keys_add(st, tx, cfg[25])
    _set_time(st, tx, UT.Command_Enums_UserTimeType_AnotherDungeonKey, t)

def dungeon_another_start(st, req, ctx):
    q = req.post_pvt_dungeon_another_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtDungeonAnotherStartResponse()
    with st.transaction('dungeon/another/start') as tx:
        dungeon_key_grant(st, tx, now)
        ad = mdx('m_another_dungeon', 0).get((q.dungeon_id,))
        if ad and ad[6]:            # ConsumptionCount: the Dungeon Keys a run takes (Crisis 1, Criterion 0)
            add_item(st, tx, DUNGEON_KEY, -ad[6])
        de = st.get('user_dungeon_entry_list', q.dungeon_type) or st.new_row('user_dungeon_entry_list', dungeon_type=q.dungeon_type)
        de.party_id = q.party_id
        de.entry_count += 1
        de.dungeon_id = q.dungeon_id
        de.content_id = q.dungeon_id
        de.dungeon_progress_type = DUNGEON_START
        de.dungeon_result_type = 0
        de.chaser_encount_count = 0
        tx.upsert('user_dungeon_entry_list', de)
        ae = st.new_row('user_another_dungeon_entry_list', dungeon_type=q.dungeon_type)
        tx.upsert('user_another_dungeon_entry_list', ae)
        party = st.get('user_party_list', q.party_id)
        if party:
            tx.touch('user_party_list', party)
        for m in st['user_party_member_list'].values():
            if m.party_member_id // 10 == q.party_id:
                tx.touch('user_party_member_list', m)
        body.entry_count = de.entry_count
        mission_event(st, tx, 'SetDungeonAnotherEntry', 1, q.dungeon_type)
    return _resp(tx, 'post_pvt_dungeon_another_start', body)

def another_battle_start(st, req, ctx):
    q = req.post_pvt_another_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtAnotherBattleStartResponse()
    with st.transaction('another/battle/start') as tx:
        de = st.get('user_dungeon_entry_list', q.dungeon_type)
        bid = _another_bid(q.another_battle_id, q.boss_enhance_stage)
        _start_common(st, tx, body, bid, q.another_battle_id, 'Command_Enums_BattleType_Another', de.party_id if de else 0, 0, now, ctx=ctx)
    return _resp(tx, 'post_pvt_another_battle_start', body)

_BATTLE_GROUPS = {}

def _battle_groups(battle_id):
    """m_battle_group_rel [BattleGroupId, BattleId]: the groups a battle belongs to ("Attempt ... N time(s)" missions)."""
    if not _BATTLE_GROUPS:
        for g, b in md('m_battle_group_rel'):
            _BATTLE_GROUPS.setdefault(b, []).append(g)
        _BATTLE_GROUPS.setdefault(0, [])
    return _BATTLE_GROUPS.get(battle_id, ())

def another_battle_end(st, req, ctx):
    q = req.post_pvt_another_battle_end
    body = P.Command_ApiNetwork_Api_PostPvtAnotherBattleEndResponse()
    with st.transaction('another/battle/end') as tx:
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        stage = ae.boss_enhance_stage if ae else 0
        bid = _another_bid(q.another_battle_id, stage)
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, kinds=(('ProgressAnotherClear', 0),))
        if q.battle_result_type != RETIRE:
            for g in _battle_groups(bid):
                mission_event(st, tx, 'ProgressBattleGroupChallenge', 1, g)
        if win:
            ab = mdx('m_another_battle', 0).get((q.another_battle_id,))
            row = st.get('user_another_battle_list', q.another_battle_id, stage) or st.new_row('user_another_battle_list', another_battle_id=q.another_battle_id, boss_enhance_stage=stage)
            row.win_count += 1
            row.is_done = True
            tx.upsert('user_another_battle_list', row)
            if ab:
                if ab[2]:
                    grant_reward_set(st, tx, ab[2], 1, body.battle_result.reward_infos)
                if ab[3]:
                    grant_reward_set(st, tx, ab[3], 1, body.after_battle_drop_reward_infos)
        body.win_score = q.battle_input.total_damage if win else 0
    return _resp(tx, 'post_pvt_another_battle_end', body)

def dungeon_another_treasure(st, req, ctx):
    q = req.post_pvt_dungeon_another_treasure
    body = P.Command_ApiNetwork_Api_PostPvtDungeonAnotherTreasureResponse()
    with st.transaction('dungeon/another/treasure') as tx:
        t = mdx('m_dungeon_treasure', 0).get((q.dungeon_treasure_id,))
        row = st.get('user_dungeon_treasure_list', q.dungeon_treasure_id)
        if t and (not (row and t[5] and row.entry_count)):
            if t[2]:
                grant_reward_set(st, tx, t[2], 1, body.reward_infos)
        row = row or st.new_row('user_dungeon_treasure_list', dungeon_treasure_id=q.dungeon_treasure_id)
        row.entry_count = q.entry_count
        tx.upsert('user_dungeon_treasure_list', row)
    return _resp(tx, 'post_pvt_dungeon_another_treasure', body)

def dungeon_another_end(st, req, ctx):
    q = req.post_pvt_dungeon_another_end
    body = P.Command_ApiNetwork_Api_PostPvtDungeonAnotherEndResponse()
    with st.transaction('dungeon/another/end') as tx:
        de = st.get('user_dungeon_entry_list', q.dungeon_type)
        if de:
            de.dungeon_progress_type = DUNGEON_END
            tx.touch('user_dungeon_entry_list', de)
        ad = mdx('m_another_dungeon', 0).get((q.dungeon_id,))
        row = st.get('user_another_dungeon_list', q.dungeon_id) or st.new_row('user_another_dungeon_list', another_dungeon_id=q.dungeon_id)
        cleared = _another_cleared(st, q.dungeon_id, q.dungeon_type)
        first = cleared and row.win_count == 0
        if cleared:
            row.win_count += 1
            _pr_add(st, tx, 'AnotherDungeonClearCount', 1)
            # "Clear Crisis / Criterion Dungeon (X) N time(s)": col 5 = dungeon type, col 6 = dungeon id (0 = any)
            mission_event(st, tx, 'ProgressAnotherClear', 1, q.dungeon_type, sub=q.dungeon_id)
        tx.upsert('user_another_dungeon_list', row)
        if first and ad and ad[7]:
            obt = 'CrisisFirstClear' if ad[6] == 1 else 'AnotherFirstClear'
            _first_rewards(st, tx, ctx, ad[7], 1, body.first_clear_reward_infos,
                           P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_' + obt), BATTLE_FIRST_MSG)
    return _resp(tx, 'post_pvt_dungeon_another_end', body)

def _another_cleared(st, did, dt):
    """True when this run of the Another dungeon won its last boss (m_another_battle col 5 = 3). A given-up run also
    ends with dungeon/another/end, and is not a clear. A dungeon with no last boss row counts every end as a clear."""
    lb = _crk_last_boss(did)
    if lb is None:
        return True
    ae = st.get('user_another_dungeon_entry_list', dt)
    return ae is not None and bool((ae.another_battle_idx_flag >> lb) & 1)

MTYPE = {v.name.replace('Command_Enums_MissionType_', ''): v.number for v in P.Command_Enums_MissionType.DESCRIPTOR.values}
_ABS_EXTRA = {MTYPE.get('ProgressHighwindPartsLevel'), MTYPE.get('ProgressHighwindPartsLevelAll'), MTYPE.get('ProgressDamageChallengeBattleScore')} - {None}
_MT_INDEX = {}

def _mt_index():
    if not _MT_INDEX:
        for r in md('m_mission'):
            _MT_INDEX.setdefault(r[4], []).append(r)
        _MT_INDEX['groups'] = {r[0]: r for r in md('m_mission_group')}
        _MT_INDEX['by_id'] = {r[0]: r for r in md('m_mission')}
    return _MT_INDEX
_SET_GROUP = {}

def _set_group(set_id):
    if not _SET_GROUP:
        _SET_GROUP.update({r[0]: r[1] for r in md('m_mission_set')})
    return _SET_GROUP.get(set_id, set_id)

def _group_open(gid, t):
    g = _mt_index()['groups'].get(gid)
    if not g or (g[8] and g[8] <= t):
        return False
    if g[1] in (2, 3) and g[6]:
        return _series_head(g[6], g[1], t) == gid
    return True

def _series_head(series, kind, t):
    k = (series, kind, t // 3600000)
    if k not in _SERIES:
        _SERIES.clear()
        _SERIES[k] = max((o[0] for o in _mt_index()['groups'].values() if o[6] == series and o[1] == kind and (not (o[8] and o[8] <= t))), default=0)
    return _SERIES[k]
_SERIES = {}
_MSTEPS = {}

def _steps_of(mid):
    if not _MSTEPS:
        for r in md('m_mission_progress'):
            _MSTEPS.setdefault(r[0], []).append(r[1])
    return _MSTEPS.get(mid, ())
_COND = {}

def _cond_rows(cs):
    if not _COND:
        rel = {}
        for r in md('m_condition_set_rel'):
            rel.setdefault(r[0], []).append(r[1])
        _COND['rel'] = rel
        _COND['c'] = {r[0]: r for r in md('m_condition')}
    return [_COND['c'][i] for i in _COND['rel'].get(cs, ()) if i in _COND['c']]

def _mission_window_ok(m, t, strict):
    if not m[12]:
        return True
    rows = _cond_rows(m[12])
    if not rows:
        return not strict
    for c in rows:
        if c[1] != 2:
            if strict:
                return False
            continue
        if c[5] and t < c[5] or (c[6] and t > c[6]):
            return False
        if c[7] == 3 and c[8] == 24 and (not (t // 3600000 - c[10]) % 24 < c[9]):
            return False
    return True
GUIDE_GROUP_KIND = 10
_GUIDE_CHAIN = {}
_MTNAME = {}

def _is_guide(gid):
    g = _mt_index()['groups'].get(gid)
    return bool(g) and g[1] == GUIDE_GROUP_KIND

def _guide_chain(gid, t):
    k = (gid, t // 3600000)
    if k not in _GUIDE_CHAIN:
        for old in [x for x in _GUIDE_CHAIN if x[0] == gid]:
            del _GUIDE_CHAIN[old]
        _GUIDE_CHAIN[k] = sorted((m for m in _mt_index()['by_id'].values() if _set_group(m[1]) == gid and _mission_window_ok(m, t, True)), key=lambda m: m[10])
    return _GUIDE_CHAIN[k]
_FLAG_MASK = (1 << 64) - 1

def _flag_or(v, bit):
    """v with bit (taken mod 64) set, kept in the signed 64-bit range the idx_flags fields hold: bit 63 is the sign
    bit, so a plain `v | 1 << 63` is out of range and failed the whole request (Battle Flag's Zack mission, #43)."""
    u = v & _FLAG_MASK | 1 << bit % 64
    return u - (1 << 64) if u >= 1 << 63 else u

def _set_flag(s, pre, idx):
    return bool((getattr(s, f'{pre}_mission_set_idx_flags{idx // 64}') & _FLAG_MASK) >> idx % 64 & 1)

def _set_flag_on(s, pre, idx):
    f = f'{pre}_mission_set_idx_flags{idx // 64}'
    setattr(s, f, _flag_or(getattr(s, f), idx))

def _mission_received(st, m):
    if m[3] == 3:
        s = st.get('user_mission_set_list', m[1])
        return s is not None and _set_flag(s, 'received', m[2])
    row = st.get('user_mission_list', m[0])
    steps = _steps_of(m[0])
    return row is not None and row.received_progress_count >= (max(steps) if steps else 1)

def _guide_current(st, gid, t):
    for m in _guide_chain(gid, t):
        if not _mission_received(st, m):
            return m
    return None

def _tx_announce(tx, mid, step):
    a = getattr(tx, 'achieved', None)
    if a is None:
        a = tx.achieved = {}
    a[mid] = step

def _set_mission_achieve(st, tx, m, t):
    s = st.get('user_mission_set_list', m[1]) or st.new_row('user_mission_set_list', mission_set_id=m[1])
    if _set_flag(s, 'achieved', m[2]):
        return False
    _set_flag_on(s, 'achieved', m[2])
    gid = _set_group(m[1])
    cur = _guide_current(st, gid, t) if _is_guide(gid) else m
    if cur is not None and cur[0] == m[0]:
        _set_flag_on(s, 'notified', m[2])
        _tx_announce(tx, m[0], 0)
    tx.upsert('user_mission_set_list', s)
    return True

def _set_mission_receive(st, tx, m, body, pts):
    s = st.get('user_mission_set_list', m[1]) or st.new_row('user_mission_set_list', mission_set_id=m[1])
    if _set_flag(s, 'received', m[2]):
        return False
    _set_flag_on(s, 'achieved', m[2])
    _set_flag_on(s, 'received', m[2])
    steps = _mission_steps(m[0])
    rs = (steps[0][2] if steps else 0) or m[9]
    bp = (steps[0][3] if steps else 0) or m[11]
    if rs:
        grant_reward_set(st, tx, rs, 1, body.reward_info_list)
    bg = _bonus_group(m[0]) if bp else 0
    if bg:
        pts[bg] = pts.get(bg, 0) + bp
    tx.upsert('user_mission_set_list', s)
    return True

def _guide_advance(st, tx, gid, t):
    m = _guide_current(st, gid, t)
    if m is None or m[3] == 3:
        return
    row = st.get('user_mission_list', m[0])
    if row is None:
        return
    hit = [x for x in _steps_of(m[0]) if row.received_progress_count < x <= row.progress_count]
    if not hit or row.notified_progress_count >= max(hit):
        return
    row.notified_progress_count = max(hit)
    tx.upsert('user_mission_list', row)
    _tx_announce(tx, m[0], max(hit))

def _announce_step(st, tx, m, row, old, t):
    """The blue popup at the top of the screen (other_info.mission_achieved_list) for a step the mission just reached:
    any mission's, but a guide group's only for its current mission. Only the reply of the transaction that moved it
    carries the popup, so login catch-ups, which run in transactions of their own, stay silent."""
    gid = _set_group(m[1])
    if _is_guide(gid):
        cur = _guide_current(st, gid, t)
        if cur is None or cur[0] != m[0]:
            return
    hit = [x for x in _steps_of(m[0]) if old < x <= row.progress_count]
    if not hit or row.notified_progress_count >= max(hit):
        return
    row.notified_progress_count = max(hit)
    _tx_announce(tx, m[0], max(hit))

def _guide_heal(st):
    t = now_ms()
    n = 0
    if not _MTNAME:
        _MTNAME.update({v: k for k, v in MTYPE.items()})
    with st.transaction('title: guide set missions already met') as tx:
        for gid, g in _mt_index()['groups'].items():
            if g[1] != GUIDE_GROUP_KIND or not _group_open(gid, t):
                continue
            for m in _guide_chain(gid, t):
                if m[3] != 3:
                    continue
                tn = _MTNAME.get(m[4])
                ok = False
                if tn == 'SetStoryReadEpisode':
                    ep = st.get('user_episode_list', m[5])
                    ok = ep is not None and bool(ep.story_party_mode_clear_datetime or ep.free_party_mode_clear_datetime)
                elif tn == 'SetUserSetting':
                    r = st.get('user_setting_list', m[5])
                    ok = r is not None and r.value == 'true'
                elif tn == 'SetGiftReceive':
                    ok = _gift_opened_here(st)
                if ok and _set_mission_achieve(st, tx, m, t):
                    n += 1
    if n:
        print(f'title heal: {st.user_id}: {n} guide set mission(s) already met - marked achieved')
    return n

def _story_read_heal(st):
    """SetStoryReadEpisode set missions outside the guide groups (Omnislash's, 700103001: read episode 110126) are
    only raised when the episode is read, so an episode read before they were counted, or on an imported account,
    left them at "Attempt". At login each open one whose episode already has a clear time is marked achieved, as
    mission_event would have done. Guide groups are _guide_heal's."""
    t = now_ms()
    n = 0
    with st.transaction('title: story-read set missions already met') as tx:
        for m in _mt_index().get(MTYPE.get('SetStoryReadEpisode', -1), []):
            if m[3] != 3 or not m[5]:
                continue
            gid = _set_group(m[1])
            if _is_guide(gid) or not _group_open(gid, t) or not _mission_window_ok(m, t, False):
                continue
            ep = st.get('user_episode_list', m[5])
            if ep is None or not (ep.story_party_mode_clear_datetime or ep.free_party_mode_clear_datetime):
                continue
            if _set_mission_achieve(st, tx, m, t):
                n += 1
    if n:
        print(f'story read heal: {st.user_id}: {n} set mission(s) for episodes already read - marked achieved')
    return n

def _gift_opened_here(st):
    try:
        p = _hist_path(st)
        mp = os.path.join(st.folder, 'info.json') if st.folder else None
        if not p or not os.path.isfile(p) or (not mp) or (not os.path.isfile(mp)):
            return False
        born = float(json.load(open(mp)).get('first_seen') or 0) * 1000
        return any((int(h.get('archived_datetime') or 0) >= born for h in json.load(open(p))))
    except Exception:
        return False

def mission_event(st, tx, mtype, delta=1, param=0, reached=None, sub=None):
    if isinstance(mtype, str):
        mtype = MTYPE.get(mtype, 0)
    rows = _mt_index().get(mtype)
    if not rows or not delta:
        return
    absolute = 'Absolute' in next((n for n, v in MTYPE.items() if v == mtype), '') or mtype in _ABS_EXTRA
    t = now_ms()
    for m in rows:
        if m[5] and m[5] != param:
            continue
        if sub is not None and m[6] and m[6] != sub:
            continue
        if reached is not None and m[8] and (m[8] > reached):
            continue
        gid = _set_group(m[1])
        if not _group_open(gid, t):
            continue
        guide = _is_guide(gid)
        if guide and (not _mission_window_ok(m, t, True)):
            continue
        if m[3] == 3:
            if guide or _mission_window_ok(m, t, False):
                _set_mission_achieve(st, tx, m, t)
            continue
        since = _period_start(_mt_index()['groups'][gid], t)
        if since is not None:
            _open_period(st, tx, gid, since)
        row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
        new = max(row.progress_count, delta) if absolute else row.progress_count + delta
        if new == row.progress_count:
            continue
        old = row.progress_count
        row.progress_count = new
        _announce_step(st, tx, m, row, old, t)
        tx.upsert('user_mission_list', row)

def mission_periodic_reset(st, tx, day_changed, week_changed):
    kinds = {2} if day_changed else set()
    if week_changed:
        kinds.add(3)
    if not kinds:
        return
    groups = _mt_index()['groups']
    reset = set()
    stamp = set()
    due_resets = {r[0] for r in md('m_reset') if r[2] == 4 and (r[3] == 1 and day_changed or (r[3] == 7 and week_changed)) or (r[2] == 5 and r[3] == 1 and week_changed)}
    for row in list(st['user_mission_list'].values()):
        m = _mt_index()['by_id'].get(row.mission_id)
        g = groups.get(_set_group(m[1])) if m else None
        if not g or not (g[1] in kinds or (len(g) > 9 and g[9] in due_resets)):
            continue
        stamp.add(g[0])
        if row.progress_count or row.received_progress_count or row.notified_progress_count:
            row.progress_count = row.received_progress_count = row.notified_progress_count = 0
            tx.upsert('user_mission_list', row)
        if g[2]:
            reset.add(g[2])
    for srow in list(st['user_mission_set_list'].values()):
        g = groups.get(_set_group(srow.mission_set_id))
        if not g or not (g[1] in kinds or (len(g) > 9 and g[9] in due_resets)):
            continue
        stamp.add(g[0])
        fl = [f.name for f in srow.DESCRIPTOR.fields if 'flags' in f.name and getattr(srow, f.name)]
        if fl:
            for f in fl:
                srow.ClearField(f)
            tx.upsert('user_mission_set_list', srow)
    for bg in reset:
        gr = st.get('user_mission_group_list', bg)
        if gr and (gr.bonus_point or gr.received_mission_bonus_idx_flags0):
            gr.bonus_point = 0
            for f in gr.DESCRIPTOR.fields:
                if 'flags' in f.name:
                    gr.ClearField(f.name)
            gr.last_reset_datetime = now_ms()
            tx.upsert('user_mission_group_list', gr)
    _stamp_groups(st, tx, stamp)

def _stamp_groups(st, tx, gids):
    """The game shows a daily or weekly group's progress only when the group's last reset is after the latest reset
    time (seen 10-11: season pass 16's "win 3 battles" read 0/3 at 14 wins, its group having no reset time at all)."""
    t = now_ms()
    for gid in gids:
        gr = st.get('user_mission_group_list', gid) or st.new_row('user_mission_group_list', mission_group_id=gid)
        if gr.last_reset_datetime != t:
            gr.last_reset_datetime = t
            tx.upsert('user_mission_group_list', gr)

def _period_start(g, t):
    """When the current day or week began for a daily or weekly mission group (kind 2 or 3, or a group whose reset,
    column 9, rolls daily or weekly, like the event daily groups of kind 11), else None."""
    gd = game_day(t)
    if not _PERIOD_RESETS:
        rs = md('m_reset')
        _PERIOD_RESETS['daily'] = {r[0] for r in rs if r[2] == 4 and r[3] == 1}
        _PERIOD_RESETS['weekly'] = {r[0] for r in rs if r[2] == 4 and r[3] == 7 or (r[2] == 5 and r[3] == 1)}
    r9 = g[9] if len(g) > 9 else 0
    if g[1] == 2 or r9 in _PERIOD_RESETS['daily']:
        return gd * DAY_MS + RESET_OFFSET_MS
    if g[1] == 3 or r9 in _PERIOD_RESETS['weekly']:
        return ((gd + 3) // 7 * 7 - 3) * DAY_MS + RESET_OFFSET_MS
    return None
_PERIOD_RESETS = {}

def _open_period(st, tx, gid, since):
    """First progress in a daily or weekly group this period. A group the periodic reset never stamped (one with no
    mission rows at the last reset, such as an event's daily missions the first time they move) is shown at 0 by the
    game, and _mission_reset_heal would clear its progress at the next login. So any progress left from an earlier
    period is cleared now and the group stamped, as a reset does, before the new progress is added."""
    gr = st.get('user_mission_group_list', gid)
    if gr is not None and gr.last_reset_datetime >= since:
        return
    for row in list(st['user_mission_list'].values()):
        if not (row.progress_count or row.received_progress_count or row.notified_progress_count):
            continue
        m = _mt_index()['by_id'].get(row.mission_id)
        if m and _set_group(m[1]) == gid:
            row.progress_count = row.received_progress_count = row.notified_progress_count = 0
            tx.upsert('user_mission_list', row)
    _stamp_groups(st, tx, (gid,))

def _mission_reset_heal(st):
    """Daily and weekly groups whose reset time is older than the latest reset (or missing) while their missions hold
    progress: that progress is from before the reset, so it is cleared and the group stamped, as a reset does."""
    t = now_ms()
    groups = _mt_index()['groups']
    fix = {}
    for row in st['user_mission_list'].values():
        if not (row.progress_count or row.received_progress_count or row.notified_progress_count):
            continue
        m = _mt_index()['by_id'].get(row.mission_id)
        g = groups.get(_set_group(m[1])) if m else None
        if not g:
            continue
        since = _period_start(g, t)
        gr = st.get('user_mission_group_list', g[0])
        if since is not None and (gr is None or gr.last_reset_datetime < since):
            fix.setdefault(g[0], []).append(row)
    if not fix:
        return 0
    with st.transaction('missions: daily and weekly groups never reset') as tx:
        for rows in fix.values():
            for row in rows:
                row.progress_count = row.received_progress_count = row.notified_progress_count = 0
                tx.upsert('user_mission_list', row)
        _stamp_groups(st, tx, fix)
    print(f'mission reset heal: {st.user_id} {len(fix)} group(s) reset')
    return len(fix)

def mission_progress(st, req, ctx):
    q = req.post_pvt_mission_progress
    with st.transaction('mission/progress') as tx:
        mission_event(st, tx, q.mission_type, q.count or 1, q.target_id1)
    return _resp(tx, 'post_pvt_mission_progress')
HANDLERS['/api/pvt/mission/progress'] = mission_progress

def _mission_steps(mid):
    return sorted((r for r in md('m_mission_progress') if r[0] == mid), key=lambda r: r[1])

def _bonus_group(mid):
    m = mdx('m_mission', 0).get((mid,))
    g = mdx('m_mission_group', 0).get((_set_group(m[1]),)) if m else None
    return g[2] if g else 0
_STEPS_IDX = {}

def mission_receive(st, req, ctx):
    q = req.post_pvt_mission_receive
    body = P.Command_ApiNetwork_Api_PostPvtMissionReceiveResponse()
    with st.transaction('mission/receive') as tx:
        pts = {}
        cleared = 0
        got = set()
        ids = list(dict.fromkeys(q.mission_id_list))
        for mid in ids:
            m0 = _mt_index()['by_id'].get(mid)
            if m0 is not None and m0[3] == 3:
                if _set_mission_receive(st, tx, m0, body, pts):
                    cleared += 1
                    got.add(mid)
                continue
            row = st.get('user_mission_list', mid) or st.new_row('user_mission_list', mission_id=mid)
            m = mdx('m_mission', 0).get((mid,))
            steps = _mission_steps(mid)
            if not steps and m:
                steps = [[mid, max(1, row.progress_count), 0, 0, False, 0]]
            steps = [[r[0], r[1], r[2] or (m[9] if m else 0), r[3] or (m[11] if m else 0)] for r in steps]
            due = [r for r in steps if row.received_progress_count < r[1] <= row.progress_count]
            if not due and steps:
                nxt = next((r for r in steps if r[1] > row.received_progress_count), steps[-1])
                due = [nxt]
                row.progress_count = max(row.progress_count, nxt[1])
            if not due:
                continue
            if q.is_single and mid == ids[0]:
                due = due[:1]
            for r in due:
                if r[2]:
                    grant_reward_set(st, tx, r[2], 1, body.reward_info_list)
                bg = _bonus_group(mid) if r[3] else 0
                if bg:
                    pts[bg] = pts.get(bg, 0) + r[3]
            row.received_progress_count = row.notified_progress_count = due[-1][1]
            tx.upsert('user_mission_list', row)
            cleared += 1
            got.add(mid)
        for bg, add in pts.items():
            g = st.get('user_mission_group_list', bg) or st.new_row('user_mission_group_list', mission_group_id=bg)
            g.bonus_point += add
            for b in sorted((r for r in md('m_mission_bonus') if r[1] == bg), key=lambda r: r[2]):
                if b[4] > g.bonus_point:
                    continue
                fname = f'received_mission_bonus_idx_flags{b[2] // 64}'
                bit = 1 << b[2] % 64
                if getattr(g, fname) & bit:
                    continue
                setattr(g, fname, _flag_or(getattr(g, fname), b[2]))
                grant_reward_set(st, tx, b[6], 1, body.reward_bonus_info_list)
                body.received_mission_bonus_id_list.append(b[0])
            tx.upsert('user_mission_group_list', g)
        if GUIDE_FIRST_MISSION in got:
            g = st.get('user_tutorial_step_list', TUT_GUIDE)
            if g is not None and 0 < g.current_step < 2:
                g.current_step = 2
                tx.upsert('user_tutorial_step_list', g)
        tnow = now_ms()
        for ggid in {_set_group(_mt_index()['by_id'][x][1]) for x in got if x in _mt_index()['by_id']}:
            if _is_guide(ggid):
                _guide_advance(st, tx, ggid, tnow)
    return _resp(tx, 'post_pvt_mission_receive', body)
GUIDE_FIRST_MISSION = 1000001001
HANDLERS.update({'/api/pvt/dungeon/another/start': dungeon_another_start, '/api/pvt/another/battle/start': another_battle_start, '/api/pvt/another/battle/end': another_battle_end, '/api/pvt/dungeon/another/treasure': dungeon_another_treasure, '/api/pvt/dungeon/another/end': dungeon_another_end, '/api/pvt/mission/receive': mission_receive})

from google.protobuf.json_format import MessageToDict as _M2D, ParseDict as _PD
GIFT_PAGE = 300
GIFT_NO_EXPIRE = 32503680000000

def _uncapped(item_id):
    return item_id in SQUARE_UNCAPPED or item_id in {int(x) for x in CFG.get('uncapped_items', ())}

def _period_obtain(row, item_id, delta, t):
    """Items the game limits per period (m_item column 7 = how many, column 8 = the m_reset row): count what is earned,
    starting again from 0 in a new period, and give no more than the limit. Items in CFG['uncapped_items'] are counted
    (so the game's 'earned this month' still resets) but never cut off."""
    r = mdx('m_item', 0).get((item_id,))
    if not r or len(r) < 9 or not r[7] or not r[8]:
        return delta
    p = reset_period(r[8], t)
    if p is not None and (not row.last_obtain_reset_datetime or reset_period(r[8], row.last_obtain_reset_datetime) < p):
        row.obtain_count = 0
        row.last_obtain_reset_datetime = t
    if not _uncapped(item_id):
        delta = max(0, min(delta, r[7] - row.obtain_count))
    row.obtain_count += delta
    return delta

def item_cap(item_id):
    r = mdx('m_item', 0).get((item_id,))
    if _uncapped(item_id):
        return 2 ** 62
    return (r[3] if r else 0) or 2 ** 62
SQUARE_UNCAPPED = {30001, 30004}

def _gifts_path(st):
    return os.path.join(st.folder, 'inbox.json') if st.folder else None
GIFT_LIMIT_MSG = 940000000000002

def _gift_overflow(st, item_id, n):
    if not st.folder or n <= 0:
        return
    p = os.path.join(st.folder, 'inbox_overflow.json')
    pend = json.load(open(p)) if os.path.isfile(p) else []
    now = now_ms()
    pend.insert(0, {'reward_type': 'Command_Enums_RewardType_Item', 'target_id': str(item_id), 'count': str(n), 'is_gift': True, 'gift_info': {'user_gift_id': str(new_id()), 'message_template_language_id': str(GIFT_LIMIT_MSG), 'is_limit': True, 'received_datetime': str(now), 'expire_datetime': str(GIFT_NO_EXPIRE)}})
    tmp = p + '.tmp'
    json.dump(pend, open(tmp, 'w'), indent=0)
    os.replace(tmp, p)
    print(f'item {item_id}: {n} over the cap -> gift box')

def _gifts_merge_overflow(st, gifts):
    p = os.path.join(st.folder, 'inbox_overflow.json') if st.folder else None
    if not p or not os.path.isfile(p):
        return gifts
    with open(p, encoding='utf-8') as source:
        gifts = json.load(source) + gifts
    _gifts_save(st, gifts)
    os.remove(p)
    return gifts

def _gifts_load(st, ctx=None):
    p = _gifts_path(st)
    if p and os.path.isfile(p):
        with open(p, encoding='utf-8') as source:
            gifts = json.load(source)
        return _gifts_merge_overflow(st, gifts)
    gifts = []
    _gifts_save(st, gifts)
    return _gifts_merge_overflow(st, gifts)

def _has_gift_refresh(st, gifts=None):
    if st.other_info is None:
        return
    if gifts is None:
        p = _gifts_path(st)
        if not p or not os.path.isfile(p):
            return
        q = os.path.join(st.folder, 'inbox_overflow.json')
        gifts = json.load(open(p)) + (json.load(open(q)) if os.path.isfile(q) else [])
    st.other_info.has_gift = bool(gifts)

def _gifts_save(st, gifts):
    p = _gifts_path(st)
    if p:
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as target:
            json.dump(gifts, target, indent=0)
        os.replace(tmp, p)

def gift_list(st, req, ctx):
    q = req.post_pvt_gift_list
    gifts = _gifts_load(st, ctx)
    body = P.Command_ApiNetwork_Api_PostPvtGiftListResponse()
    with st.transaction('gift/list') as tx:
        page = max(1, q.page_no)
        for g in gifts[(page - 1) * GIFT_PAGE:page * GIFT_PAGE]:
            ri = body.reward_info_list.add()
            _PD(g, ri, ignore_unknown_fields=True)
            if ri.HasField('gift_info'):
                ri.gift_info.expire_datetime = GIFT_NO_EXPIRE
        body.total_gift_count = len(gifts)
        _has_gift_refresh(st, gifts)
    return _resp(tx, 'post_pvt_gift_list', body)
_RW_BY_TT = {}

def _reward_by_type_target(rt, tid):
    if not _RW_BY_TT:
        rwp = mdx('m_reward_weapon', 0)
        rar = lambda r: (rwp.get((r[0],)) or (0, 0))[1]
        for r in md('m_reward'):
            k = (r[1], r[2])
            # A weapon has one reward row per rarity, R first; the game's own gifts (events, login bonuses,
            # missions, shops) hand out its top-rarity row, so a gift naming no rarity takes that one.
            if k not in _RW_BY_TT or (r[1] == RT_WEAPON and rar(r) > rar(_RW_BY_TT[k])):
                _RW_BY_TT[k] = r
    return _RW_BY_TT.get((rt, tid))

def gift_open(st, req, ctx):
    q = req.post_pvt_gift_open
    gifts = _gifts_load(st, ctx)
    want = set(q.user_gift_id_list)
    if len(want) > 1:
        want = {int(g.get('gift_info', {}).get('user_gift_id', 0)) for g in gifts}
    body = P.Command_ApiNetwork_Api_PostPvtGiftOpenResponse()
    with st.transaction('gift/open') as tx:
        keep = []
        for g in gifts:
            gid = int(g.get('gift_info', {}).get('user_gift_id', 0))
            if gid not in want:
                keep.append(g)
                continue
            ri = P.Command_Entity_RewardInfo()
            _PD(g, ri, ignore_unknown_fields=True)
            rw = _reward_by_type_target(ri.reward_type, ri.target_id)
            if ri.reward_type == RT_ITEM:
                have = st.get('user_item_list', ri.target_id)
                if (have.count if have else 0) + (ri.count or 1) > item_cap(ri.target_id):
                    body.is_limit = True
                    keep.append(g)
                    continue
            wr = ri.reward_option.weapon_option.rarity_type if ri.reward_type == RT_WEAPON else 0
            if wr:
                for _ in range(max(1, ri.count)):
                    _grant_weapon(st, tx, ri.target_id, wr, body.reward_info_list, from_gacha=False)
            elif rw:
                grant_reward(st, tx, rw[0], ri.count or 1, body.reward_info_list)
            elif ri.reward_type == RT_ITEM:
                add_item(st, tx, ri.target_id, ri.count or 1)
                body.reward_info_list.add().CopyFrom(ri)
            elif ri.reward_type == RT_STONE:
                give_stone(st, tx, ri.target_id, ri.count or 1)
                body.reward_info_list.add().CopyFrom(ri)
            elif ri.reward_type == RT_WEAPON:
                _grant_weapon(st, tx, ri.target_id, ri.reward_option.weapon_option.rarity_type or 1, body.reward_info_list)
            else:
                print(f'gift/open: reward type {ri.reward_type} target {ri.target_id} not grantable yet')
                keep.append(g)
                continue
        _gifts_save(st, keep)
        _has_gift_refresh(st, keep)
        opened = [g for g in gifts if int(g.get('gift_info', {}).get('user_gift_id', 0)) in want and g not in keep]
        if opened:
            _hist_add(st, opened)
            mission_event(st, tx, 'SetGiftReceive', 1)
        body.gift_count = len(keep)
        if body.is_limit:
            body.reward_provide_behavior_type_list.append(P.Command_Enums_RewardProvideBehaviorType.Value('Command_Enums_RewardProvideBehaviorType_ToGiftByLimit'))
    return _resp(tx, 'post_pvt_gift_open', body)
HANDLERS.update({'/api/pvt/gift/list': gift_list, '/api/pvt/gift/open': gift_open})
GIFT_HISTORY_SHOW_MS = 7 * 86400000

def _hist_cap():
    try:
        return int(md('m_config')[0][35]) or 50
    except Exception:
        return 50

def _hist_path(st):
    return os.path.join(st.folder, 'inbox_history.json') if st.folder else None

def _hist_load(st, ctx=None):
    p = _hist_path(st)
    if p and os.path.isfile(p):
        with open(p, encoding='utf-8') as source:
            return json.load(source)
    hist = []
    _hist_save(st, hist)
    return hist

def _hist_save(st, hist):
    p = _hist_path(st)
    if p:
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as target:
            json.dump(hist[:_hist_cap()], target, indent=0)
        os.replace(tmp, p)

def _hist_add(st, opened):
    p = _hist_path(st)
    hist = json.load(open(p)) if p and os.path.isfile(p) else []
    now = now_ms()
    new = [{'reward_info': g, 'display_expire_datetime': str(now + GIFT_HISTORY_SHOW_MS), 'archived_datetime': str(now), 'is_expired': False} for g in opened]
    _hist_save(st, new + hist)

def gift_history(st, req, ctx):
    hist = _hist_load(st, ctx)
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtGiftHistoryResponse()
    with st.transaction('gift/history') as tx:
        for h in hist[:_hist_cap()]:
            x = body.history_gift_info_list.add()
            _PD(h, x, ignore_unknown_fields=True)
            x.is_expired = x.display_expire_datetime < now
    return _resp(tx, 'post_pvt_gift_history', body)
HANDLERS['/api/pvt/gift/history'] = gift_history

def _hw(hid):
    return mdx('m_highwind_battle', 0).get((hid,))

def highwind_battle_start(st, req, ctx):
    q = req.post_pvt_highwind_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtHighwindBattleStartResponse()
    with st.transaction('highwind/battle/start') as tx:
        hb = _hw(q.highwind_battle_id)
        btype = 'Command_Enums_BattleType_HighwindRare' if hb and hb[4] == 2 else 'Command_Enums_BattleType_HighwindBoss'
        _start_common(st, tx, body, hb[1] if hb else q.highwind_battle_id, q.highwind_battle_id, btype, q.party_id, 0, now, ctx=ctx)
        row = st.get('user_highwind_battle_list', q.highwind_battle_id) or st.new_row('user_highwind_battle_list', highwind_battle_id=q.highwind_battle_id)
        row.last_challenge_user_highwind_battle_release_id = q.user_highwind_battle_release_id
        tx.upsert('user_highwind_battle_list', row)
    return _resp(tx, 'post_pvt_highwind_battle_start', body)

def highwind_battle_end(st, req, ctx):
    q = req.post_pvt_highwind_battle_end
    body = P.Command_ApiNetwork_Api_PostPvtHighwindBattleEndResponse()
    with st.transaction('highwind/battle/end') as tx:
        hb = _hw(q.highwind_battle_id)
        bid = hb[1] if hb else q.highwind_battle_id
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, kinds=(('ProgressHighwindBattleWin', 0),))
        if win and hb:
            row = st.get('user_highwind_battle_list', q.highwind_battle_id) or st.new_row('user_highwind_battle_list', highwind_battle_id=q.highwind_battle_id)
            first = row.win_count == 0
            row.win_count += 1
            tx.upsert('user_highwind_battle_list', row)
            # "Highwind Boss Battles Cleared" key-item missions (type 127, target = m_highwind_battle col 4, 1 boss / 2 rare)
            mission_event(st, tx, 'ProgressHighwindBattleTypeWin', 1, hb[4])
            if first and hb[2]:
                grant_reward_set(st, tx, hb[2], 1, body.battle_result.first_reward_infos)
                _battle_first_to_gifts(st, tx, ctx, body.battle_result.first_reward_infos)
            if hb[3]:
                grant_reward_set(st, tx, hb[3], 1, body.battle_result.reward_infos)
            if hb[4] == 2 and row.last_challenge_user_highwind_battle_release_id:
                rel = st.get('user_highwind_battle_release_list', row.last_challenge_user_highwind_battle_release_id)
                if rel is not None and (not rel.release_end_datetime):
                    rel.release_end_datetime = now_ms()
                    tx.upsert('user_highwind_battle_release_list', rel)
        body.ClearField('score_result')
    return _resp(tx, 'post_pvt_highwind_battle_end', body)

def _box_groups(box_id):
    return sorted((r for r in md('m_box_gacha_reward_group_rel') if r[0] == box_id), key=lambda r: r[2])

def _box_state(st, box_id):
    ub = st.get('user_box_gacha_list', box_id)
    if not ub:
        groups = _box_groups(box_id)
        ub = st.new_row('user_box_gacha_list', box_gacha_id=box_id, current_box_gacha_reward_group_id=groups[0][1] if groups else 0)
    return ub

def box_gacha_draw(st, req, ctx):
    q = req.post_pvt_box_gacha_draw
    body = P.Command_ApiNetwork_Api_PostPvtBoxGachaDrawResponse()
    with st.transaction('box/gacha/draw') as tx:
        box = mdx('m_box_gacha', 0).get((q.box_gacha_id,))
        if not box:
            raise ValueError(f'unknown box gacha {q.box_gacha_id}')
        ub = _box_state(st, q.box_gacha_id)
        tx.upsert('user_box_gacha_list', ub)
        rewards = [r for r in md('m_box_gacha_reward') if r[1] == ub.current_box_gacha_reward_group_id]
        drawn = {r[0]: st.get('user_box_gacha_reward_limited_list', q.box_gacha_id, r[0]) for r in rewards}
        n = max(1, q.draw_count)
        if box[3] and box[4]:
            add_item(st, tx, box[3], -box[4] * n)
        groups = _box_groups(q.box_gacha_id)
        cur = next((g for g in groups if g[1] == ub.current_box_gacha_reward_group_id), None)
        last = cur is not None and not any(g[2] == cur[3] and g[1] != cur[1] for g in groups)
        for _ in range(n):
            # #64: on the last level (its NextSeq is itself) the prizes start over once every limited one is drawn,
            # as a reset there would; the reply says so (is_reward_group_updated). Inferred: no recording of it.
            if last and any(r[4] for r in rewards) and all((drawn[r[0]].draw_count if drawn[r[0]] else 0) >= r[4] for r in rewards if r[4]):
                for r in rewards:
                    if r[4] and drawn[r[0]]:
                        drawn[r[0]].draw_count = 0
                        tx.upsert('user_box_gacha_reward_limited_list', drawn[r[0]])
                ub.total_reward_group_update_count += 1
                tx.upsert('user_box_gacha_list', ub)
                body.is_reward_group_updated = True
            pool = [(r, r[4] - (drawn[r[0]].draw_count if drawn[r[0]] else 0) if r[4] else 1) for r in rewards]
            pool = [(r, w) for r, w in pool if w > 0]
            if not pool:
                break
            r = random.choices([p[0] for p in pool], weights=[p[1] for p in pool])[0]
            if r[4]:
                lim = drawn[r[0]] or st.new_row('user_box_gacha_reward_limited_list', box_gacha_id=q.box_gacha_id, box_gacha_reward_id=r[0])
                lim.draw_count += 1
                drawn[r[0]] = lim
                tx.upsert('user_box_gacha_reward_limited_list', lim)
            info = body.box_gacha_reward_info_list.add()
            info.is_rare = r[2] != 1      # #65: Rare (2) and Limited (3) prizes are the gold ones; Normal (1) was marked gold
            tmp = P.Command_ApiNetwork_Api_PostPvtGachaDrawResponse()
            grant_reward_set(st, tx, r[5], 1, tmp.gacha_result_list)
            if tmp.gacha_result_list:
                info.reward_info.CopyFrom(tmp.gacha_result_list[0])
    return _resp(tx, 'post_pvt_box_gacha_draw', body)

def box_gacha_reward_group_update(st, req, ctx):
    q = req.post_pvt_box_gacha_reward_group_update
    body = P.Command_ApiNetwork_Api_PostPvtBoxGachaRewardGroupUpdateResponse()
    with st.transaction('box/gacha/reward/group/update') as tx:
        ub = _box_state(st, q.box_gacha_id)
        groups = _box_groups(q.box_gacha_id)
        cur = next((g for g in groups if g[1] == ub.current_box_gacha_reward_group_id), None)
        nxt = next((g for g in groups if cur and g[2] == cur[3]), None) or (groups[-1] if groups else None)
        if nxt:
            ub.current_box_gacha_reward_group_id = nxt[1]
        ub.total_reward_group_update_count += 1
        tx.upsert('user_box_gacha_list', ub)
        for lim in list(st['user_box_gacha_reward_limited_list'].values()):
            if lim.box_gacha_id == q.box_gacha_id:
                lim.draw_count = 0
                tx.touch('user_box_gacha_reward_limited_list', lim)
    return _resp(tx, 'post_pvt_box_gacha_reward_group_update', body)
HANDLERS.update({'/api/pvt/highwind/battle/start': highwind_battle_start, '/api/pvt/highwind/battle/end': highwind_battle_end, '/api/pvt/box/gacha/draw': box_gacha_draw, '/api/pvt/box/gacha/reward/group/update': box_gacha_reward_group_update})

def recovery_item(st, req, ctx):
    q = req.post_pvt_recovery_item
    body = P.Command_ApiNetwork_Api_PostPvtRecoveryItemResponse()
    with st.transaction('recovery/item') as tx:
        status = st.one('user_status_list')
        gain = 0
        keys = q.recovery_type == RECOVER_KEY      # Dungeon Key Vouchers (m_item_another_dungeon_key) for keys
        for item_id, n in q.consumption_item_id_map.items():
            per = next((r[1] for r in md('m_item_another_dungeon_key' if keys else 'm_item_stamina') if r[0] == item_id), 0)
            add_item(st, tx, item_id, -n)
            gain += per * n
        if keys:
            dungeon_keys_add(st, tx, gain)
        elif status:
            stamina_add(status, gain)
            tx.touch('user_status_list', status)
    return _resp(tx, 'post_pvt_recovery_item', body)

def recovery_stone(st, req, ctx):
    q = req.post_pvt_recovery_stone
    body = P.Command_ApiNetwork_Api_PostPvtRecoveryStoneResponse()
    with st.transaction('recovery/stone') as tx:
        rs = mdx('m_recovery_stone', 0).get((q.recovery_stone_id,))
        row = st.get('user_recovery_stone_list', q.recovery_stone_id) or st.new_row('user_recovery_stone_list', recovery_stone_id=q.recovery_stone_id)
        now = now_ms()
        if rs and reset_period(rs[3], row.last_reset_datetime) != reset_period(rs[3], now):
            row.recovery_count = 0          # RecoveryResetId (col 3): the price steps and the limit start over
            row.last_reset_datetime = now
        cons = sorted((r for r in md('m_recovery_stone_consumption') if rs and r[0] == rs[2]), key=lambda r: r[1])
        step = next((r for r in reversed(cons) if r[1] <= row.recovery_count), cons[0] if cons else None)
        if step:
            cost, gain = (step[2], step[3])
            free = next((s for s in st.other_info.user_stone_list if s.stone_id == 2), None)
            n = cost
            if free:
                use = min(free.count, n)
                free.count -= use
                n -= use
            if n:
                give_stone(st, tx, 1, -n)
            status = st.one('user_status_list')
            if rs[1] == RECOVER_KEY:     # RecoveryType (col 1): Crystals for Dungeon Keys, not stamina
                dungeon_keys_add(st, tx, gain)
            elif status:
                stamina_add(status, gain)
                tx.touch('user_status_list', status)
        row.recovery_count += 1
        row.total_recovery_count += 1
        tx.upsert('user_recovery_stone_list', row)
    return _resp(tx, 'post_pvt_recovery_stone', body)

def _weapon_row(wid):
    return mdx('m_weapon', 0).get((wid,))

def _level_exp_cap(mw, release_count):
    rel = mdx('m_weapon_release_setting', 0, 1).get((mw[12], release_count))
    lvl = rel[2] if rel else 20
    lv = mdx('m_weapon_level', 0, 1).get((mw[3], lvl))
    return lv[2] if lv else 10 ** 9

def _consume_ingredients(st, tx, group):
    for r in md('m_weapon_ingredient'):
        if r[0] == group:
            add_item(st, tx, r[1], -r[2])

def _enhance(st, tx, w, mw, items, unused=None):
    cap = _level_exp_cap(mw, w.release_count)
    room = room0 = max(0, cap - w.exp)
    exp_gain = 0
    used = 0
    for item_id, n in items.items():
        per = next((r[1] for r in md('m_item_weapon_enhance_ingredient') if r[0] == item_id), 0)
        take = min(n, -(-room // per)) if per and room > 0 else 0
        if take:
            add_item(st, tx, item_id, -take)
            exp_gain += per * take
            used += take
            room = max(0, room - per * take)
        if unused is not None and n - take:
            unused[item_id] = n - take
    w.exp = min(cap, w.exp + exp_gain)
    w.enhance_count += used
    if used:
        mission_event(st, tx, 'ProgressWeaponEnhance', used)
    # Charge only the exp the weapon can still take; cap is a total, not the room left below it.
    return min(exp_gain, room0) * (mw[5] or 0) // 1000

def _release(st, tx, w, mw):
    rel = mdx('m_weapon_release_setting', 0, 1).get((mw[12], w.release_count))
    if not rel:
        return 0
    if rel[3]:
        _consume_ingredients(st, tx, rel[3])
    w.release_count += 1
    mission_event(st, tx, 'ProgressWeaponRelease')
    return rel[4]

def weapon_enhance(st, req, ctx):
    q = req.post_pvt_weapon_enhance
    body = P.Command_ApiNetwork_Api_PostPvtWeaponEnhanceResponse()
    with st.transaction('weapon/enhance') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        mw = _weapon_row(q.weapon_id)
        if not w or not mw:
            raise ValueError(f'weapon {q.weapon_id} not owned / unknown')
        body.before_user_weapon.CopyFrom(w)
        unused = {}
        gil = _enhance(st, tx, w, mw, dict(q.ingredient_item_map), unused)
        for k, v in unused.items():
            body.unused_ingredient_item_map[k] = v
        if gil:
            add_item(st, tx, GIL_ITEM, -gil)
        body.consumption_gil = gil
        tx.touch('user_weapon_list', w)
        body.after_user_weapon.CopyFrom(w)
        _pr_max(st, tx, 'WeaponLevelMax', _lvl_weapon(w.weapon_id, w.exp))
    return _resp(tx, 'post_pvt_weapon_enhance', body)

def weapon_release(st, req, ctx):
    q = req.post_pvt_weapon_release
    body = P.Command_ApiNetwork_Api_PostPvtWeaponReleaseResponse()
    with st.transaction('weapon/release') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        mw = _weapon_row(q.weapon_id)
        if not w or not mw:
            raise ValueError(f'weapon {q.weapon_id} not owned / unknown')
        body.before_user_weapon.CopyFrom(w)
        gil = _release(st, tx, w, mw)
        if gil:
            add_item(st, tx, GIL_ITEM, -gil)
        body.consumption_gil = gil
        tx.touch('user_weapon_list', w)
        body.after_user_weapon.CopyFrom(w)
    return _resp(tx, 'post_pvt_weapon_release', body)

def weapon_enhance_release(st, req, ctx):
    q = req.post_pvt_weapon_enhance_release
    body = P.Command_ApiNetwork_Api_PostPvtWeaponEnhanceReleaseResponse()
    with st.transaction('weapon/enhance/release') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        mw = _weapon_row(q.weapon_id)
        if not w or not mw:
            raise ValueError(f'weapon {q.weapon_id} not owned / unknown')
        body.before_user_weapon.CopyFrom(w)
        gil = 0
        for _ in range(max(1, q.weapon_release_count)):
            gil += _release(st, tx, w, mw)
        gil += _enhance(st, tx, w, mw, dict(q.enhance_ingredient_item_map))
        if gil:
            add_item(st, tx, GIL_ITEM, -gil)
        body.consumption_gil = gil
        tx.touch('user_weapon_list', w)
        body.after_user_weapon.CopyFrom(w)
        _pr_max(st, tx, 'WeaponLevelMax', _lvl_weapon(w.weapon_id, w.exp))
    return _resp(tx, 'post_pvt_weapon_enhance_release', body)

def weapon_upgrade(st, req, ctx):
    q = req.post_pvt_weapon_upgrade
    body = P.Command_ApiNetwork_Api_PostPvtWeaponUpgradeResponse()
    with st.transaction('weapon/upgrade') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        mw = _weapon_row(q.weapon_id)
        if not w or not mw:
            raise ValueError(f'weapon {q.weapon_id} not owned / unknown')
        body.before_user_weapon.CopyFrom(w)
        row = mdx('m_weapon_upgrade_consumption', 0, 1, 2).get((mw[15], q.weapon_upgrade_type, q.upgrade_count))
        gil = 0
        if row:
            gil = row[4]
        for item_id, n in q.ingredient_item_map.items():
            add_item(st, tx, item_id, -n)
        if gil:
            add_item(st, tx, GIL_ITEM, -gil)
        up = max(1, q.upgrade_count - w.upgrade_count)
        w.weapon_upgrade_type = q.weapon_upgrade_type
        w.upgrade_count = q.upgrade_count
        mission_event(st, tx, 'ProgressWeaponUpgrade', up, q.weapon_id)
        mission_event(st, tx, 'ProgressWeaponUpgradeCount')
        body.consumption_gil = gil
        tx.touch('user_weapon_list', w)
        body.after_user_weapon.CopyFrom(w)
        _pr_max(st, tx, 'WeaponUpgradeCountMax', w.upgrade_count)
    return _resp(tx, 'post_pvt_weapon_upgrade', body)
HANDLERS.update({'/api/pvt/recovery/item': recovery_item, '/api/pvt/recovery/stone': recovery_stone, '/api/pvt/weapon/enhance': weapon_enhance, '/api/pvt/weapon/release': weapon_release, '/api/pvt/weapon/enhance/release': weapon_enhance_release, '/api/pvt/weapon/upgrade': weapon_upgrade})

def _hw_cfg():
    c = md('m_config_highwind')
    return ((c[0][1] if c else 30) * 60000, c[0][2] if c else 99)

def _hw_parts_level(st, pid):
    r = st.get('user_highwind_parts_list', pid)
    return r.level if r else 1

def _hw_normal_set(st):
    lvl = next((r[3] for r in md('m_highwind_idling_parts') if r[0] == 3 and r[1] == _hw_parts_level(st, 3)), 1)
    return next((r[2] for r in md('m_highwind_idling_item_normal_level') if r[0] == lvl), 0)

def _hw_max_collect(st):
    return next((r[2] for r in md('m_highwind_idling_parts') if r[0] == 2 and r[1] == _hw_parts_level(st, 2)), 10)
HW_PLATE = 18002
HW_RARE_POOL = (2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2020, 2049, 2050, 2051, 2052, 2056, 6005, 6006, 6007, 6008, 10111, 10112, 10172, 10202, 18003)

# Character Memories (item ids 2001-2099) were rolled only at rare level 120, so an airship below max Radar never found
# one (#78). Below 120 each rare roll now turns up one character Memory; the level 120 pool is unchanged.
HW_RARE_MEMORIES = tuple((i for i in HW_RARE_POOL if 2000 < i < 2100))

def _hw_rare_level(st):
    return next((r[4] for r in md('m_highwind_idling_parts') if r[0] == 4 and r[1] == _hw_parts_level(st, 4)), 1) or 1

def _hw_rare(st, tx, infos, n):
    if n <= 0:
        return
    L = min(max(1, _hw_rare_level(st)), 120)
    per = 0.6 * (5582 / 0.6) ** ((L - 1) / 119)
    plates = int(round(per * n * random.uniform(0.5, 1.5)))
    got = {}
    pool = HW_RARE_POOL if L >= 120 else HW_RARE_MEMORIES
    for _ in range(sum((1 for _ in range(n) if random.random() < 0.15))):
        i = random.choice(pool)
        got[i] = got.get(i, 0) + (1 if L < 120 else random.randint(4, 5) if 6000 < i < 7000 else random.randint(2, 3))
    if plates:
        add_big_item(st, tx, HW_PLATE, plates, 0)
        ri = infos.add()
        ri.reward_type = RT_BIGITEM
        ri.target_id = HW_PLATE
        ri.count = plates
        ri.reward_option.big_item_option.exponent = 0
    for i, k in got.items():
        add_item(st, tx, i, k)
        ri = infos.add()
        ri.reward_type = RT_ITEM
        ri.target_id = i
        ri.count = k
HW_RARE_PAIRS = {20: (20016,), 30: (20001,), 40: (20002, 20003), 45: (20004, 20005), 50: (20007, 20008), 55: (20010, 20011), 60: (20029, 20030)}
HW_RARE_BOMB = 20031

def _hw_rare_pool(st):
    """Rare encounters offered: the pair whose monster level is the highest at or below the active
    party's average character level, plus the Golden Bomb. Rare battles appear from the start and
    their level follows the party's strength; recorded maxed parties only ever got the Lv 60 pair.
    Offering the Lv 60 pair to every account paid its 1.6G Paint Cans regardless of progress (#45)."""
    party = next((p for p in st['user_party_list'].values() if p.is_active), None)
    chars = _party_characters(st, party.party_id) if party else []
    lv = [_lvl_char(c.character_id, c.exp) for c in chars if c]
    avg = sum(lv) / len(lv) if lv else 0
    tier = max((k for k in HW_RARE_PAIRS if k <= avg), default=min(HW_RARE_PAIRS))
    return HW_RARE_PAIRS[tier] + (HW_RARE_BOMB,)

def _hw_rare_battles(st, tx, body, n):
    if n <= 0 or not hasattr(body, 'rare_highwind_battle_ids'):
        return
    tier = _hw_rare_pool(st)
    k = min(len(tier), sum((1 for _ in range(n) if random.random() < 0.012)))
    now = now_ms()
    for hid in random.sample(tier, k):
        if not _hw(hid):
            continue
        row = st.new_row('user_highwind_battle_release_list', user_highwind_battle_release_id=new_id(), highwind_battle_id=hid, release_start_datetime=now)
        tx.upsert('user_highwind_battle_release_list', row)
        body.rare_highwind_battle_ids.append(hid)

def _hw_merge_big(infos):
    """One reward row per material with the summed count, as the official collect reply sends it.
    grant_reward_set adds one row per collect, so Paint Cans showed as many separate items (#47)."""
    big = {}
    out = []
    for ri in infos:
        if ri.reward_type == RT_BIGITEM:
            k = (ri.target_id, ri.reward_option.big_item_option.exponent)
            if k in big:
                big[k].count += ri.count
                continue
            c = P.Command_Entity_RewardInfo()
            c.CopyFrom(ri)
            big[k] = c
            out.append(c)
        else:
            c = P.Command_Entity_RewardInfo()
            c.CopyFrom(ri)
            out.append(c)
    if len(out) == len(infos):
        return
    del infos[:]
    for c in out:
        infos.add().CopyFrom(c)

def _hw_collect(st, tx, body, n):
    hw = st.one('user_highwind_list') or st.new_row('user_highwind_list', idling_start_datetime=now_ms())
    sid = _hw_normal_set(st)
    if sid and n:
        grant_reward_set(st, tx, sid, n, body.normal_reward_infos)
    _hw_merge_big(body.normal_reward_infos)
    _hw_rare(st, tx, body.rare_reward_infos, n)
    _hw_rare_battles(st, tx, body, n)
    hw.total_idling_collect_count += n
    tx.upsert('user_highwind_list', hw)
    mission_event(st, tx, 'ProgressHighwindIdlingCollect', n)
    return hw

def highwind_idling_collect_result(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtHighwindIdlingCollectResultResponse()
    with st.transaction('highwind/idling/collect/result') as tx:
        interval, _ = _hw_cfg()
        now = now_ms()
        hw = st.one('user_highwind_list')
        if hw is None or not hw.idling_start_datetime:
            # A new account has no Highwind row: the game reads that as idle since forever and shows the full 10/10, so the
            # first collect pays the full amount (it paid nothing and showed N/A, #40).
            n = _hw_max_collect(st)
        else:
            n = min(int((now - hw.idling_start_datetime) // interval), _hw_max_collect(st))
        hw = _hw_collect(st, tx, body, max(0, n))
        hw.idling_start_datetime = now
        tx.upsert('user_highwind_list', hw)
        t = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_HighwindIdlingCollectCount')
        pr = st.get('user_play_record_list', t) or st.new_row('user_play_record_list', play_record_type=t)
        if pr.int_value != hw.total_idling_collect_count:
            pr.int_value = hw.total_idling_collect_count
            tx.upsert('user_play_record_list', pr)
    return _resp(tx, 'post_pvt_highwind_idling_collect_result', body)

def highwind_idling_collect_skip(st, req, ctx):
    q = req.post_pvt_highwind_idling_collect_skip
    body = P.Command_ApiNetwork_Api_PostPvtHighwindIdlingCollectSkipResponse()
    with st.transaction('highwind/idling/collect/skip') as tx:
        _, skip_max = _hw_cfg()
        n = 0
        for item_id, k in q.skip_item_map.items():
            per = next((r[1] for r in md('m_item_highwind_idling_skip') if r[0] == item_id), 1)
            add_item(st, tx, item_id, -k)
            n += per * k
        _hw_collect(st, tx, body, min(n, skip_max))
    return _resp(tx, 'post_pvt_highwind_idling_collect_skip', body)
HANDLERS.update({'/api/pvt/highwind/idling/collect/result': highwind_idling_collect_result, '/api/pvt/highwind/idling/collect/skip': highwind_idling_collect_skip})
RT_HW_EFFECT = 30

def _grant_hw_effect(st, tx, effect_id, infos=None):
    fx = mdx('m_highwind_key_item_effect', 0).get((effect_id,))
    if not fx:
        return
    grp, idx = (fx[1], fx[2])
    row = st.get('user_highwind_key_item_effect_group_list', grp) or st.new_row('user_highwind_key_item_effect_group_list', highwind_key_item_effect_group_id=grp)
    f = f'received_highwind_key_item_effect_group_idx_flags{idx // 64}'
    if hasattr(row, f):
        setattr(row, f, _flag_or(getattr(row, f), idx))
    tx.upsert('user_highwind_key_item_effect_group_list', row)
    if infos is not None:
        ri = infos.add()
        ri.reward_type = RT_HW_EFFECT
        ri.target_id = effect_id
        ri.count = 1

def highwind_key_item_rank_upgrade(st, req, ctx):
    q = req.post_pvt_highwind_key_item_rank_upgrade
    body = P.Command_ApiNetwork_Api_PostPvtHighwindKeyItemRankUpgradeResponse()
    with st.transaction('highwind/key/item/rank/upgrade') as tx:
        ki = st.get('user_highwind_key_item_list', q.highwind_key_item_id)
        if not ki:
            raise ValueError(f'key item {q.highwind_key_item_id} not owned')
        mk = mdx('m_highwind_key_item', 0).get((q.highwind_key_item_id,))
        grp = mk[8] if mk else q.highwind_key_item_id
        row = mdx('m_highwind_key_item_rank_upgrade', 1, 3).get((grp, q.upgrade_count))
        if not row:
            raise ValueError(f'no rank {q.upgrade_count} for key item {q.highwind_key_item_id}')
        if row[4]:
            consume_set(st, tx, row[4])
        if row[5]:
            for rel in md('m_reward_set_reward_rel'):
                if rel[1] == row[5]:
                    rw = mdx('m_reward', 0).get((rel[2],))
                    if rw and rw[1] == RT_HW_EFFECT:
                        _grant_hw_effect(st, tx, rw[2], body.reward_info_list)
                    elif rw:
                        grant_reward(st, tx, rel[2], rel[4] or 1, body.reward_info_list)
        ki.rank_upgrade_type = q.rank_upgrade_type or 1
        ki.upgrade_count = q.upgrade_count
        tx.upsert('user_highwind_key_item_list', ki)
    return _resp(tx, 'post_pvt_highwind_key_item_rank_upgrade', body)
HANDLERS['/api/pvt/highwind/key/item/rank/upgrade'] = highwind_key_item_rank_upgrade
QE = {1: (3, 4), 2: (5, 6), 3: (7, 8), 4: (9, 10), 5: (11, 12)}
PR_ATTACH = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_WeaponAttachmentTotalAttachCount')

def _materia_levels(m):
    mm = mdx('m_materia', 0).get((m.materia_id,))
    grp = mm[3] if mm else 1
    b = mdx('m_materia_base_coefficient', 0, 1, 2).get((mm[1] if mm else 1, m.evolve_count, int(m.quality_type)))
    if b is None:
        b = mdx('m_materia_base_coefficient', 0, 1, 2).get((mm[1] if mm else 1, 0, int(m.quality_type)))
    base = b[3] if b else 1000
    return [[r[0], r[1], r[2] * base // 1000] + list(r[3:]) for r in sorted((r for r in md('m_materia_level') if r[0] == grp), key=lambda r: r[1])]

def _param_levels(m, lv0, lv1):
    mm = mdx('m_materia', 0).get((m.materia_id,))
    g = mm[4] if mm else 1
    return sum((1 for r in md('m_materia_enhance') if r[0] == g and r[2] and (lv0 < r[1] <= lv1)))

def _materia_level(levels, exp):
    return max((r for r in levels if r[2] <= exp), key=lambda r: r[1], default=levels[0])

def _evolve_setting(m, count=None):
    """A few materia (Ruinra 2102003 and three others) have no evolve rows of their own: m_materia col 5 names the
    materia whose evolve cost and level caps they share, as the game itself reads them."""
    c = m.evolve_count if count is None else count
    ix = mdx('m_materia_evolve_setting', 0, 1)
    row = ix.get((m.materia_id, c))
    if row is None:
        mm = mdx('m_materia', 0).get((m.materia_id,))
        if mm and len(mm) > 5 and mm[5] and mm[5] != m.materia_id:
            row = ix.get((mm[5], c))
    return row

def _evolve_id(m):
    return next((r[0] for r in md('m_materia_evolve') if r[1] == m.materia_id and r[2] == m.evolve_count), None)

def _collection(st, tx, m, bump=0, max_level=None):
    eid = _evolve_id(m)
    if not eid:
        return
    row = st.get('user_materia_collection_list', eid) or st.new_row('user_materia_collection_list', materia_evolve_id=eid)
    row.enhance_count += bump
    if max_level:
        row.max_level = max(row.max_level, max_level)
    tx.upsert('user_materia_collection_list', row)

def _grow(m, levelups):
    params = {r[0]: r for r in md('m_materia_parameter')}
    lo_i, hi_i = QE.get(m.quality_type, QE[5])
    slots = [i for i in range(m.parameter_open_count) if getattr(m, f'parameter_id{i}')]
    for _ in range(levelups):
        if not slots:
            break
        i = random.choice(slots)
        p = params.get(getattr(m, f'parameter_id{i}'))
        if p:
            setattr(m, f'parameter_value{i}', getattr(m, f'parameter_value{i}') + _biased(p[lo_i], p[hi_i]))

def _fill_open_slots(m):
    params = {r[0]: r for r in md('m_materia_parameter')}
    have = {getattr(m, f'parameter_id{i}') for i in range(4)} - {0}
    pool = [int(x) for x in POOLS.get(str(m.materia_id), [])]
    if not pool:
        fam = str(m.materia_id)[:4]
        pool = sorted({int(p) for k, v in POOLS.items() if k[:4] == fam for p in v})
    free = [p for p in pool if p not in have and p in params]
    lo_i, hi_i = QV.get(int(m.quality_type), QV[5])
    n = 0
    for i in range(4):
        if getattr(m, f'parameter_id{i}'):
            continue
        if not free:
            if not any((getattr(m, f'parameter_id{j}') for j in range(i, 4))):
                m.parameter_open_count = min(m.parameter_open_count, i)
            break
        pid = free.pop(random.randrange(len(free)))
        setattr(m, f'parameter_id{i}', pid)
        setattr(m, f'parameter_value{i}', _biased(params[pid][lo_i], params[pid][hi_i]))
        n += 1
    return n

def _craft_slot_heal(st):
    """A materia synth slot marked busy with no synth behind it (a Never Crisis import leaves out synths in progress):
    empty the slot, so the synth screen has nothing missing to show and the slot can be used again."""
    stuck = [c for c in st.tables['user_craft_list'].values()
             if not c.is_empty and c.craft_type == CT_MATERIA and not st.get('user_materia_craft_list', c.idx)]
    if not stuck:
        return 0
    with st.transaction('craft: empty slots with no synth') as tx:
        for c in stuck:
            c.is_empty = True
            tx.upsert('user_craft_list', c)
            for row in [r for r in st['user_craft_option_item_list'].values() if r.idx == c.idx]:
                tx.delete('user_craft_option_item_list', row)
    print(f'craft slot heal: {st.user_id} {len(stuck)} synth slot(s) emptied')
    return len(stuck)

def _materia_slot_heal(st):
    bad = [m for m in st.tables['user_materia_list'].values() if any((not getattr(m, f'parameter_id{i}') for i in range(m.parameter_open_count)))]
    if not bad:
        return 0
    with st.transaction('materia: fill empty open slots') as tx:
        for m in bad:
            _fill_open_slots(m)
            tx.upsert('user_materia_list', m)
    print(f'slot heal: {st.user_id} {len(bad)} materia had an empty open slot')
    return len(bad)

def _materia_reveal_heal(st):
    orig = {}
    try:
        a = P.Command_ApiNetwork_Api_ApiResponse()
        a.ParseFromString(open(os.path.join(st.folder, 'base.pb'), 'rb').read())
        orig = {r.user_materia_id: r.exp for r in a.common.user.update.user_materia_list}
    except Exception:
        return 0
    fix = []
    for m in st.tables['user_materia_list'].values():
        if not m.exp or orig.get(m.user_materia_id) == m.exp or m.parameter_open_count >= 4:
            continue
        want = min(4, 1 + _param_levels(m, 1, _materia_level(_materia_levels(m), m.exp)[1]))
        if m.parameter_open_count < want:
            fix.append((m, want))
    if not fix:
        return 0
    with st.transaction('materia: reveal sub-stats') as tx:
        for m, want in fix:
            m.parameter_open_count = want
            _fill_open_slots(m)
            tx.upsert('user_materia_list', m)
    print(f'materia reveal heal: {st.user_id} {len(fix)} materia got hidden sub-stats revealed')
    return len(fix)

def _materia_collection_heal(st):
    best = {}
    for m in st.tables['user_materia_list'].values():
        eid = _evolve_id(m)
        if eid:
            best[eid] = max(best.get(eid, 0), _materia_level(_materia_levels(m), m.exp)[1])
    fix = [(eid, lv) for eid, lv in best.items() if lv > getattr(st.get('user_materia_collection_list', eid), 'max_level', 0)]
    if not fix:
        return 0
    with st.transaction('materia: collection max level') as tx:
        for eid, lv in fix:
            row = st.get('user_materia_collection_list', eid) or st.new_row('user_materia_collection_list', materia_evolve_id=eid)
            row.max_level = lv
            tx.upsert('user_materia_collection_list', row)
    print(f'materia collection heal: {st.user_id} {len(fix)} collection row(s) raised')
    return len(fix)

def _materia_cap_heal(st):
    fix = []
    for m in st.tables['user_materia_list'].values():
        es = _evolve_setting(m)
        if not es:
            continue
        cap = next((r[2] for r in _materia_levels(m) if r[1] == es[2]), None)
        if cap is not None and m.exp > cap:
            fix.append((m, cap))
    if not fix:
        return 0
    with st.transaction('materia: trim exp to level cap') as tx:
        for m, cap in fix:
            m.exp = cap
            tx.upsert('user_materia_list', m)
    print(f'materia cap heal: {st.user_id} {len(fix)} materia trimmed to their level cap')
    return len(fix)

def _materia_enhance_roll():
    r = random.random()
    for t, chance, mult in CFG.get('materia_enhance_results', [[3, 0.02, 3.0], [2, 0.05, 2.0], [1, 0.15, 1.5]]):
        if r < chance:
            return (int(t), float(mult))
        r -= chance
    return (0, 1.0)

def materia_enhance(st, req, ctx):
    q = req.post_pvt_materia_enhance
    body = P.Command_ApiNetwork_Api_PostPvtMateriaEnhanceResponse()
    with st.transaction('materia/enhance') as tx:
        m = st.get('user_materia_list', q.user_materia_id)
        if not m:
            raise ValueError(f'materia {q.user_materia_id} not owned')
        body.before_user_materia.CopyFrom(m)
        levels = _materia_levels(m)
        es = _evolve_setting(m)
        cap_lv = es[2] if es else levels[-1][1]
        cap = next((r[2] for r in levels if r[1] == cap_lv), levels[-1][2])
        room = max(0, cap - m.exp)
        lv0 = _materia_level(levels, m.exp)[1]
        base = 0
        for uid in q.ingredient_materia_id_list:
            ing = st.get('user_materia_list', uid)
            if not ing or uid == q.user_materia_id:
                continue
            il = _materia_level(_materia_levels(ing), ing.exp)
            imm = mdx('m_materia', 0).get((ing.materia_id,))
            bc = mdx('m_materia_base_coefficient', 0, 1, 2).get((imm[1] if imm else 1, ing.evolve_count, int(ing.quality_type))) or mdx('m_materia_base_coefficient', 0, 1, 2).get((imm[1] if imm else 1, 0, int(ing.quality_type)))
            base += (bc[4] if bc else 150) * il[3] // 1000
            tx.delete('user_materia_list', ing)
        per_item = {r[0]: r[1] for r in md('m_item_materia_enhance_ingredient')}
        for item_id, n in q.ingredient_item_map.items():
            if per_item.get(item_id) and n > 0:
                add_item(st, tx, item_id, -n)
                base += per_item[item_id] * n
        rt, mult = _materia_enhance_roll() if base and room else (0, 1.0)
        total = int(base * mult)
        gain = min(total, room)
        gil = min(base, room)
        over = total - gain
        for item_id, per in sorted(per_item.items(), key=lambda kv: -kv[1]):
            k = over // per
            if not k:
                continue
            over -= k * per
            body.unused_ingredient_item_map[item_id] = k
            row = st.get('user_item_list', item_id) or st.new_row('user_item_list', item_id=item_id)
            row.count += k
            tx.upsert('user_item_list', row)
        m.exp += gain
        lv1 = _materia_level(levels, m.exp)[1]
        ev = _param_levels(m, lv0, lv1) if lv1 > lv0 else 0
        reveal = min(ev, max(0, 4 - m.parameter_open_count))
        if reveal:
            m.parameter_open_count += reveal
            _fill_open_slots(m)
        if ev - reveal:
            _grow(m, ev - reveal)
        if gil:
            add_item(st, tx, GIL_ITEM, -gil)
        tx.touch('user_materia_list', m)
        _collection(st, tx, m, bump=1, max_level=lv1)
        mission_event(st, tx, 'ProgressMateriaEnhance', sum(q.ingredient_item_map.values()) + len(q.ingredient_materia_id_list))
        body.consumption_gil = gil
        body.after_user_materia.CopyFrom(m)
        # The materia screen knows two results, Normal and Good: it reads Great or Excellent as no level-up at all and
        # waits on an animation that never plays, so the game sits on the upgrade screen. Their bonus still counts.
        body.result_type = min(rt, 1)
    return _resp(tx, 'post_pvt_materia_enhance', body)

def materia_evolve(st, req, ctx):
    q = req.post_pvt_materia_evolve
    body = P.Command_ApiNetwork_Api_PostPvtMateriaEvolveResponse()
    with st.transaction('materia/evolve') as tx:
        m = st.get('user_materia_list', q.user_materia_id)
        if not m:
            raise ValueError(f'materia {q.user_materia_id} not owned')
        es = _evolve_setting(m)
        nxt = _evolve_setting(m, m.evolve_count + 1)
        if not es or not nxt:
            raise ValueError(f'materia {m.materia_id} evolve {m.evolve_count} has no next stage')
        body.before_user_materia.CopyFrom(m)
        for r in md('m_materia_ingredient'):
            if r[0] == es[3]:
                add_item(st, tx, r[1], -r[2])
        if es[4]:
            add_item(st, tx, GIL_ITEM, -es[4])
        m.evolve_count += 1
        tx.touch('user_materia_list', m)
        mission_event(st, tx, 'ProgressMateriaEvolve')
        _collection(st, tx, m, max_level=nxt[2])
        body.consumption_gil = es[4]
        body.after_user_materia.CopyFrom(m)
    return _resp(tx, 'post_pvt_materia_evolve', body)

def materia_sell(st, req, ctx):
    q = req.post_pvt_materia_sell
    body = P.Command_ApiNetwork_Api_PostPvtMateriaSellResponse()
    with st.transaction('materia/sell') as tx:
        gil = 0
        for uid in q.user_materia_id_list:
            m = st.get('user_materia_list', uid)
            if not m:
                continue
            lv = _materia_level(_materia_levels(m), m.exp)
            gil += lv[4] * m.quality_type // 10
            tx.delete('user_materia_list', m)
        if gil:
            add_item(st, tx, GIL_ITEM, gil)
        body.sales_gil = gil
    return _resp(tx, 'post_pvt_materia_sell', body)

def _attach_effect_index():
    if 'attach_fx' not in MD:
        MD['attach_fx'] = {(str(r[0])[:7], r[1], r[2]): r[0] for r in md('m_weapon_attachment_effect')}
    return MD['attach_fx']

def _roll_attachment(item_id):
    mi = mdx('m_item_weapon_attachment', 0).get((item_id,))
    if not mi:
        raise ValueError(f'item {item_id} is not an attachment')
    counts = [r for r in md('m_weapon_attachment_effect_count_lot') if r[0] == mi[1]]
    probs = [r for r in md('m_auto_weapon_attachment_effect_probability') if r[0] == mi[2]]
    idx = _attach_effect_index()
    pre = str(mi[2])
    n = _roll(counts, 2)[1] if counts else 1
    out = []
    for _ in range(n):
        pr = _roll(probs, 5)
        v = random.randint(pr[3], pr[4])
        fx = idx.get((pre, pr[2], v))
        if fx is None:
            cands = [(abs(val - v), fid) for (p, t, val), fid in idx.items() if p == pre and t == pr[2]]
            fx = min(cands)[1] if cands else 0
        out.append(fx)
    return out

def weapon_attachment_attach(st, req, ctx):
    q = req.post_pvt_weapon_attachment_attach
    body = P.Command_ApiNetwork_Api_PostPvtWeaponAttachmentAttachResponse()
    with st.transaction('weapon/attachment/attach') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        if not w:
            raise ValueError(f'weapon {q.weapon_id} not owned')
        cands = []
        total = 0
        for item_id, n in q.weapon_attachment_item_map.items():
            have = st.get('user_item_list', item_id)
            n = min(n, have.count if have else 0)
            if n <= 0:
                continue
            add_item(st, tx, item_id, -n)
            total += n
            for _ in range(n):
                fx = _roll_attachment(item_id)
                cands.append({'weapon_attachment_item_id': item_id, 'weapon_attachment_effect_ids': fx})
                e = body.weapon_attachment_item_list.add()
                e.weapon_attachment_item_id = item_id
                e.weapon_attachment_effect_id.extend(fx)
        if total:
            pr = st.get('user_play_record_list', PR_ATTACH) or st.new_row('user_play_record_list', play_record_type=PR_ATTACH)
            pr.int_value += total
            tx.upsert('user_play_record_list', pr)
        sel = st.new_row('user_weapon_attachment_effect_selecting_list', weapon_id=q.weapon_id, weapon_attachment_item=json.dumps({'weapon_attachment_item_list': cands}, separators=(',', ':')))
        tx.upsert('user_weapon_attachment_effect_selecting_list', sel)
        body.is_select_required = bool(cands)
    return _resp(tx, 'post_pvt_weapon_attachment_attach', body)

def weapon_attachment_effect_select(st, req, ctx):
    q = req.post_pvt_weapon_attachment_effect_select
    with st.transaction('weapon/attachment/effect/select') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        if not w:
            raise ValueError(f'weapon {q.weapon_id} not owned')
        ids = list(q.weapon_attachment_effect_ids)[:3]
        for i in range(3):
            if i < len(ids):
                setattr(w, f'weapon_attachment_effect_id{i}', ids[i])
            else:
                w.ClearField(f'weapon_attachment_effect_id{i}')
        tx.touch('user_weapon_list', w)
        st.tables.setdefault('user_weapon_attachment_effect_selecting_list', {}).clear()
    r = _resp(tx, 'post_pvt_weapon_attachment_effect_select')
    r.common.user.update.user_weapon_attachment_effect_selecting_list.add().user_id = st.user_id
    return r
HANDLERS.update({'/api/pvt/materia/enhance': materia_enhance, '/api/pvt/materia/evolve': materia_evolve, '/api/pvt/materia/sell': materia_sell, '/api/pvt/weapon/attachment/attach': weapon_attachment_attach, '/api/pvt/weapon/attachment/effect/select': weapon_attachment_effect_select})

_PROFILES = storage.path('saves', '_profiles', root=HERE)

def profile_get(st, req, ctx):
    duid = req.post_pvt_profile_get.display_user_id
    body = P.Command_ApiNetwork_Api_PostPvtProfileGetResponse()
    path = os.path.join(_PROFILES, f'{duid}.pb')
    if duid and os.path.isfile(path):
        try:
            body.profile_info.ParseFromString(open(path, 'rb').read())
            body.friend_count = body.profile_info.friend_count
        except Exception:
            pass
    with st.transaction('profile/get') as tx:
        pass
    return _resp(tx, 'post_pvt_profile_get', body)
HANDLERS['/api/pvt/profile/get'] = profile_get
def tutorial_skip(st, req, ctx):
    q = req.post_pvt_tutorial_skip
    from engine.tutorial_templates import response
    step = st.get('user_tutorial_step_list', _TUT_INITIAL_T)
    # A retry after completion must not award a second generated materia or gifts.
    if step is not None and step.current_step >= _TUT_INITIAL_DONE:
        profile = st.one('user_profile_list') or st.new_row('user_profile_list', is_show_character_rank=True)
        profile.name, profile.message = q.user_name, q.message
        with st.transaction('tutorial/skip') as tx:
            tx.upsert('user_profile_list', profile)
        ctx['new_name'] = q.user_name
        r = _resp(tx)
        r.post_pvt_tutorial_skip.SetInParent()
        return r
    r = response('skip', st, q, now_ms(), new_id, ctx)
    upd = r.common.user.update
    if not len(upd.user_profile_list):
        p = upd.user_profile_list.add()
        p.user_id = st.user_id
        p.is_show_character_rank = True
    for p in upd.user_profile_list:
        p.user_id = st.user_id
        p.name = q.user_name
        p.message = q.message
    with st.transaction('tutorial/skip') as tx:
        for f, rows in upd.ListFields():
            for row in rows:
                cp = type(row)()
                cp.CopyFrom(row)
                tx.upsert(f.name, cp)
        _skip_story_gifts(st, ctx, upd)
    ctx['new_name'] = q.user_name
    return r
HANDLERS['/api/pvt/tutorial/skip'] = tutorial_skip

def _skip_story_gifts(st, ctx, upd):
    # the real skip reply sets has_gift: the prologue's first-clear crystals wait in the gift box
    rw = {r[0]: r for r in md('m_reward')}
    obt = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_FirstStoryParty')
    tmp = P.Command_ApiNetwork_Api_PostPvtStoryResultResponse()
    for e in upd.user_episode_list:
        ep = mdx('m_story_episode', 0).get((e.story_episode_id,))
        for rid, n in _set_rewards(ep[12] if ep else 0):
            r = rw.get(rid)
            if r and r[1] == RT_STONE:
                ri = tmp.story_party_first_reward_infos.add()
                ri.reward_type, ri.target_id, ri.count = r[1], r[2], n
                _gift_reward(st, ctx, ri, obt)
_CRISIS_STAGE = None

def _crisis_stage(esb_id):
    global _CRISIS_STAGE
    if _CRISIS_STAGE is None:
        _CRISIS_STAGE = {r[0]: r for r in md('m_event_crisis_battle_solo_battle')}
    return _CRISIS_STAGE.get(esb_id)
_B32 = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

def _syn_duid(uid):
    n = int(uid)
    s = ''
    for _ in range(13):
        s = _B32[n % 32] + s
        n //= 32
    return s

def _crisis_load(st):
    if not st.folder:
        return {}
    from engine.vault import board_all
    return board_all(os.path.dirname(st.folder), 'crisis')

def _crisis_save(st, board):
    if not st.folder:
        return
    from engine.vault import board_put
    board_put(os.path.dirname(st.folder), 'crisis', st.user_id, board.get(str(st.user_id), {}))

def _crisis_profile(st):
    try:
        prof = next(iter(st['user_profile_list'].values()), None)
    except Exception:
        prof = None
    if prof:
        return (prof.name, [prof.top_honor_id, prof.bottom_honor_id, prof.background_honor_id, prof.icon_honor_id])
    return ('', [0, 0, 0, 0])

def _crisis_ack(st, field):
    with st.transaction(field) as tx:
        pass
    return _resp(tx, field)

def _rank_user_info(dst, e, rank):
    ri = dst.add()
    ri.display_user_id = e.get('duid', '')
    ri.player_name = e.get('name', '')
    hon = (e.get('hon') or [0, 0, 0, 0]) + [0, 0, 0, 0]
    ri.top_honor_id, ri.bottom_honor_id, ri.background_honor_id, ri.icon_honor_id = (hon[0], hon[1], hon[2], hon[3])
    ri.rank = rank
    ri.score = e.get('pt', 0)

def event_crisis_battle_current_rank(st, req, ctx):
    ecb_id = req.post_pvt_event_crisis_battle_current_rank.event_crisis_battle_id
    rows = _crisis_ranked(_crisis_load(st), ecb_id)
    rank = next((i + 1 for i, (u, e) in enumerate(rows) if u == str(st.user_id)), 0)
    r = _crisis_ack(st, 'post_pvt_event_crisis_battle_current_rank')
    r.post_pvt_event_crisis_battle_current_rank.rank = rank
    return r

def event_crisis_battle_group_ranking(st, req, ctx):
    q = req.post_pvt_event_crisis_battle_group_ranking
    rows = _crisis_ranked(_crisis_load(st), q.event_crisis_battle_id)
    r = _crisis_ack(st, 'post_pvt_event_crisis_battle_group_ranking')
    body = r.post_pvt_event_crisis_battle_group_ranking
    start = q.start_rank or 1
    for i, (u, e) in enumerate(rows):
        if i + 1 < start:
            continue
        _rank_user_info(body.ranking_user_infos, e, i + 1)
        if len(body.ranking_user_infos) >= 50:
            break
    body.max_rank = len(rows)
    return r

def _friend_rows(st, rows):
    """The Friends tab of a ranking (#66): the player and their friends only, in board order."""
    keep = {str(x) for x in _me(_soc(), str(st.user_id))['friends']} | {str(st.user_id)}
    return [(u, e) for u, e in rows if str(u) in keep]

def event_crisis_battle_friend_ranking(st, req, ctx):
    ecb_id = req.post_pvt_event_crisis_battle_friend_ranking.event_crisis_battle_id
    rows = _crisis_ranked(_crisis_load(st), ecb_id)
    r = _crisis_ack(st, 'post_pvt_event_crisis_battle_friend_ranking')
    body = r.post_pvt_event_crisis_battle_friend_ranking
    for i, (u, e) in enumerate(_friend_rows(st, rows)[:50]):
        _rank_user_info(body.ranking_user_infos, e, i + 1)
    return r

def event_crisis_battle_ranking_reward_receive(st, req, ctx):
    ecb_id = req.post_pvt_event_crisis_battle_ranking_reward_receive.event_crisis_battle_id
    rows = _crisis_ranked(_crisis_load(st), ecb_id)
    rank = next((i + 1 for i, (u, e) in enumerate(rows) if u == str(st.user_id)), 0)
    with st.transaction('event/crisis/battle/ranking/reward/receive') as tx:
        r = _resp(tx, 'post_pvt_event_crisis_battle_ranking_reward_receive')
        body = r.post_pvt_event_crisis_battle_ranking_reward_receive
        body.rank = rank
        if rank > 0:
            ecb = st.get('user_event_crisis_battle_list', ecb_id) or st.new_row('user_event_crisis_battle_list', event_crisis_battle_id=ecb_id)
            if not ecb.is_received_ranking_reward:
                tiers = sorted((row for row in md('m_event_crisis_battle_ranking') if row[0] == ecb_id), key=lambda x: -x[1])
                rs = next((row[2] for row in tiers if rank >= row[1]), None)
                if rs:
                    grant_reward_set(st, tx, rs, 1, body.reward_info_list)
                ecb.is_received_ranking_reward = True
                ecb.final_rank = rank
                tx.upsert('user_event_crisis_battle_list', ecb)
    return r
HANDLERS.update({'/api/pvt/event/crisis/battle/current/rank': event_crisis_battle_current_rank, '/api/pvt/event/crisis/battle/group/ranking': event_crisis_battle_group_ranking, '/api/pvt/event/crisis/battle/friend/ranking': event_crisis_battle_friend_ranking, '/api/pvt/event/crisis/battle/ranking/reward/receive': event_crisis_battle_ranking_reward_receive})

def _hw_jump_cost(set_ids):
    from collections import Counter
    times = Counter((s for s in set_ids if s))
    rw = {r[0]: r for r in md('m_reward')}
    bexp = {b[0]: b[1] for b in md('m_reward_big_item')}
    need = {}
    for rel in md('m_consumption_set_consumption_rel'):
        k = times.get(rel[1])
        if not k:
            continue
        r = rw.get(rel[2])
        n = rel[3] * k
        if not r or not n:
            continue
        if r[1] == RT_BIGITEM:
            e = bexp.get(r[0], 0)
            key = ('big', r[2])
            amt = n * 10 ** e if e >= 0 else n / 10 ** (-e)
        elif r[1] == RT_STONE:
            key = ('stone', r[2])
            amt = n
        else:
            key = ('item', r[2])
            amt = n
        need[key] = need.get(key, 0) + amt
    return need

def _hw_have(st, kind, i):
    if kind == 'big':
        r = st.get('user_big_item_list', i)
        if not r:
            return 0
        return r.count_significand * 10 ** r.count_exponent if r.count_exponent >= 0 else r.count_significand / 10 ** (-r.count_exponent)
    if kind == 'stone':
        s = {x.stone_id: x.count for x in st.other_info.user_stone_list}
        return s.get(i, 0) + (s.get(2, 0) if i == 1 else 0)
    r = st.get('user_item_list', i)
    return r.count if r else 0

def highwind_parts_upgrade(st, req, ctx):
    q = req.post_pvt_highwind_parts_upgrade
    body = P.Command_ApiNetwork_Api_PostPvtHighwindPartsUpgradeResponse()
    pid, want = (q.highwind_parts_id, q.level)

    def lv(i):
        r = st.get('user_highwind_parts_list', i)
        return r.level if r else 1
    cur = lv(pid)
    if want <= cur:
        raise ValueError(f'highwind part {pid}: target {want} is not above current {cur}')
    if pid != 1 and want > lv(1):
        raise ValueError(f"highwind part {pid}: target {want} is above the Highwind's level {lv(1)}")
    up = mdx('m_highwind_parts_upgrade', 0, 1)
    rows = [up.get((pid, L)) for L in range(cur + 1, want + 1)]
    if not all(rows):
        raise ValueError(f'no upgrade rows for highwind part {pid} levels {cur + 1}..{want}')
    for (kind, i), amt in _hw_jump_cost([r[4] for r in rows]).items():
        have = _hw_have(st, kind, i)
        if have < amt:
            raise ValueError(f'highwind part {pid} -> {want}: need {amt} of {kind} {i}, have {have}')
    with st.transaction('highwind/parts/upgrade') as tx:
        for row in rows:
            if row[4]:
                consume_set(st, tx, row[4])
        p = st.get('user_highwind_parts_list', pid) or st.new_row('user_highwind_parts_list', highwind_parts_id=pid)
        p.level = want
        tx.upsert('user_highwind_parts_list', p)
        _hw_parts_missions(st, tx)
    return _resp(tx, 'post_pvt_highwind_parts_upgrade', body)

def _hw_parts_missions(st, tx):
    lv = {pid: 1 for pid in (1, 2, 3, 4)}
    lv.update({r.highwind_parts_id: r.level for r in st['user_highwind_parts_list'].values()})
    for pid, L in lv.items():
        mission_event(st, tx, 'ProgressHighwindPartsLevel', L, pid)
    mission_event(st, tx, 'ProgressHighwindPartsLevelAll', sum(lv.values()))
HANDLERS['/api/pvt/highwind/parts/upgrade'] = highwind_parts_upgrade
import collections
_GB = {}

def _set_need(set_id, acc, times=1):
    rw = {r[0]: r for r in md('m_reward')}
    for rel in md('m_consumption_set_consumption_rel'):
        if rel[1] != set_id:
            continue
        r = rw.get(rel[2])
        n = rel[3] * times
        if not r or not n:
            continue
        if r[1] == RT_STONE:
            acc['stone',] += n
        elif r[1] == RT_BIGITEM:
            acc['big', r[2]] += n
        else:
            acc['item', r[2]] += n

def _summon_mem_cost(n):
    g = _GB['group'].get(n[15])
    if not g or g[3] != 4 or (not n[14]):
        return None
    s = _GB.setdefault('summon', {r[0]: r for r in md('m_summon')}).get(g[1])
    return (s[1], n[14]) if s else None

def _can_pay(st, need):
    for k, n in need.items():
        if k[0] == 'item':
            row = st.get('user_item_list', k[1])
            have = row.count if row else 0
        elif k[0] == 'stone':
            have = sum((s.count for s in st.other_info.user_stone_list if s.stone_id in (1, 2)))
        else:
            row = st.get('user_big_item_list', k[1])
            have = row.count_significand * 10 ** row.count_exponent if row else 0
        if have < n:
            return False
    return True

def _gb_load():
    if 'node' not in _GB:
        _GB['node'] = {r[0]: r for r in md('m_growth_board_node')}
        _GB['group'] = {r[0]: r for r in md('m_growth_board_group')}

def _lb_level(st, sk, rows=None):
    """An LB's level = 1 + its opened stream "Lv. Up" nodes (m_growth_board_node col 3 = special_skill_id)."""
    _gb_load()
    if 'lbn' not in _GB:
        ids = {r[0] for r in md('m_skill_special')}
        idx = collections.defaultdict(list)
        for n in _GB['node'].values():
            if n[3] in ids:
                idx[n[3]].append(n)
        _GB['lbn'] = idx
    lv = 1
    for n in _GB['lbn'].get(sk, ()):
        row = (rows or {}).get(n[15]) or st.get('user_growth_board_group_list', n[15])
        if row and row.group_idx_flags0 >> n[16] & 1:
            lv += 1
    return lv

def _lb_missions_heal(st, tx, rows=None):
    """ProgressLimitBreakLevel missions ("Raise X to Lv. N", the 3rd LB unlock) track the LB's real level. Nothing
    else moved them past 1, so LB 3 stayed locked with LB 2 at Lv 4."""
    ms = _mt_index().get(MTYPE.get('ProgressLimitBreakLevel', -1), [])
    if not ms:
        return 0
    owned = {k[0] if isinstance(k, tuple) else k for k in st['user_skill_special_list'].keys()}
    t = now_ms()
    done = 0
    for m in ms:
        if m[5] not in owned:
            continue
        lv = _lb_level(st, m[5], rows)
        row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
        if row.progress_count >= lv:
            continue
        old = row.progress_count
        row.progress_count = lv
        _announce_step(st, tx, m, row, old, t)
        tx.upsert('user_mission_list', row)
        done += 1
    return done

def _stream_nodes_open(st, cid, rows=None):
    """Nodes open in a character's Character Stream, all sheets (m_growth_board_group col 3 = 1 Character,
    col 1 = the character): the stream screen's "Nodes Unlocked"."""
    _gb_load()
    if 'cn' not in _GB:
        idx = collections.defaultdict(list)
        for n in _GB['node'].values():
            g = _GB['group'].get(n[15])
            if g and g[3] == 1:
                idx[g[1]].append((n[15], n[16]))
        _GB['cn'] = idx
    k = 0
    for gid, i in _GB['cn'].get(cid, ()):
        row = (rows or {}).get(gid) or st.get('user_growth_board_group_list', gid)
        if row and row.group_idx_flags0 >> i & 1:
            k += 1
    return k

def _stream_node_missions_heal(st, tx, rows=None):
    """ProgressGrowthBoardCharacterOpen missions about ONE character, in a group with no end date (the Overspeed
    unlock "Unlock 100 node(s) in X's character stream", the Normal 20 / 50 ones) = the nodes open in that stream.
    A group with an end date (events, panels) counts only what is opened while it runs, so it is left to the event."""
    ms = _mt_index().get(MTYPE.get('ProgressGrowthBoardCharacterOpen', -1), [])
    groups = _mt_index()['groups']
    t = now_ms()
    done = 0
    for m in ms:
        if not m[5] or m[3] == 3:
            continue
        g = groups.get(_set_group(m[1]))
        if not g or g[8]:
            continue
        k = _stream_nodes_open(st, m[5], rows)
        row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
        if row.progress_count >= k:
            continue
        old = row.progress_count
        row.progress_count = k
        _announce_step(st, tx, m, row, old, t)
        tx.upsert('user_mission_list', row)
        done += 1
    return done

def _summon_missions_heal(st, tx, rows=None):
    """Summon missions follow the account (nothing raised them, so a summon's Pact "Raise Hellfire to Lv. 5" and the
    summon achievements stayed at 0):
      12 ProgressSummonSkillLevel [skill]  = 1 + the opened summon-board nodes that level it (node col 3, as for LBs)
      10 ProgressSummonBoardOpen [summon, 0 = all] = nodes open on that summon's boards (m_growth_board_group col 3 = 4)
       8 ProgressSummonGet                 = summons owned
       9 SetSummonSkillGet [skill]         = set mission, achieved once the skill is owned
    Only a summon the player owns counts. A group with an end date counts only what happens while it runs, so it is
    left to the events growth_board_open and mission_special_receive send. Only ever raised."""
    _gb_load()
    if 'sn' not in _GB:
        sn = collections.defaultdict(list)
        sk = collections.defaultdict(list)
        owner = {r[0]: r[2] for r in md('m_skill_special') if r[1] == SKILL_SPECIAL_SUMMON}
        for n in _GB['node'].values():
            g = _GB['group'].get(n[15])
            if g and g[3] == GB_SUMMON:
                sn[g[1]].append((n[15], n[16]))
                if n[3] in owner:
                    sk[n[3]].append((n[15], n[16]))
        _GB['sn'], _GB['sk'], _GB['sko'] = sn, sk, owner

    def opened(pairs):
        k = 0
        for gid, i in pairs:
            row = (rows or {}).get(gid) or st.get('user_growth_board_group_list', gid)
            if row and row.group_idx_flags0 >> i & 1:
                k += 1
        return k
    owned = {k[0] if isinstance(k, tuple) else k for k in st['user_summon_list'].keys()}
    nodes = {s: opened(_GB['sn'].get(s, ())) for s in owned}
    level = {k: 1 + opened(_GB['sk'].get(k, ())) for k, s in _GB['sko'].items() if s in owned}
    value = {MTYPE.get('ProgressSummonSkillLevel'): lambda m: level.get(m[5], 0),
             MTYPE.get('ProgressSummonBoardOpen'): lambda m: nodes.get(m[5], 0) if m[5] else sum(nodes.values()),
             MTYPE.get('ProgressSummonGet'): lambda m: len(owned)}
    groups = _mt_index()['groups']
    t = now_ms()
    done = 0
    for mt, val in value.items():
        for m in _mt_index().get(mt, []) if mt else ():
            gid = _set_group(m[1])
            g = groups.get(gid)
            if m[3] == 3 or not g or g[8] or not _group_open(gid, t):
                continue
            if _is_guide(gid) and (not _mission_window_ok(m, t, True)):
                continue
            v = val(m)
            row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
            if not v or row.progress_count >= v:
                continue
            old = row.progress_count
            row.progress_count = v
            _announce_step(st, tx, m, row, old, t)
            tx.upsert('user_mission_list', row)
            done += 1
    skills = {k[0] if isinstance(k, tuple) else k for k in st['user_skill_special_list'].keys()}
    for m in _mt_index().get(MTYPE.get('SetSummonSkillGet', -1), []):
        gid = _set_group(m[1])
        if m[3] != 3 or m[5] not in skills or not _group_open(gid, t):
            continue
        if (_is_guide(gid) or _mission_window_ok(m, t, False)) and _set_mission_achieve(st, tx, m, t):
            done += 1
    return done
GB_SUMMON = 4               # m_growth_board_group col 3 (GrowthBoardType): 1 Character, 4 Summon
SKILL_SPECIAL_SUMMON = 2    # m_skill_special col 1 (SkillSpecialType); col 2 = the summon

def _stream_missions_heal(st):
    """At login: LB level and stream node-count missions catch up with what is already open, so a save from before
    this fix doesn't have to open one more node first."""
    _gb_load()
    with st.transaction('title: stream missions') as tx:
        a = _lb_missions_heal(st, tx)
        b = _stream_node_missions_heal(st, tx)
        c = _summon_missions_heal(st, tx)
    return a + b + c

def growth_board_open(st, req, ctx):
    _gb_load()
    q = req.post_pvt_character_growth_board_open
    body = P.Command_ApiNetwork_Api_PostPvtCharacterGrowthBoardOpenResponse()
    with st.transaction('character/growth/board/open') as tx:
        todo = []
        need = collections.Counter()
        for nid in q.node_ids:
            n = _GB['node'].get(nid)
            if not n:
                continue
            row = st.get('user_growth_board_group_list', n[15])
            if row and row.group_idx_flags0 >> n[16] & 1:
                continue
            todo.append(n)
            if n[19]:
                _set_need(n[19], need)
            sc = _summon_mem_cost(n)
            if sc:
                need['item', sc[0]] += sc[1]
        if need and (not _can_pay(st, need)):
            return _resp(tx, 'post_pvt_character_growth_board_open', body)
        rows = {}
        opened = collections.Counter()
        by_char = collections.Counter()
        by_summon = collections.Counter()
        for n in todo:
            gid, idx = (n[15], n[16])
            row = rows.get(gid) or st.get('user_growth_board_group_list', gid) or st.new_row('user_growth_board_group_list', growth_board_group_id=gid)
            row.group_idx_flags0 = _flag_or(row.group_idx_flags0, idx)
            rows[gid] = row
            if n[19]:
                consume_set(st, tx, n[19])
            sc = _summon_mem_cost(n)
            if sc:
                add_item(st, tx, sc[0], -sc[1])
            g = _GB['group'].get(gid)
            opened[g[3] if g else 0] += 1
            if g and g[3] == 1:
                by_char[g[1]] += 1
            if g and g[3] == GB_SUMMON:
                by_summon[g[1]] += 1
        for row in rows.values():
            tx.upsert('user_growth_board_group_list', row)
        ts = st.get('user_tutorial_step_list', 13)
        if rows and (ts is None or ts.current_step < 1):
            ts = ts or st.new_row('user_tutorial_step_list', tutorial_step_type=13)
            ts.current_step = 1
            tx.upsert('user_tutorial_step_list', ts)
        # one event per character: a mission naming a character (c5) only moves for that character's nodes
        for cid, k in by_char.items():
            mission_event(st, tx, 'ProgressGrowthBoardCharacterOpen', k, cid)
        for sid, k in by_summon.items():    # groups with an end date; the heal below raises the others to the count
            mission_event(st, tx, 'ProgressSummonBoardOpen', k, sid)
        _lb_missions_heal(st, tx, rows)
        _stream_node_missions_heal(st, tx, rows)
        if by_summon:
            _summon_missions_heal(st, tx, rows)
    return _resp(tx, 'post_pvt_character_growth_board_open', body)
HANDLERS['/api/pvt/character/growth/board/open'] = growth_board_open

STORY_FIRST_MSG = 991000000000034

def _gift_reward(st, ctx, ri, obtain, msg=STORY_FIRST_MSG):
    ri.is_gift = True
    if st.other_info is not None:
        st.other_info.has_gift = True
    gi = ri.gift_info
    gi.user_gift_id = new_id()
    gi.message_template_language_id = msg
    gi.reward_obtain_type = obtain
    gi.received_datetime = now_ms()
    gi.expire_datetime = GIFT_NO_EXPIRE
    g = _M2D(ri, preserving_proto_field_name=True)
    p = _gifts_path(st)
    if p and os.path.isfile(p):
        box = _gifts_load(st)
        box.insert(0, g)
        _gifts_save(st, box)
    elif st.folder:
        q = os.path.join(st.folder, 'inbox_overflow.json')
        pend = json.load(open(q)) if os.path.isfile(q) else []
        pend.insert(0, g)
        tmp = q + '.tmp'
        json.dump(pend, open(tmp, 'w'), indent=0)
        os.replace(tmp, q)
BATTLE_FIRST_MSG = 991000000000011

TOWER_FIRST_OBTAIN = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_TowerFloorFirst')
TOWER_FIRST_MSG = 991000000000055     # m_gift_message 55: "A reward for clearing the Battle Tower for the first time."

def _battle_first_to_gifts(st, tx, ctx, infos, obtain=None, msg=BATTLE_FIRST_MSG):
    obt = obtain or P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_Battle')
    for ri in infos:
        if ri.reward_type != RT_STONE or ri.is_gift:
            continue
        give_stone(st, tx, ri.target_id, -ri.count)
        _gift_reward(st, ctx, ri, obt, msg)

def _set_rewards(set_id, times=1, depth=0):
    out = []
    if depth > 4 or not set_id:
        return out
    rels = [r for r in md('m_reward_set_reward_rel') if r[1] == set_id]
    rs = mdx('m_reward_set', 0).get((set_id,))
    for _ in range(times):
        for rel in [random.choice(rels)] if rs and rs[1] in (2, 3) and rels else rels:
            n = random.randint(rel[4], rel[5]) if rel[5] > rel[4] else rel[4]
            if n:
                out.append((rel[2], n))
    for rel in md('m_reward_set_reward_set_rel'):
        if rel[1] == set_id:
            out += _set_rewards(rel[2], times, depth + 1)
    return out

def _first_rewards(st, tx, ctx, set_id, times, infos, obtain, msg=STORY_FIRST_MSG):
    rw = {r[0]: r for r in md('m_reward')}
    for rid, n in _set_rewards(set_id, times):
        r = rw.get(rid)
        if not r:
            continue
        if r[1] != RT_STONE:
            grant_reward(st, tx, rid, n, infos)
            continue
        ri = infos.add()
        ri.reward_type = r[1]
        ri.target_id = r[2]
        ri.count = n
        _gift_reward(st, ctx, ri, obtain, msg)

def story_result(st, req, ctx):
    q = req.post_pvt_story_result
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtStoryResultResponse()
    with st.transaction('story/result') as tx:
        ep = mdx('m_story_episode', 0).get((q.episode_id,))
        row = st.get('user_episode_list', q.episode_id) or st.new_row('user_episode_list', story_episode_id=q.episode_id)
        free = q.story_mode_type == 2
        first = not (row.free_party_mode_clear_datetime if free else row.story_party_mode_clear_datetime)
        if free:
            row.free_party_mode_clear_datetime = now
        else:
            row.story_party_mode_clear_datetime = now
        tx.upsert('user_episode_list', row)
        mission_event(st, tx, 'SetStoryReadEpisode', 1, q.episode_id)
        _ste_credit(st, tx, q.episode_id, body.reward_infos)
        if first and ep:
            obt = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_FirstStoryParty')
            if free and ep[6]:
                _first_rewards(st, tx, ctx, ep[6], max(1, ep[7]), body.reward_infos, obt)
            elif not free and ep[12]:
                _first_rewards(st, tx, ctx, ep[12], 1, body.story_party_first_reward_infos, obt)
            elif not free and ep[6]:
                _first_rewards(st, tx, ctx, ep[6], max(1, ep[7]), body.reward_infos, obt)
    return _resp(tx, 'post_pvt_story_result', body)
HANDLERS['/api/pvt/story/result'] = story_result
DUNGEON_STORY = 1
TREASURE_MSG = 991000000000046

def dungeon_story_start(st, req, ctx):
    q = req.post_pvt_dungeon_story_start
    t = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtDungeonStoryStartResponse()
    with st.transaction('dungeon/story/start') as tx:
        pi = q.party_info
        if pi.party_id:
            party = st.get('user_party_list', pi.party_id) or st.new_row('user_party_list', party_id=pi.party_id)
            party.combat_power = pi.combat_power
            party.is_active = True
            if pi.HasField('memoria_info'):
                for f in party.DESCRIPTOR.fields:
                    if f.name.startswith('memoria_id'):
                        party.ClearField(f.name)
                for f, v in pi.memoria_info.ListFields():
                    if hasattr(party, f.name):
                        setattr(party, f.name, v)
            tx.upsert('user_party_list', party)
            for m in pi.user_party_member_list:
                row = P.Command_Entity_UserPartyMember()
                row.CopyFrom(m)
                row.user_id = st.user_id
                row.updated_datetime = t
                tx.upsert('user_party_member_list', row)
        ee = st.one('user_episode_entry_list') or st.new_row('user_episode_entry_list')
        ee.party_id = q.party_id
        ee.episode_id = q.episode_id
        ee.story_mode_type = q.story_mode_type
        tx.upsert('user_episode_entry_list', ee)
        ep = mdx('m_story_episode', 0).get((q.episode_id,))
        de = st.get('user_dungeon_entry_list', DUNGEON_STORY) or st.new_row('user_dungeon_entry_list', dungeon_type=DUNGEON_STORY)
        de.party_id = q.party_id
        de.entry_count += 1
        de.dungeon_id = ep[4] if ep else 0
        de.content_id = q.episode_id
        de.dungeon_progress_type = DUNGEON_START
        de.dungeon_result_type = 0
        de.chaser_encount_count = 0
        tx.upsert('user_dungeon_entry_list', de)
        body.entry_count = de.entry_count
    return _resp(tx, 'post_pvt_dungeon_story_start', body)

def dungeon_story_treasure(st, req, ctx):
    q = req.post_pvt_dungeon_story_treasure
    body = P.Command_ApiNetwork_Api_PostPvtDungeonStoryTreasureResponse()
    with st.transaction('dungeon/story/treasure') as tx:
        t = mdx('m_dungeon_treasure', 0).get((q.dungeon_treasure_id,))
        row = st.get('user_dungeon_treasure_list', q.dungeon_treasure_id)
        if t and t[2] and (not (row and t[5] and row.entry_count)):
            obt = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_TreasureStory')
            _first_rewards(st, tx, ctx, t[2], 1, body.reward_infos, obt, TREASURE_MSG)
        row = row or st.new_row('user_dungeon_treasure_list', dungeon_treasure_id=q.dungeon_treasure_id)
        row.entry_count = q.entry_count
        tx.upsert('user_dungeon_treasure_list', row)
    return _resp(tx, 'post_pvt_dungeon_story_treasure', body)

def dungeon_story_end(st, req, ctx):
    q = req.post_pvt_dungeon_story_end
    body = P.Command_ApiNetwork_Api_PostPvtDungeonStoryEndResponse()
    with st.transaction('dungeon/story/end') as tx:
        de = st.get('user_dungeon_entry_list', DUNGEON_STORY)
        if de:
            de.dungeon_progress_type = DUNGEON_END
            tx.touch('user_dungeon_entry_list', de)
    return _resp(tx, 'post_pvt_dungeon_story_end', body)
HANDLERS.update({'/api/pvt/dungeon/story/start': dungeon_story_start, '/api/pvt/dungeon/story/treasure': dungeon_story_treasure, '/api/pvt/dungeon/story/end': dungeon_story_end})
AREA_SOLO = P.Command_Enums_BattleType.Value('Command_Enums_BattleType_AreaSolo')
EVENT_SOLO = P.Command_Enums_BattleType.Value('Command_Enums_BattleType_EventSolo')
DAILY_QUEST = P.Command_Enums_BattleType.Value('Command_Enums_BattleType_DailyQuest')

def _all_beaten(battle_id, into):
    for w in _waves(battle_id):
        er = into.add()
        er.wave_idx = w[1]
        er.enemy_group_id = w[2]
        er.win_enemy_idxs.extend(sorted({en[1] for en in md('m_battle_enemy') if en[0] == w[2]}))

def battle_skip(st, req, ctx):
    q = req.post_pvt_battle_skip
    if q.battle_type == DAILY_QUEST:
        return daily_quest_skip(st, req, ctx)
    if q.battle_type not in (AREA_SOLO, EVENT_SOLO):
        raise NotImplementedError(f'battle/skip type {q.battle_type}')
    last = st.one('user_battle_entry_list')
    party = last.party_id if last and last.party_id else next((p.party_id for p in st['user_party_list'].values() if p.is_active), 0)
    body = P.Command_ApiNetwork_Api_PostPvtBattleSkipResponse()
    s = P.Command_ApiNetwork_Api_ApiRequest()
    e = P.Command_ApiNetwork_Api_ApiRequest()
    if q.battle_type == AREA_SOLO:
        a = _area_battle(q.target_id)
        bid = a[4] if a else q.target_id
        row = st.get('user_solo_area_battle_list', q.target_id)
        s.post_pvt_area_solo_battle_start.area_battle_id = q.target_id
        s.post_pvt_area_solo_battle_start.party_id = party
        area_solo_battle_start(st, s, ctx)
        en = e.post_pvt_area_solo_battle_end
        en.area_battle_id = q.target_id
        en.entry_count = st.one('user_battle_entry_list').entry_count
        en.battle_result_type = WIN
        en.battle_input.total_damage = row.high_score if row else 0
        _all_beaten(bid, en.battle_enemy_results)
        _SKIP_ALL_CHARS.on = True
        try:
            r = area_solo_battle_end(st, e, ctx)
        finally:
            _SKIP_ALL_CHARS.on = False
        src = r.post_pvt_area_solo_battle_end
    else:
        stage = _stage(q.target_id)
        bid = stage[5] if stage else q.target_id
        row = st.get('user_event_solo_battle_list', q.target_id)
        s.post_pvt_event_solo_battle_start.event_battle_id = q.target_id
        s.post_pvt_event_solo_battle_start.party_id = party
        event_solo_battle_start(st, s, ctx)
        en = e.post_pvt_event_solo_battle_end
        en.event_battle_id = q.target_id
        en.entry_count = st.one('user_battle_entry_list').entry_count
        en.battle_result_type = WIN
        en.battle_input.total_damage = row.high_score if row else 0
        _all_beaten(bid, en.battle_enemy_results)
        _SKIP_ALL_CHARS.on = True
        try:
            r = event_solo_battle_end(st, e, ctx)
        finally:
            _SKIP_ALL_CHARS.on = False
        src = r.post_pvt_event_solo_battle_end
        if stage:
            with st.transaction('battle/skip') as tx2:
                area = mdx('m_event_solo_area', 0).get((stage[2],))
                # a list card holds other events' areas: the daily count stays with the stage's own event
                ev_id = q.target_id // 1000 if not area or area[1] in _list_card_ids() else area[1]
                eb = st.get('user_event_base_list', ev_id) or st.new_row('user_event_base_list', event_base_id=ev_id)
                _daily_count(eb, 'reset_battle_win_count', 'last_win_reset_datetime', now_ms())
                tx2.upsert('user_event_base_list', eb)
            r.common.user.update.user_event_base_list.append(eb)
    body.battle_result.CopyFrom(src.battle_result)
    for f in ('before_exp', 'after_exp'):
        if hasattr(src, f):
            setattr(body, f, getattr(src, f))
    if hasattr(src, 'event_bonus_reward_infos'):
        body.event_bonus_reward_infos.extend(src.event_bonus_reward_infos)
    out = P.Command_ApiNetwork_Api_ApiResponse()
    out.common.CopyFrom(r.common)
    out.post_pvt_battle_skip.CopyFrom(body)
    return out
HANDLERS['/api/pvt/battle/skip'] = battle_skip
_OFFLINE_RECIPE = {}

def _offline_recipe():
    """engine/data/masterdata-patches.json, read once ({} when it can't be read)."""
    if 'recipe' not in _OFFLINE_RECIPE:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'masterdata-patches.json'), encoding='utf-8-sig') as f:
                _OFFLINE_RECIPE['recipe'] = json.load(f)
        except (OSError, ValueError) as e:
            print(f'offline recipe unreadable: {e!r}')
            _OFFLINE_RECIPE['recipe'] = {}
    return _OFFLINE_RECIPE['recipe']

def _list_card_ids():
    """Ids the generated cards own (engine/cards.py): an area under one of them belongs to another event."""
    from engine import cards
    try:
        return cards.generated_ids(_offline_recipe())
    except (KeyError, TypeError):
        return frozenset()

def _merge_infos(infos):
    keep = []
    seen = {}
    for ri in infos:
        k = (ri.reward_type, ri.target_id)
        if not ri.HasField('reward_option') and (not ri.is_gift) and (k in seen):
            seen[k].count += ri.count
            continue
        c = P.Command_Entity_RewardInfo()
        c.CopyFrom(ri)
        keep.append(c)
        if not ri.HasField('reward_option') and (not ri.is_gift):
            seen[k] = c
    del infos[:]
    infos.extend(keep)

def _profile(st):
    return st.one('user_profile_list') or st.new_row('user_profile_list')

def honor_setting(st, req, ctx):
    q = req.post_pvt_honor_setting
    with st.transaction('honor/setting') as tx:
        p = _profile(st)
        changed = any((getattr(p, f) != getattr(q, f) for f in ('top_honor_id', 'bottom_honor_id', 'background_honor_id', 'icon_honor_id')))
        for f in ('top_honor_id', 'bottom_honor_id', 'background_honor_id', 'icon_honor_id'):
            setattr(p, f, getattr(q, f))
        tx.upsert('user_profile_list', p)
        mission_event(st, tx, 'SetProfileUpdate', 1)
        if changed:
            mission_event(st, tx, 'SetProfileHonorUpdate', 1)
    return _resp(tx, 'post_pvt_honor_setting', P.Command_ApiNetwork_Api_PostPvtHonorSettingResponse())

def profile_update(st, req, ctx):
    q = req.post_pvt_profile_update
    with st.transaction('profile/update') as tx:
        p = _profile(st)
        if q.user_name:
            p.name = q.user_name
        p.message = q.message
        if q.message:
            mission_event(st, tx, 'SetProfileMessageUpdate', 1)   # "Write a self-introduction"
        if hasattr(p, 'show_character_rank_flag'):
            p.show_character_rank_flag = q.show_character_rank_flag
        tx.upsert('user_profile_list', p)
    return _resp(tx, 'post_pvt_profile_update', P.Command_ApiNetwork_Api_PostPvtProfileUpdateResponse())

def home_background_setting(st, req, ctx):
    q = req.post_pvt_user_home_background_setting
    with st.transaction('user/home/background/setting') as tx:
        s = st.one('user_home_background_setting_list') or st.new_row('user_home_background_setting_list')
        s.is_random = q.is_random
        for i in range(4):
            setattr(s, f'home_background_id{i}', getattr(q, f'home_background_id{i}'))
        tx.upsert('user_home_background_setting_list', s)
    return _resp(tx, 'post_pvt_user_home_background_setting', P.Command_ApiNetwork_Api_PostPvtUserHomeBackgroundSettingResponse())

def user_setting(st, req, ctx):
    q = req.post_pvt_user_setting
    with st.transaction('user/setting') as tx:
        for s in q.setting_list:
            row = st.get('user_setting_list', s.setting_type) or st.new_row('user_setting_list', setting_type=s.setting_type)
            row.value = s.value
            tx.upsert('user_setting_list', row)
            if s.value == 'true':
                mission_event(st, tx, 'SetUserSetting', 1, s.setting_type)
    return _resp(tx, 'post_pvt_user_setting', P.Command_ApiNetwork_Api_PostPvtUserSettingResponse())

def item_box_open_choice(st, req, ctx):
    q = req.post_pvt_item_box_open_choice
    body = P.Command_ApiNetwork_Api_PostPvtItemBoxOpenChoiceResponse()
    with st.transaction('item/box/open/choice') as tx:
        ch = {r[0]: r for r in md('m_item_box_choice_reward')}
        for cid, n in q.item_box_choice_reward_ids_map.items():
            c = ch.get(cid)
            if not c or n <= 0:
                continue
            add_item(st, tx, q.item_id, -n)
            grant_reward_set(st, tx, c[2], n, body.reward_info_list)
        _merge_infos(body.reward_info_list)
    return _resp(tx, 'post_pvt_item_box_open_choice', body)

def item_box_open_lot(st, req, ctx):
    q = req.post_pvt_item_box_open_lot
    body = P.Command_ApiNetwork_Api_PostPvtItemBoxOpenLotResponse()
    with st.transaction('item/box/open/lot') as tx:
        box = mdx('m_item_box', 0).get((q.item_id,))
        if box and box[1] and (q.consumption_count > 0):
            add_item(st, tx, q.item_id, -q.consumption_count)
            grant_reward_set(st, tx, box[1], q.consumption_count, body.reward_info_list)
    return _resp(tx, 'post_pvt_item_box_open_lot', body)

def weapon_rarity_up(st, req, ctx):
    q = req.post_pvt_weapon_rarity_up
    body = P.Command_ApiNetwork_Api_PostPvtWeaponRarityUpResponse()
    with st.transaction('weapon/rarity/up') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        if w:
            body.before_user_weapon.CopyFrom(w)
            for item_id, n in q.ingredient_item_map.items():
                add_item(st, tx, item_id, -n)
            w.rarity_type = q.rarity_type
            w.rarity_up_count += 1
            w.get_datetime = now_ms()
            tx.upsert('user_weapon_list', w)
            body.after_user_weapon.CopyFrom(w)
            mission_event(st, tx, 'ProgressWeaponRarityUp', 1, q.weapon_id)
    return _resp(tx, 'post_pvt_weapon_rarity_up', body)
HANDLERS.update({'/api/pvt/honor/setting': honor_setting, '/api/pvt/profile/update': profile_update, '/api/pvt/user/home/background/setting': home_background_setting, '/api/pvt/user/setting': user_setting, '/api/pvt/item/box/open/choice': item_box_open_choice, '/api/pvt/item/box/open/lot': item_box_open_lot, '/api/pvt/weapon/rarity/up': weapon_rarity_up})
EXTEND = {'materia': (7, 28, 57), 'accessory': (6, 27, 58), 'craft': (8, 29, 60), 'chocobo': (11, 30, 59)}

def _extend(kind):
    item, cost_col, n_col = EXTEND[kind]

    def h(st, req, ctx):
        cfg = md('m_config')[0]
        with st.transaction(f'shop/extend/{kind}') as tx:
            have = st.get('user_item_list', item)
            cap = item_cap(item)
            if (have.count if have else 0) < cap:
                give_stone(st, tx, 2, -cfg[cost_col]) if _blue(st) >= cfg[cost_col] else give_stone(st, tx, 1, -cfg[cost_col])
                add_item(st, tx, item, min(cfg[n_col], cap - (have.count if have else 0)))
        name = f'post_pvt_shop_extend_{kind}'
        return _resp(tx, name, getattr(P, f'Command_ApiNetwork_Api_PostPvtShopExtend{kind.capitalize()}Response')())
    return h

def _blue(st):
    return next((s.count for s in st.other_info.user_stone_list if s.stone_id == 2), 0) if st.other_info else 0

def weapon_limit_medal_exchange(st, req, ctx):
    q = req.post_pvt_weapon_limit_medal_exchange
    rate = md('m_config')[0][142] or 1
    body = P.Command_ApiNetwork_Api_PostPvtWeaponLimitMedalExchangeResponse()
    with st.transaction('weapon/limit/medal/exchange') as tx:
        total = 0
        for item_id, n in q.weapon_medal_map.items():
            if n > 0:
                add_item(st, tx, item_id, -n)
                total += n * rate
        if total:
            add_item(st, tx, 54, total)
            ri = body.reward_info_list.add()
            ri.reward_type = RT_ITEM
            ri.target_id = 54
            ri.count = total
    return _resp(tx, 'post_pvt_weapon_limit_medal_exchange', body)

def gacha_wish_choice(st, req, ctx):
    q = req.post_pvt_gacha_wish_choice
    with st.transaction('gacha/wish/choice') as tx:
        for c in q.gacha_wish_choice_info_list:
            row = st.get('user_gacha_wish_choice_list', c.gacha_wish_choice_id) or st.new_row('user_gacha_wish_choice_list', gacha_wish_choice_id=c.gacha_wish_choice_id)
            row.weapon_id = c.weapon_id
            tx.upsert('user_gacha_wish_choice_list', row)
    return _resp(tx, 'post_pvt_gacha_wish_choice', P.Command_ApiNetwork_Api_PostPvtGachaWishChoiceResponse())

SUMMON_PACT_GROUP_TYPE = 8   # m_mission_group col 1: 7 = a character's LB missions, 8 = a summon's Pact / Unlock

def mission_special_change(st, req, ctx):
    q = req.post_pvt_mission_special_change
    group = _mt_index()['groups'].get(q.mission_group_id)
    with st.transaction('mission/special/change') as tx:
        if group and group[1] == SUMMON_PACT_GROUP_TYPE:
            # A summon's Pact / Unlock mission lives in the account's single summon slot (one at a time); the client
            # times the switch wait from last_change_mission_datetime. It used to be written onto a character.
            slot = st.one('user_mission_special_summon_list') or st.new_row('user_mission_special_summon_list')
            slot.mission_group_id = q.mission_group_id
            slot.last_change_mission_datetime = now_ms()
            tx.upsert('user_mission_special_summon_list', slot)
        else:
            ch = st.get('user_character_list', group[10] if group and group[10] else q.mission_group_id // 100 % 1000)
            if ch:
                ch.mission_group_id = q.mission_group_id
                tx.upsert('user_character_list', ch)
        owned = set(st['user_skill_special_list'].keys())
        for m in _mt_index().get(MTYPE.get('ProgressLimitBreakLevel', -1), []):
            if _set_group(m[1]) == q.mission_group_id and m[5] and ((m[5],) in owned):
                row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
                if row.progress_count < 1:
                    row.progress_count = 1
                    tx.upsert('user_mission_list', row)
        gr = st.get('user_mission_group_list', q.mission_group_id) or st.new_row('user_mission_group_list', mission_group_id=q.mission_group_id)
        gr.last_reset_datetime = now_ms()
        tx.upsert('user_mission_group_list', gr)
        ts = st.get('user_tutorial_step_list', LBMISSION)
        if ts is None or ts.current_step < 1:
            ts = ts or st.new_row('user_tutorial_step_list', tutorial_step_type=LBMISSION)
            ts.current_step = 1
            tx.upsert('user_tutorial_step_list', ts)
    return _resp(tx, 'post_pvt_mission_special_change', P.Command_ApiNetwork_Api_PostPvtMissionSpecialChangeResponse())
LBMISSION = P.Command_Enums_TutorialStepType.Value('Command_Enums_TutorialStepType_Lbmission')

def shop_exchange_bulk(st, req, ctx):
    q = req.post_pvt_shop_exchange_bulk
    body = P.Command_ApiNetwork_Api_PostPvtShopExchangeBulkResponse()
    ups = []
    before = {}
    for line in q.shop_exchange_request_infos:
        if line.shop_item_id not in before:
            r0 = st.get('user_shop_item_list', line.shop_item_id)
            c = None
            if r0 is not None:
                c = type(r0)()
                c.CopyFrom(r0)
            before[line.shop_item_id] = c
    for line in q.shop_exchange_request_infos:
        one = P.Command_ApiNetwork_Api_ApiRequest()
        se = one.post_pvt_shop_exchange
        se.shop_item_id = line.shop_item_id
        se.purchase_count = line.purchase_count
        lr0 = st.get('user_shop_item_lineup_list', q.shop_id, line.idx) if q.shop_id in CHOCO_SHOPS else None
        pre = slot_weights(st, q.shop_id, line.idx, line.shop_item_id) if lr0 is not None and lr0.shop_item_id == line.shop_item_id else None
        r = shop_exchange(st, one, dict(ctx or {}, preroll=pre))
        b = r.post_pvt_shop_exchange
        body.reward_infos.extend(b.reward_infos)
        body.bonus_reward_infos.extend(b.bonus_reward_infos)
        body.is_limit = body.is_limit or b.is_limit
        ups.append(r.common.user.update)
    from engine.vault import row_key
    for sid, old in before.items():
        if old is None:
            st.tables['user_shop_item_list'].pop(row_key('user_shop_item_list', st.new_row('user_shop_item_list', shop_item_id=sid)), None)
        else:
            st.tables['user_shop_item_list'][row_key('user_shop_item_list', old)] = old
    for u in ups:
        del u.user_shop_item_list[:]
    with st.transaction('shop/exchange/bulk') as tx:
        for line in q.shop_exchange_request_infos:
            lr = st.get('user_shop_item_lineup_list', q.shop_id, line.idx) or st.new_row('user_shop_item_lineup_list', shop_id=q.shop_id, idx=line.idx, shop_item_id=line.shop_item_id)
            lr.purchase_count += line.purchase_count
            tx.upsert('user_shop_item_lineup_list', lr)
    out = _resp(tx, 'post_pvt_shop_exchange_bulk', body)
    merged = P.Command_Entity_Tables()
    for u in ups:
        merged.MergeFrom(u)
    merged.MergeFrom(out.common.user.update)
    out.common.user.update.CopyFrom(_dedupe(merged))
    return out

def _dedupe(tables):
    from engine.vault import row_key
    t = P.Command_Entity_Tables()
    for f, rows in tables.ListFields():
        last = {}
        for r in rows:
            last[row_key(f.name, r)] = r
        getattr(t, f.name).extend(last.values())
    return t

def shop_reset_lineup(st, req, ctx):
    q = req.post_pvt_shop_reset_lineup
    with st.transaction('shop/reset/lineup') as tx:
        us = st.get('user_shop_list', q.shop_id) or st.new_row('user_shop_list', shop_id=q.shop_id)
        cost = next((r[2] for r in md('m_shop_lineup_reset') if r[0] == q.shop_id and r[1] == us.lineup_reset_count), None)
        if cost is None:
            cost = max((r[2] for r in md('m_shop_lineup_reset') if r[0] == q.shop_id), default=0)
        if cost:
            add_item(st, tx, 1, -cost)
        for idx, items in sorted(_shop_slots(q.shop_id).items()):
            lr = st.get('user_shop_item_lineup_list', q.shop_id, idx) or st.new_row('user_shop_item_lineup_list', shop_id=q.shop_id, idx=idx)
            lr.shop_item_id = random.choice(items)
            lr.purchase_count = 0
            tx.upsert('user_shop_item_lineup_list', lr)
        us.lineup_reset_count += 1
        us.last_reset_datetime = now_ms()
        tx.upsert('user_shop_list', us)
        preroll_shop(st, q.shop_id)
    return _resp(tx, 'post_pvt_shop_reset_lineup', P.Command_ApiNetwork_Api_PostPvtShopResetLineupResponse())
_POOL = {}

def _lineup_pool():
    if not _POOL:
        p = storage.path('res', 'lineups.json', root=HERE)
        _POOL.update(json.load(open(p)) if os.path.isfile(p) else {})
    return _POOL
HANDLERS.update({f'/api/pvt/shop/extend/{k}': _extend(k) for k in EXTEND})
HANDLERS.update({'/api/pvt/weapon/limit/medal/exchange': weapon_limit_medal_exchange, '/api/pvt/gacha/wish/choice': gacha_wish_choice, '/api/pvt/mission/special/change': mission_special_change, '/api/pvt/shop/exchange/bulk': shop_exchange_bulk, '/api/pvt/shop/reset/lineup': shop_reset_lineup})
LOGIN_MSG = 991000000000014
_LB = {}
# Login bonus 3245 (2025-09-06 to 2026-03-06: two Lv. 120 OB6 weapons of one character a day for 15 days) runs again, whatever its dates,
# for accounts made on this server from STARTER_SINCE on. Imported accounts (origin "archived") and older ones never get it.
STARTER_LOGIN_BONUS = 3245
STARTER_SINCE = 1792195200000  # 2026-10-17 00:00 UTC
PR_GAME_START = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_GameStartDatetime')

def _starter_account(st):
    if getattr(st, 'origin', 'archived') != 'new':
        return False
    pr = st.get('user_play_record_list', PR_GAME_START)
    return bool(pr and pr.date_value >= STARTER_SINCE)

def login_bonus(st, req, ctx):
    now = (ctx or {}).get('now_ms') or now_ms()
    day = game_day(now)
    if not _LB:
        _LB['sheets'] = collections.defaultdict(list)
        for r in md('m_login_bonus_sheet'):
            _LB['sheets'][r[1]].append(r)
        _LB['reward'] = {(r[0], r[1]): r[2] for r in md('m_login_bonus_sheet_reward')}
        _LB['days'] = collections.Counter()
        for sheet, n in _LB['reward']:
            _LB['days'][sheet] = max(_LB['days'][sheet], n)
    body = P.Command_ApiNetwork_Api_PostPvtLoginBonusResponse()
    lt = P.Command_Enums_UserTimeType.Value('Command_Enums_UserTimeType_LoginBonus')
    with st.transaction('login/bonus') as tx:
        ut = st.get('user_time_list', lt)
        if ut and ut.datetime and (game_day(ut.datetime) == day):
            return _resp(tx, 'post_pvt_login_bonus', body)
        obt = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_Login')
        mission_event(st, tx, 'ProgressLogin')
        starter = _starter_account(st)
        active = [lb for lb in md('m_login_bonus') if not lb[9] and (lb[10] <= now and (lb[11] == 0 or now <= lb[11]) or (starter and lb[0] == STARTER_LOGIN_BONUS))]

        def running(lb):
            sh = sorted(_LB['sheets'].get(lb[4], []), key=lambda r: r[2])
            row = st.get('user_login_bonus_list', lb[0])
            return bool(sh) and (row is None or row.login_count < _LB['days'].get(sh[0][0], 0))
        beginner = any((lb[1] == 2 and running(lb) for lb in active))
        for lb in active:
            if beginner and lb[1] == 1:
                continue
            sheets = sorted(_LB['sheets'].get(lb[4], []), key=lambda r: r[2])
            if not sheets:
                continue
            sheet = sheets[0][0]
            days = _LB['days'].get(sheet, 0)
            row = st.get('user_login_bonus_list', lb[0])
            if row and row.last_get_date and (game_day(row.last_get_date) == day):
                continue
            n = (row.login_count if row else 0) + 1
            wrap = False
            if days and n > days:
                if lb[2] == 2:
                    n = 1
                    wrap = True
                else:
                    continue
            row = row or st.new_row('user_login_bonus_list', login_bonus_id=lb[0])
            if wrap:
                row.sheet_count += 1
            row.login_count = n
            row.total_login_count += 1
            row.last_get_date = now
            tx.upsert('user_login_bonus_list', row)
            res = body.login_bonus_result_list.add()
            res.login_bonus_id = lb[0]
            res.login_bonus_sheet_id = sheet
            res.login_count = n
            if row.sheet_count:
                res.sheet_count = row.sheet_count
            rs = _LB['reward'].get((sheet, n))
            if rs:
                _first_rewards(st, tx, ctx, rs, 1, res.reward_info_list, obt, LOGIN_MSG)
        ut = ut or st.new_row('user_time_list', user_time_type=lt)
        ut.datetime = now
        tx.upsert('user_time_list', ut)
        pt = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_TotalLoginCount')
        pr = st.get('user_play_record_list', pt) or st.new_row('user_play_record_list', play_record_type=pt)
        pr.int_value += 1
        tx.upsert('user_play_record_list', pr)
    return _resp(tx, 'post_pvt_login_bonus', body)
HANDLERS['/api/pvt/login/bonus'] = login_bonus
DQ_TYPE_GIL, DQ_TYPE_MEDAL = (1, 3)

def _dq(qid):
    return mdx('m_daily_quest', 0).get((qid,))

def _dq_pay(st, tx, q, reward_infos, judge_infos):
    judge = {r[0]: r[1] for r in md('m_daily_quest_judge_reward_type_reward')}
    if q[2] == DQ_TYPE_GIL or (q[2] != DQ_TYPE_MEDAL and q[3]):
        if q[3]:
            add_item(st, tx, GIL_ITEM, q[3])
            ri = reward_infos.add()
            ri.reward_type = RT_ITEM
            ri.target_id = GIL_ITEM
            ri.count = q[3]
    if q[2] == DQ_TYPE_GIL:
        rolls = [(random.choice(list(judge)), 1)] if q[1] <= 1 else [(t, random.randint(1, 3)) for t in sorted(judge)]
        for t, k in rolls:
            g = judge[t] * k
            add_item(st, tx, GIL_ITEM, g)
            ri = judge_infos.add()
            ri.reward_type = RT_ITEM
            ri.target_id = GIL_ITEM
            ri.count = g
            ri.reward_option.common_option.judge_reward_type = t
    elif q[2] == DQ_TYPE_MEDAL:
        s = st.one('user_daily_quest_setting_list')
        weapons = [getattr(s, f'target_id{i}') for i in range(5) if s and getattr(s, f'target_id{i}')] or [w.weapon_id for w in st['user_weapon_list'].values()][:5]
        medal = {r[0]: r[18] for r in md('m_weapon')}
        items = [medal[w] for w in weapons if medal.get(w)]
        if items:
            got = collections.Counter((random.choice(items) for _ in range(30)))
            for it, n in got.items():
                add_item(st, tx, it, n)
                ri = reward_infos.add()
                ri.reward_type = RT_ITEM
                ri.target_id = it
                ri.count = n
    elif q[9]:
        grant_reward_set(st, tx, q[9], max(1, q[8]), reward_infos)

def _dq_finish(st, tx, q, body_result):
    bt = _battle(q[5])
    gain = bt[13] // 5 if bt else 0
    for c in st['user_character_list'].values():
        body_result.before_user_characters.add().CopyFrom(c)
        if gain:
            cap = max((r[2] for r in md('m_character_level') if r[0] == c.character_id), default=c.exp)
            new = min(cap, c.exp + gain)
            if new != c.exp:
                c.exp = new
                tx.touch('user_character_list', c)
        body_result.after_user_characters.add().CopyFrom(c)
    row = st.get('user_daily_quest_list', q[0]) or st.new_row('user_daily_quest_list', daily_quest_id=q[0])
    row.daily_quest_id = q[0]
    # Square's accounts show (remain 1, total 1) for a request not yet cleared today and (remain 0, total 1) once it is
    row.remain_win_count = max(0, row.remain_win_count - 1)
    row.total_remain_win_count = max(1, row.total_remain_win_count)
    tx.upsert('user_daily_quest_list', row)
    pt = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_DailyQuestClearCount')
    pr = st.get('user_play_record_list', pt) or st.new_row('user_play_record_list', play_record_type=pt)
    pr.int_value += 1
    tx.upsert('user_play_record_list', pr)
    mission_event(st, tx, 'ProgressDailyQuestBattleWin')

def daily_quest_skip(st, req, ctx):
    qr = req.post_pvt_battle_skip
    q = _dq(qr.target_id)
    if not q:
        raise NotImplementedError(f'daily quest {qr.target_id}')
    body = P.Command_ApiNetwork_Api_PostPvtBattleSkipResponse()
    with st.transaction('battle/skip') as tx:
        old = st.one('user_battle_entry_list')
        e = st.new_row('user_battle_entry_list')
        e.entry_count = (old.entry_count if old else 0) + 1
        if old:
            st.tables['user_battle_entry_list'].clear()
        e.entry_count = e.entry_count
        e.battle_id = q[5]
        e.battle_type = DAILY_QUEST
        e.content_id = q[0]
        _entry_versions(e, old, ctx)
        e.battle_progress_type = P.Command_Enums_BattleProgressType.Value('Command_Enums_BattleProgressType_End')
        e.start_datetime = now_ms()
        tx.upsert('user_battle_entry_list', e)
        _dq_pay(st, tx, q, body.battle_result.reward_infos, body.battle_result.reward_infos)
        _dq_finish(st, tx, q, body.battle_result)
    return _resp(tx, 'post_pvt_battle_skip', body)

def daily_quest_setting(st, req, ctx):
    q = req.post_pvt_daily_quest_setting
    with st.transaction('daily/quest/setting') as tx:
        s = st.one('user_daily_quest_setting_list') or st.new_row('user_daily_quest_setting_list')
        s.daily_quest_type = q.daily_quest_type
        for i in range(5):
            setattr(s, f'target_id{i}', getattr(q, f'target_id{i}'))
        tx.upsert('user_daily_quest_setting_list', s)
    return _resp(tx, 'post_pvt_daily_quest_setting', P.Command_ApiNetwork_Api_PostPvtDailyQuestSettingResponse())

def daily_quest_start(st, req, ctx):
    q = req.post_pvt_daily_quest_start
    now = now_ms()
    dq = _dq(q.daily_quest_id)
    body = P.Command_ApiNetwork_Api_PostPvtDailyQuestStartResponse()
    with st.transaction('daily/quest/start') as tx:
        _start_common(st, tx, body, dq[5] if dq else q.daily_quest_id, q.daily_quest_id, 'Command_Enums_BattleType_DailyQuest', q.party_id, 0, now, ctx=ctx)
        en = st.one('user_daily_quest_entry_list') or st.new_row('user_daily_quest_entry_list')
        en.daily_quest_id = q.daily_quest_id
        en.entry_datetime = now
        en.party_id = q.party_id
        tx.upsert('user_daily_quest_entry_list', en)
    return _resp(tx, 'post_pvt_daily_quest_start', body)

def daily_quest_end(st, req, ctx):
    q = req.post_pvt_daily_quest_end
    body = P.Command_ApiNetwork_Api_PostPvtDailyQuestEndResponse()
    with st.transaction('daily/quest/end') as tx:
        en = st.one('user_daily_quest_entry_list')
        dq = _dq(en.daily_quest_id) if en else None
        e = _end_entry(st, tx, dq[5] if dq else 0)
        if dq and q.battle_result_type == WIN:
            _dq_pay(st, tx, dq, body.reward_infos, body.judge_reward_infos)
            _dq_finish(st, tx, dq, body.battle_result)
        else:
            # Retire or loss: the client waits for battle_result on every outcome, as the solo battle end always sends
            # it (before/after characters, no gain). Without it the battle screen never closes.
            body.battle_result.SetInParent()
            for c in st['user_character_list'].values():
                body.battle_result.before_user_characters.add().CopyFrom(c)
                body.battle_result.after_user_characters.add().CopyFrom(c)
    return _resp(tx, 'post_pvt_daily_quest_end', body)
HANDLERS.update({'/api/pvt/daily/quest/setting': daily_quest_setting, '/api/pvt/daily/quest/start': daily_quest_start, '/api/pvt/daily/quest/end': daily_quest_end})
_STOCK = {1: 'r_count', 2: 'sr_count', 3: 'ssr_count'}

def weapon_stock_exchange(st, req, ctx):
    q = req.post_pvt_weapon_stock_exchange
    body = P.Command_ApiNetwork_Api_PostPvtWeaponStockExchangeResponse()
    with st.transaction('weapon/stock/exchange') as tx:
        mw = {r[0]: r for r in md('m_weapon')}
        for e in q.exchange_weapon_info_list:
            w = st.get('user_weapon_list', e.weapon_id)
            m = mw.get(e.weapon_id)
            if not w or not m or (not m[18]):
                continue
            n = 0
            for rt in e.rarity_type_list:
                f = _STOCK.get(rt)
                if f and getattr(w, f):
                    n += getattr(w, f) * _medals(rt)
                    setattr(w, f, 0)
            tx.upsert('user_weapon_list', w)
            if n:
                add_item(st, tx, m[18], n)
                ri = body.reward_info_list.add()
                ri.reward_type = RT_ITEM
                ri.target_id = m[18]
                ri.count = n
    return _resp(tx, 'post_pvt_weapon_stock_exchange', body)
HANDLERS['/api/pvt/weapon/stock/exchange'] = weapon_stock_exchange

import threading
_SOCIAL_LOCK = threading.RLock()
_ACC = storage.path('saves', root=HERE)
_DISP = os.path.join(_ACC, 'friend_ids.json')
_SOC = os.path.join(_ACC, 'social.json')
FRT = lambda n: P.Command_Enums_FriendResponseType.Value('Command_Enums_FriendResponseType_' + n)
FTY = lambda n: P.Command_Enums_FriendType.Value('Command_Enums_FriendType_' + n)

def _jload(p, dflt):
    try:
        return json.load(open(p))
    except Exception:
        return dflt

def _jsave(p, d):
    from engine.vault import kv_set
    if p == _SOC:
        return kv_set(_ACC, 'social', d)
    if p == _DISP:
        return kv_set(_ACC, 'display_ids', d)
    tmp = p + f'.{os.getpid()}.tmp'
    json.dump(d, open(tmp, 'w'), indent=0)
    os.replace(tmp, p)

def _displays():
    from engine.vault import kv_get
    return kv_get(_ACC, 'display_ids', {})

def _display_of(uid):
    return next((d for d, u in _displays().items() if str(u) == str(uid)), None)

def square_display_id(uid):
    # Square's friend ID: the uid as 8 little-endian bytes XOR the battle_user_id key, base32 in the game's alphabet
    # (docs/ID-GENERATION.md). None for a uid that is not a 64-bit number.
    import base64
    try:
        raw = int(uid).to_bytes(8, 'little')
    except (TypeError, ValueError, OverflowError):
        return None
    raw = bytes(a ^ b for a, b in zip(raw, bytes.fromhex('4b10bc26a8e26257')))
    return base64.b32encode(raw).decode().rstrip('=').translate(
        str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567', '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'))

def display_id_for(uid):
    with _SOCIAL_LOCK:
        d = _displays()
        have = next((k for k, u in d.items() if str(u) == str(uid)), None)
        if have:
            return have
        alpha = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        new = square_display_id(uid)
        while new is None or new in d:
            new = ''.join((random.choice(alpha) for _ in range(13)))
        d[new] = str(uid)
        _jsave(_DISP, d)
        return new

def record_display_id(uid, file_name):
    import re
    import urllib.parse
    stem = os.path.splitext(os.path.basename(urllib.parse.unquote(file_name or '')))[0].strip().upper()
    if not re.fullmatch('[A-Z0-9]{13}', stem):
        return f'file name {file_name!r} is not a friend ID: none recorded'
    with _SOCIAL_LOCK:
        d = _displays()
        if stem in d:
            return f'{stem} already belongs to {d[stem]}' if str(d[stem]) != str(uid) else f'{stem} already recorded'
        have = next((k for k, u in d.items() if str(u) == str(uid)), None)
        if have:
            return f'{uid} already has friend ID {have}; {stem} not recorded'
        d[stem] = str(uid)
        _jsave(_DISP, d)
    return f'{stem} -> {uid} recorded'
_NAMES = [None, {}]

def _names():
    p = os.path.join(_ACC, 'folders.json')
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    if _NAMES[0] != mt:
        _NAMES[1] = _jload(p, {})
        _NAMES[0] = mt
    return _NAMES[1]

def _uid_to_display(d):
    r = {}
    for k, u in d.items():
        r.setdefault(str(u), k)
    return r

def _folder(uid):
    names = _names()
    f = os.path.join(_ACC, names.get(str(uid), str(uid)))
    return f if os.path.isfile(os.path.join(f, 'info.json')) else None
_INFO = globals().get('_INFO', {})
_LOGIN = globals().get('_LOGIN', {})

def _login_from_db(f):
    import sqlite3
    from engine import vault as _es
    fp = os.path.join(f, _es.DB)
    if not os.path.isfile(fp):
        return None
    t = 'user_time_list'
    kf = (_es.KEYS.get(t) or ['user_id'])[-1]
    try:
        c = sqlite3.connect(f'file:{fp}?mode=ro', uri=True, timeout=5)
        try:
            rows = c.execute('SELECT pb FROM rows WHERE tbl=?', (t,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    for pb, in rows:
        r = _es.ROW_CLASS[t]()
        r.ParseFromString(pb)
        if getattr(r, kf) == UT.Command_Enums_UserTimeType_Login:
            return r.datetime
    return 0

def _login_ms(uid, f, st):
    try:
        if st is None:
            if uid in _LOGIN:
                return _LOGIN[uid]
            v = _login_from_db(f)
            if v is not None:
                _LOGIN[uid] = v
                return v
            st = State.load(f)
        r = st.get('user_time_list', UT.Command_Enums_UserTimeType_Login)
        v = r.datetime if r is not None else 0
        _LOGIN[uid] = v
        return v
    except Exception as e:
        print(f'friend login time failed for {uid}: {e!r}')
        return 0
_INFO = globals().get('_INFO', {})

def _list_info(uid, disp=None):
    f = _folder(uid)
    disp = disp or _display_of(uid)
    if not f or not disp:
        return None
    live = getattr(State, 'LIVE', {}).get(os.path.realpath(f))
    if live is not None and live.mtime:
        mt = live.mtime
        st = live
    else:
        from engine.vault import play_state_mtime
        mt = play_state_mtime(f) or os.path.getmtime(os.path.join(f, 'base.pb'))
        st = None
    if _INFO.get(uid, (None,))[0] != mt:
        from engine.vault import read_summary
        sm = st.summary() if st is not None else read_summary(f) or State.load(f).summary()
        info = P.Command_Entity_UserListInfo()
        info.display_user_id = disp
        if sm.get('name'):
            info.player_name = sm['name']
        for k in ('top_honor_id', 'bottom_honor_id', 'background_honor_id', 'icon_honor_id'):
            setattr(info, k, sm.get(k) or 0)
        if sm.get('exp') is not None:
            info.user_rank = max((r[0] for r in md('m_user_rank') if r[2] <= sm['exp']), default=1)
        info.combat_power = sm.get('combat_power') or 0
        info.last_access_datetime = _login_ms(uid, f, st) or int(mt * 1000)
        _INFO[uid] = (mt, info)
    c = P.Command_Entity_UserListInfo()
    c.CopyFrom(_INFO[uid][1])
    return c

def _soc():
    from engine.vault import kv_get
    return kv_get(_ACC, 'social', {})

def _me(soc, uid):
    return soc.setdefault(str(uid), {'friends': [], 'out': [], 'in': [], 'hidden': []})

def _square_friends(st, ctx):
    # Links belong to the local social store, never to a captured player's friends.
    return []

def _seed_square(soc, uid, sq):
    d = _displays()
    me = _me(soc, uid)
    changed = False
    for info in sq:
        other = d.get(info.display_user_id)
        if other and other != str(uid) and (other not in me['friends']) and (info.display_user_id not in me['hidden']):
            me['friends'].append(other)
            o = _me(soc, other)
            if str(uid) not in o['friends']:
                o['friends'].append(str(uid))
            changed = True
    return changed

def _sort_by_login(infos):
    rows = []
    for i in infos:
        c = type(i)()
        c.CopyFrom(i)
        rows.append(c)
    rows.sort(key=lambda x: x.last_access_datetime, reverse=True)
    del infos[:]
    infos.extend(rows)

def friend_list(st, req, ctx):
    uid = str(st.user_id)
    body = P.Command_ApiNetwork_Api_PostPvtFriendListResponse()
    soc = _soc()
    sq = _square_friends(st, ctx)
    if _seed_square(soc, uid, sq):
        with _SOCIAL_LOCK:
            soc = _soc()
            if _seed_square(soc, uid, sq):
                _jsave(_SOC, soc)
    me = _me(soc, uid)
    d = _displays()
    rev = _uid_to_display(d)
    shown = set()
    for f in me['friends']:
        i = _list_info(f, rev.get(str(f)))
        if i:
            body.user_list_infos.add().CopyFrom(i)
            shown.add(i.display_user_id)
    _sort_by_login(body.user_list_infos)
    with st.transaction('friend/list') as tx:
        pass
    return _resp(tx, 'post_pvt_friend_list', body)

def _pending_list(kind, field):

    def h(st, req, ctx):
        body = getattr(P, f'Command_ApiNetwork_Api_PostPvtFriend{kind}ListResponse')()
        rev = _uid_to_display(_displays())
        for u in _me(_soc(), st.user_id)[field]:
            i = _list_info(u, rev.get(str(u)))
            if i:
                body.user_list_infos.add().CopyFrom(i)
        with st.transaction(f'friend/{kind.lower()}/list') as tx:
            pass
        return _resp(tx, f'post_pvt_friend_{kind.lower()}_list', body)
    return h

def _limits():
    c = md('m_config')[0]
    return (c[63] or 99, c[64] or 10, c[65] or 10)

def friend_request(st, req, ctx):
    disp = req.post_pvt_friend_request.display_user_id
    uid = str(st.user_id)
    body = P.Command_ApiNetwork_Api_PostPvtFriendRequestResponse()
    fmax, omax, imax = _limits()
    with _SOCIAL_LOCK:
        soc = _soc()
        other = _displays().get(disp)
        me = _me(soc, uid)
        if not other or other == uid or (not _folder(other)):
            body.friend_response_type = FRT('UserNotFoundByDisplayId')
        elif other in me['friends']:
            body.friend_response_type = FRT('FriendExists')
        elif other in me['out']:
            body.friend_response_type = FRT('FriendRequestExists')
        elif other in me['in']:
            body.friend_response_type = FRT('FriendReceiveExists')
        elif len(me['friends']) >= fmax:
            body.friend_response_type = FRT('FriendMaxLimit')
        elif len(me['out']) >= omax:
            body.friend_response_type = FRT('FriendRequestMaxLimit')
        elif len(_me(soc, other)['in']) >= imax:
            body.friend_response_type = FRT('FriendReceiveMaxLimit')
        else:
            me['out'].append(other)
            _me(soc, other)['in'].append(uid)
            _jsave(_SOC, soc)
            body.friend_response_type = FRT('Success')
    with st.transaction('friend/request') as tx:
        if body.friend_response_type == FRT('Success'):
            mission_event(st, tx, 'SetFriendRequest', 1)   # "Send a friend invitation"
    return _resp(tx, 'post_pvt_friend_request', body)

def friend_approve(st, req, ctx):
    disp = req.post_pvt_friend_approve.display_user_id
    uid = str(st.user_id)
    body = P.Command_ApiNetwork_Api_PostPvtFriendApproveResponse()
    fmax = _limits()[0]
    with _SOCIAL_LOCK:
        soc = _soc()
        other = _displays().get(disp)
        me = _me(soc, uid)
        if not other or other not in me['in']:
            body.friend_response_type = FRT('FriendReceiveNotExists')
        elif len(me['friends']) >= fmax:
            body.friend_response_type = FRT('FriendMaxLimit')
        elif len(_me(soc, other)['friends']) >= fmax:
            body.friend_response_type = FRT('FriendMaxLimitTargetUser')
        else:
            o = _me(soc, other)
            me['in'].remove(other)
            o['out'] = [x for x in o['out'] if x != uid]
            me['friends'].append(other)
            o['friends'].append(uid)
            _jsave(_SOC, soc)
            body.friend_response_type = FRT('Success')
    with st.transaction('friend/approve') as tx:
        pass
    return _resp(tx, 'post_pvt_friend_approve', body)

def _drop(field_me, field_other, resp_name, body_cls, with_type=False):

    def h(st, req, ctx):
        disp = getattr(req, resp_name).display_user_id
        uid = str(st.user_id)
        body = body_cls()
        with _SOCIAL_LOCK:
            soc = _soc()
            other = _displays().get(disp)
            me = _me(soc, uid)
            if other:
                me[field_me] = [x for x in me[field_me] if x != other]
                o = _me(soc, other)
                o[field_other] = [x for x in o[field_other] if x != uid]
            if resp_name == 'post_pvt_friend_dissolve' and disp not in me['hidden']:
                me['hidden'].append(disp)
            _jsave(_SOC, soc)
            if with_type:
                body.friend_response_type = FRT('Success')
        with st.transaction(resp_name) as tx:
            pass
        return _resp(tx, resp_name, body)
    return h

def user_search(st, req, ctx):
    disp = req.post_pvt_user_search.display_user_id
    uid = str(st.user_id)
    body = P.Command_ApiNetwork_Api_PostPvtUserSearchResponse()
    soc = _soc()
    other = _displays().get(disp)
    info = _list_info(other) if other else None
    if not info:
        body.friend_response_type = FRT('UserNotFoundByDisplayId')
    else:
        me = _me(soc, uid)
        body.user_list_info.CopyFrom(info)
        body.friend_count = len(_me(soc, other)['friends'])
        body.friend_type = FTY('Myself') if other == uid else FTY('Friend') if other in me['friends'] else FTY('PendingRequest') if other in me['out'] else FTY('PendingReceive') if other in me['in'] else FTY('NotFriend')
        body.friend_response_type = FRT('Success')
    with st.transaction('user/search') as tx:
        pass
    return _resp(tx, 'post_pvt_user_search', body)
HANDLERS.update({'/api/pvt/friend/list': friend_list, '/api/pvt/friend/request/list': _pending_list('Request', 'out'), '/api/pvt/friend/receive/list': _pending_list('Receive', 'in'), '/api/pvt/friend/request': friend_request, '/api/pvt/friend/approve': friend_approve, '/api/pvt/friend/reject': _drop('in', 'out', 'post_pvt_friend_reject', P.Command_ApiNetwork_Api_PostPvtFriendRejectResponse), '/api/pvt/friend/cancel': _drop('out', 'in', 'post_pvt_friend_cancel', P.Command_ApiNetwork_Api_PostPvtFriendCancelResponse, True), '/api/pvt/friend/dissolve': _drop('friends', 'friends', 'post_pvt_friend_dissolve', P.Command_ApiNetwork_Api_PostPvtFriendDissolveResponse), '/api/pvt/user/search': user_search})

def story_select_drama(st, req, ctx):
    q = req.post_pvt_story_select_drama
    with st.transaction('story/select/drama') as tx:
        row = st.get('user_story_drama_selection_list', q.selection_id) or st.new_row('user_story_drama_selection_list', selection_id=q.selection_id)
        row.selection_idx = q.selection_idx
        tx.upsert('user_story_drama_selection_list', row)
    r = _resp(tx)
    r.post_pvt_story_select_drama.SetInParent()
    return r
HANDLERS['/api/pvt/story/select/drama'] = story_select_drama

def _cs_episode(eid):
    return mdx('m_character_story_episode', 0).get((eid,))

def _cs_clear(st, tx, eid, ep_infos, sec_infos):
    ep = _cs_episode(eid)
    if st.get('user_character_story_episode_list', eid) is None:
        tx.upsert('user_character_story_episode_list', st.new_row('user_character_story_episode_list', character_story_episode_id=eid))
        if ep and ep[7] and (ep_infos is not None):
            grant_reward_set(st, tx, ep[7], 1, ep_infos)
        if ep:
            sec = ep[1]
            eps = [r[0] for r in md('m_character_story_episode') if r[1] == sec]
            if eps and all((st.get('user_character_story_episode_list', x) is not None for x in eps)):
                s = mdx('m_character_story_section', 0).get((sec,))
                if s and s[5] and (sec_infos is not None):
                    grant_reward_set(st, tx, s[5], 1, sec_infos)
                mission_event(st, tx, 'ProgressCharacterStorySectionClear')
    mission_event(st, tx, 'ProgressCharacterStoryEpisodeClear')

def _save_party_info(st, tx, pi, t):
    if not pi.party_id:
        return
    party = st.get('user_party_list', pi.party_id) or st.new_row('user_party_list', party_id=pi.party_id)
    party.combat_power = pi.combat_power
    party.is_active = True
    if pi.HasField('memoria_info'):
        for f in party.DESCRIPTOR.fields:
            if f.name.startswith('memoria_id'):
                party.ClearField(f.name)
        for f, v in pi.memoria_info.ListFields():
            if hasattr(party, f.name):
                setattr(party, f.name, v)
    tx.upsert('user_party_list', party)
    for m in pi.user_party_member_list:
        row = P.Command_Entity_UserPartyMember()
        row.CopyFrom(m)
        row.user_id = st.user_id
        row.updated_datetime = t
        if not m.is_active:
            keep = {f.name: v for f, v in row.ListFields() if f.name in CLEARED_MEMBER_KEEP}
            row = P.Command_Entity_UserPartyMember()
            for k, v in keep.items():
                setattr(row, k, v)
        else:
            _oacc_slot(st, row)
        tx.upsert('user_party_member_list', row)

def character_story_battle_start(st, req, ctx):
    q = req.post_pvt_character_story_battle_start
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtCharacterStoryBattleStartResponse()
    with st.transaction('character/story/battle/start') as tx:
        if q.HasField('party_info'):
            _save_party_info(st, tx, q.party_info, now)
        ep = _cs_episode(q.episode_id)
        bid = q.battle_id or (ep[5] if ep else 0)
        _start_common(st, tx, body, bid, q.episode_id, 'Command_Enums_BattleType_CharacterStory', q.party_id, 0, now, ctx=ctx)
    return _resp(tx, 'post_pvt_character_story_battle_start', body)

def character_story_battle_end(st, req, ctx):
    q = req.post_pvt_character_story_battle_end
    body = P.Command_ApiNetwork_Api_PostPvtCharacterStoryBattleEndResponse()
    with st.transaction('character/story/battle/end') as tx:
        ep = _cs_episode(q.episode_id)
        bid = q.battle_id or (ep[5] if ep else 0)
        e, win, mult, chars, status = _end_common(st, tx, body, q, bid, kinds=(('ProgressCharacterStoryBattleWin', 0),))
        body.battle_result.SetInParent()
        if win:
            if not st.get('user_character_story_episode_battle_list', q.episode_id):
                tx.upsert('user_character_story_episode_battle_list', st.new_row('user_character_story_episode_battle_list', character_story_episode_id=q.episode_id, battle_id=bid))
                _drops(st, tx, bid, q.battle_enemy_results, 1, body.battle_result.reward_infos)
            _cs_clear(st, tx, q.episode_id, body.rewards, body.rewards)
    return _resp(tx, 'post_pvt_character_story_battle_end', body)

def character_story_result(st, req, ctx):
    q = req.post_pvt_character_story_result
    body = P.Command_ApiNetwork_Api_PostPvtCharacterStoryResultResponse()
    with st.transaction('character/story/result') as tx:
        _cs_clear(st, tx, q.story_episode_id, body.story_section_first_clear_reward_infos, body.story_section_first_clear_reward_infos)
    r = _resp(tx, 'post_pvt_character_story_result', body)
    r.post_pvt_character_story_result.SetInParent()
    return r
HANDLERS.update({'/api/pvt/character/story/battle/start': character_story_battle_start, '/api/pvt/character/story/battle/end': character_story_battle_end, '/api/pvt/character/story/result': character_story_result})

def party_multi_set_upsert(st, req, ctx):
    q = req.post_pvt_party_multi_set_upsert
    t = now_ms()
    with st.transaction('party/multi/set/upsert') as tx:
        for mp in q.multi_party_member_list:
            m = mp.user_party_member
            row = P.Command_Entity_UserPartyMember()
            row.CopyFrom(m)
            row.user_id = st.user_id
            row.updated_datetime = t
            _oacc_slot(st, row)
            tx.upsert('user_party_member_list', row)
            pid = m.party_member_id // 10
            party = st.get('user_party_list', pid) or st.new_row('user_party_list', party_id=pid)
            party.combat_power = m.combat_power
            party.is_active = True
            for f in party.DESCRIPTOR.fields:
                if f.name.startswith('memoria_id'):
                    party.ClearField(f.name)
            for f, v in mp.memoria_info.ListFields():
                if hasattr(party, f.name):
                    setattr(party, f.name, v)
            tx.upsert('user_party_list', party)
    r = _resp(tx)
    r.post_pvt_party_multi_set_upsert.SetInParent()
    return r

def battle_delete(st, req, ctx):
    with st.transaction('battle/delete') as tx:
        e = st.one('user_battle_entry_list')
        if e:
            e.battle_progress_type = P.Command_Enums_BattleProgressType.Value('Command_Enums_BattleProgressType_End')
            tx.touch('user_battle_entry_list', e)
    r = _resp(tx)
    r.post_pvt_battle_delete.SetInParent()
    return r

def mission_supply(st, req, ctx):
    q = req.post_pvt_mission_supply
    t = now_ms()
    m = _mt_index()['by_id'].get(q.mission_id)
    with st.transaction('mission/supply') as tx:
        for item_id, n in q.item_id_map.items():
            it = st.get('user_item_list', item_id)
            if it is None or not n:
                continue
            use = min(n, it.count)
            it.count -= use
            it.total_consumption_count += use
            tx.upsert('user_item_list', it)
        if m:
            s = st.get('user_mission_set_list', m[1]) or st.new_row('user_mission_set_list', mission_set_id=m[1])
            k = m[2] // 64
            for pre in ('achieved', 'notified'):
                f = f'{pre}_mission_set_idx_flags{k}'
                setattr(s, f, _flag_or(getattr(s, f), m[2]))
            tx.upsert('user_mission_set_list', s)
            gid = _set_group(m[1])
            g = st.get('user_mission_group_list', gid) or st.new_row('user_mission_group_list', mission_group_id=gid)
            g.last_reset_datetime = t
            tx.upsert('user_mission_group_list', g)
    r = _resp(tx)
    r.post_pvt_mission_supply.SetInParent()
    if m:
        r.common.user.other_info.mission_achieved_list.add().mission_id = q.mission_id
    return r

def steam_achievement(st, req, ctx):
    q = req.post_pvt_steam_achievement
    t = now_ms()
    with st.transaction('steam/achievement') as tx:
        for aid in q.steam_achievement_ids:
            row = st.get('user_steam_achievement_list', aid)
            if row is None:
                row = st.new_row('user_steam_achievement_list', steam_achievement_id=aid, achievement_datetime=t)
            tx.upsert('user_steam_achievement_list', row)
    r = _resp(tx)
    r.post_pvt_steam_achievement.SetInParent()
    return r
HANDLERS.update({'/api/pvt/party/multi/set/upsert': party_multi_set_upsert, '/api/pvt/battle/delete': battle_delete, '/api/pvt/mission/supply': mission_supply, '/api/pvt/steam/achievement': steam_achievement})

def materia_lock(st, req, ctx):
    q = req.post_pvt_materia_lock
    with st.transaction('materia/lock') as tx:
        for uid, locked in q.user_materia_lock_map.items():
            m = st.get('user_materia_list', uid)
            if m:
                m.is_lock = bool(locked)
                tx.touch('user_materia_list', m)
    r = _resp(tx)
    r.post_pvt_materia_lock.SetInParent()
    return r

def chocobo_event_farm_treasure(st, req, ctx):
    q = req.post_pvt_chocobo_event_farm_treasure
    now = now_ms()
    body = P.Command_ApiNetwork_Api_PostPvtChocoboEventFarmTreasureResponse()
    with st.transaction('chocobo/event/farm/treasure') as tx:
        t = mdx('m_chocobo_event_farm_treasure', 0).get((q.chocobo_event_farm_treasure_id,))
        row = st.get('user_chocobo_event_farm_list', t[1]) if t else None
        term = mdx('m_chocobo_event_farm_term', 0).get((t[1],)) if t else None
        if row and _treasure_taken(term[1] if term else 0, row.last_open_treasure_datetime, now):
            t = None
        if t:
            if t[2]:
                grant_reward_set(st, tx, t[2], 1, body.reward_infos)
            row = row or st.new_row('user_chocobo_event_farm_list', chocobo_event_farm_term_id=t[1])
            row.last_open_treasure_datetime = now
            tx.upsert('user_chocobo_event_farm_list', row)
            mission_event(st, tx, 'ProgressChocoboEventFarmRewardReceive')
    r = _resp(tx, 'post_pvt_chocobo_event_farm_treasure', body)
    r.post_pvt_chocobo_event_farm_treasure.SetInParent()
    return r
HANDLERS.update({'/api/pvt/materia/lock': materia_lock, '/api/pvt/chocobo/event/farm/treasure': chocobo_event_farm_treasure})

def _evolve_row(weapon_id, etype):
    return next((r for r in md('m_weapon_evolve') if r[1] == weapon_id and r[2] == etype), None)

def _evolve_set(st, tx, w, etype):
    if w.evolve_flags0 >> etype & 1 and w.weapon_evolve_type != etype:
        w.weapon_evolve_type = etype
        tx.touch('user_weapon_list', w)
        return True
    return False

def weapon_evolve(st, req, ctx):
    q = req.post_pvt_weapon_evolve
    body = P.Command_ApiNetwork_Api_PostPvtWeaponEvolveResponse()
    with st.transaction('weapon/evolve') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        ev = _evolve_row(q.weapon_id, q.weapon_evolve_type)
        if w and ev and (not w.evolve_flags0 >> ev[2] & 1):
            body.before_user_weapon.CopyFrom(w)
            if ev[3]:
                add_item(st, tx, GIL_ITEM, -ev[3])
                body.consumption_gil = ev[3]
            for g in md('m_weapon_ingredient'):
                if g[0] == ev[4] and g[2]:
                    add_item(st, tx, g[1], -g[2])
            w.evolve_flags0 |= 1 << ev[2]
            if not w.weapon_evolve_type:  # #70: the first unlock goes into use; later ones leave the one in use
                w.weapon_evolve_type = ev[2]
            tx.touch('user_weapon_list', w)
            mission_event(st, tx, 'ProgressWeaponEvolveCount')
            body.after_user_weapon.CopyFrom(w)
        elif w:
            body.before_user_weapon.CopyFrom(w)
            body.after_user_weapon.CopyFrom(w)
    r = _resp(tx, 'post_pvt_weapon_evolve', body)
    r.post_pvt_weapon_evolve.SetInParent()
    return r

def weapon_evolve_change(st, req, ctx):
    q = req.post_pvt_weapon_evolve_change
    body = P.Command_ApiNetwork_Api_PostPvtWeaponEvolveChangeResponse()
    with st.transaction('weapon/evolve/change') as tx:
        w = st.get('user_weapon_list', q.weapon_id)
        if w:
            body.before_user_weapon.CopyFrom(w)
            if not _evolve_set(st, tx, w, q.weapon_evolve_type):
                tx.touch('user_weapon_list', w)
            body.after_user_weapon.CopyFrom(w)
    r = _resp(tx, 'post_pvt_weapon_evolve_change', body)
    r.post_pvt_weapon_evolve_change.SetInParent()
    return r

def weapon_evolve_change_bulk(st, req, ctx):
    q = req.post_pvt_weapon_evolve_change_bulk
    with st.transaction('weapon/evolve/change/bulk') as tx:
        for c in q.weapon_evolve_change_info_list:
            w = st.get('user_weapon_list', c.weapon_id)
            if w:
                _evolve_set(st, tx, w, c.weapon_evolve_type)
    r = _resp(tx)
    r.post_pvt_weapon_evolve_change_bulk.SetInParent()
    return r
HANDLERS.update({'/api/pvt/weapon/evolve': weapon_evolve, '/api/pvt/weapon/evolve/change': weapon_evolve_change, '/api/pvt/weapon/evolve/change/bulk': weapon_evolve_change_bulk})

def dungeon_select_drama(st, req, ctx):
    q = req.post_pvt_dungeon_select_drama
    body = P.Command_ApiNetwork_Api_PostPvtDungeonSelectDramaResponse()
    with st.transaction('dungeon/select/drama') as tx:
        row = st.get('user_dungeon_drama_selection_list', q.selection_id) or st.new_row('user_dungeon_drama_selection_list', dungeon_drama_selection_id=q.selection_id)
        bit = 1 << q.selection_idx
        new = not row.selected_idx_flag & bit
        ch = next((c for c in md('m_dungeon_drama_selection_choice') if c[0] == q.selection_id and c[1] == q.selection_idx), None)
        if ch and ch[2] and (new or not ch[3]):
            grant_reward_set(st, tx, ch[2], 1, body.reward_infos)
        row.selection_idx = q.selection_idx
        row.selected_idx_flag |= bit
        tx.upsert('user_dungeon_drama_selection_list', row)
    r = _resp(tx, 'post_pvt_dungeon_select_drama', body)
    r.post_pvt_dungeon_select_drama.SetInParent()
    return r
HANDLERS['/api/pvt/dungeon/select/drama'] = dungeon_select_drama

def event_scenario_result(st, req, ctx):
    q = req.post_pvt_event_scenario_result
    body = P.Command_ApiNetwork_Api_PostPvtEventScenarioResultResponse()
    with st.transaction('event/scenario/result') as tx:
        _episode_clear(st, tx, q.event_scenario_episode_id, body.reward_infos)
    r = _resp(tx, 'post_pvt_event_scenario_result', body)
    r.post_pvt_event_scenario_result.SetInParent()
    return r
HANDLERS['/api/pvt/event/scenario/result'] = event_scenario_result
RETIRE = P.Command_Enums_BattleResultType.Value('Command_Enums_BattleResultType_Retire')
RT_EVENT_IDLING_LEVEL = P.Command_Enums_RewardType.Value('Command_Enums_RewardType_EventIdlingCollectLevel')

def _episode_clear(st, tx, ep_id, infos):
    """An event episode is done: a scene watched, or (kind 3) the battle that stands for it won. The next episode
    and stage open from its clear count."""
    ep = mdx('m_event_scenario_episode', 0).get((ep_id,))
    row = st.get('user_event_scenario_episode_list', ep_id) or st.new_row('user_event_scenario_episode_list', event_scenario_episode_id=ep_id)
    if row.clear_count == 0 and ep and ep[5]:
        grant_reward_set(st, tx, ep[5], max(1, ep[6]), infos)
    row.clear_count += 1
    tx.upsert('user_event_scenario_episode_list', row)
    mission_event(st, tx, 'SetEventStoryRead', 1, ep_id)

# A Story-title copy (engine/storytitles.py: Event Stories / Crossover Stories, 88xxxxxx) clears the event episode it copies.
# The events' "Play ..." missions are SetEventStoryRead on the EVENT episode (Flash of Destiny's "Play "Carving Light"" =
# 303607), so reading the copy never counted. _episode_clear pays the event episode's first reward once, whichever way
# it is read (the copies pay none), and raises the mission. Copies read before this are caught up at login.
_STE = {}

def _story_title_event_map():
    """Story-title episode -> the event scenario episode it copies. A chapter is one event's scenario in order: its match
    is the event's own section (m_event_base col 4) whose scenes play the same dungeons (Story copy = event dungeon -
    200000) and whose fights fight the same battles. Re-runs sharing every dungeon and battle (Reawakened) go by
    order: chapter ids are numbered oldest first, and the lower event id is the older run."""
    src = md('m_story_episode')
    if _STE.get('src') is src:
        return _STE['map']
    chapters = sorted(r[0] for r in md('m_story_chapter') if r[1] in (8, 9))
    by_ch = {}
    for e in src:
        if e[1] in chapters and e[0] // 1000000 == 88 and e[0] % 1000:
            by_ch.setdefault(e[1], []).append(e)
    battle_of = {r[0]: r[1] for r in md('m_story_battle')}
    stages_of_battle = {}
    for s in md('m_event_solo_battle'):
        stages_of_battle.setdefault(s[5], set()).add(s[0])
    own = {r[4]: r[0] for r in md('m_event_base') if r[4]}
    secs = {}
    for e in md('m_event_scenario_episode'):
        if e[1] in own:
            secs.setdefault(e[1], []).append(e)
    for v in secs.values():
        v.sort(key=lambda e: e[4])

    def key(e, story):
        if story:
            return ('b', frozenset(stages_of_battle.get(battle_of.get(e[15]), ()))) if e[15] else ('d', e[4] + 200000)
        return ('s', e[11]) if (not e[3] and e[11]) else ('d', e[3])

    def same(a, b):
        return (a[0] == 'b' and b[0] == 's' and b[1] in a[1]) or (a[0] == b[0] == 'd' and a[1] == b[1])
    m = {}
    taken = set()
    for cid in chapters:
        eps = sorted(by_ch.get(cid, []), key=lambda e: e[5])
        if not eps:
            continue
        ks = [key(e, True) for e in eps]
        cand = sorted((sid for sid, sc in secs.items() if sid not in taken and len(sc) == len(eps)
                       and all(same(a, key(b, False)) for a, b in zip(ks, sc))), key=lambda sid: own[sid])
        if not cand:
            continue
        taken.add(cand[0])
        for e, ev in zip(eps, secs[cand[0]]):
            m[e[0]] = ev[0]
    _STE.update(src=src, map=m)
    return m

def _ste_credit(st, tx, story_ep, infos=None):
    """A Story-title copy was cleared: clear its event episode once (nothing for any other episode)."""
    ev = _story_title_event_map().get(story_ep) if story_ep else None
    if not ev:
        return False
    row = st.get('user_event_scenario_episode_list', ev)
    if row and row.clear_count:
        return False
    _episode_clear(st, tx, ev, infos)
    return True

def _story_title_events_heal(st):
    m = _story_title_event_map()
    todo = []
    for k, v in st.tables['user_episode_list'].items():
        ev = m.get(k[0])
        if ev and (v.story_party_mode_clear_datetime or v.free_party_mode_clear_datetime):
            row = st.get('user_event_scenario_episode_list', ev)
            if not (row and row.clear_count):
                todo.append(k[0])
    if not todo:
        return 0
    with st.transaction('story titles: event episodes already read') as tx:
        n = sum(_ste_credit(st, tx, e) for e in todo)
    print(f'story title events heal: {st.user_id} {n} event episode(s)')
    return n

def _story_entry_heal(st):
    """The last story episode entered (user_episode_entry_list, and the Story dungeon's content_id) may be one the Story
    titles no longer build, such as the crossovers' old "Entrance" 88901000 (engine/storytitles.py). Home looks it up
    while loading and the game waits on "Connecting" forever. Such an entry moves to the first episode left in its
    chapter (Story-title episodes are 88000000 + chapter * 1000 + n), or else goes."""
    eps = mdx('m_story_episode', 0)
    first = {}
    for e in md('m_story_episode'):
        if e[1] not in first or e[5] < first[e[1]][5]:
            first[e[1]] = e
    fixes = []
    for table, row, field in (('user_episode_entry_list', st.one('user_episode_entry_list'), 'episode_id'),
                              ('user_dungeon_entry_list', st.get('user_dungeon_entry_list', DUNGEON_STORY), 'content_id')):
        eid = getattr(row, field, 0) if row is not None else 0
        if eid and (eid,) not in eps:
            e = first.get(eid // 1000 % 1000) if eid // 1000000 == 88 else None
            fixes.append((table, row, field, eid, e[0] if e else 0))
    if not fixes:
        return 0
    with st.transaction('story: entered episode no longer built') as tx:
        for table, row, field, eid, to in fixes:
            print(f'story entry heal: {st.user_id} {table} episode {eid} -> {to or "none"}')
            if to or table == 'user_dungeon_entry_list':
                setattr(row, field, to)
                tx.upsert(table, row)
            else:
                tx.delete(table, row)
    return len(fixes)
_BATTLE_EP = {}

def _battle_episode(stage_id):
    """The battle episode (kind 3) a stage stands for, e.g. Holy Flame's "Present Leveling Battle 1" is episode 800102."""
    if not _BATTLE_EP:
        _BATTLE_EP.update({ep[11]: ep[0] for ep in md('m_event_scenario_episode') if ep[10] == 3 and ep[11]})
        _BATTLE_EP.setdefault(0, 0)
    return _BATTLE_EP.get(stage_id, 0) if stage_id else 0

def _battle_episode_req(ep_id):
    ep = mdx('m_event_scenario_episode', 0).get((ep_id,)) if ep_id else None
    return ep[0] if ep and ep[10] == 3 else 0

def _score_battle(stage):
    sid = stage[18] if stage and len(stage) > 18 else 0
    return mdx('m_event_score_battle', 0).get((sid,)) if sid else None

def _score_battle_rank(sb, score):
    return max((r[2] for r in md('m_event_score_battle_score_rank') if r[0] == sb[0] and score >= r[1]), default=RANK_S)

def _score_battle_result(st, tx, sb, old_high, score, rank, body):
    """The score is the damage dealt (Square's own formula isn't known); score rewards are given once, as the best
    score passes each step."""
    res = body.score_battle_score_result
    res.base_damage_score = score
    res.final_score = score
    res.final_score_battle_rank_type = rank
    for r in md('m_event_score_battle_score_reward'):
        if r[0] == sb[0] and old_high < r[1] <= score:
            grant_reward_set(st, tx, r[2], 1, body.battle_result.reward_infos)
    print(f'score battle {sb[0]}: score {score}, rank {P.Command_Enums_BattleRankType.Name(rank)[-1]}')

def _idling_levels(cid):
    return sorted((r for r in md('m_event_idling_collect_level') if r[0] == cid), key=lambda r: r[1])

def _idling_row(st, cid, t):
    return st.get('user_event_idling_collect_list', cid) or st.new_row('user_event_idling_collect_list', event_idling_collect_id=cid, level=1, start_datetime=t, calc_start_datetime=t)

def _idling_level_up(st, tx, cid, n):
    """An event's present level (Holy Flame's presents): it starts at 1 and each leveling battle's first clear adds one."""
    lv = _idling_levels(cid)
    row = _idling_row(st, cid, now_ms())
    row.level = min(lv[-1][1] if lv else row.level + n, max(1, row.level) + n)
    tx.upsert('user_event_idling_collect_list', row)
    mission_event(st, tx, 'ProgressAbsoluteEventIdlingCollectLevel', row.level, cid)   # "Reach Present Level N" [event]

def event_idling_collect_result(st, req, ctx):
    """Presents piled up since the last collect: one every interval of the present level, up to its cap; each gives
    the level's normal set and one draw from its extra set."""
    q = req.post_pvt_event_idling_collect_result
    body = P.Command_ApiNetwork_Api_PostPvtEventIdlingCollectResultResponse()
    t = now_ms()
    with st.transaction('event/idling/collect/result') as tx:
        row = _idling_row(st, q.event_base_id, t)
        lv = next((r for r in _idling_levels(q.event_base_id) if r[1] == row.level), None)
        if lv and lv[3]:
            step = lv[3] * 1000
            n = min(max(0, t - row.calc_start_datetime) // step, lv[2])
            if n:
                grant_reward_set(st, tx, lv[4], n, body.normal_reward_infos)
                grant_reward_set(st, tx, lv[5], n, body.additional_reward_infos)
                row.calc_start_datetime = t if n >= lv[2] else row.calc_start_datetime + n * step
        tx.upsert('user_event_idling_collect_list', row)
    return _resp(tx, 'post_pvt_event_idling_collect_result', body)
HANDLERS['/api/pvt/event/idling/collect/result'] = event_idling_collect_result

def grant_character(st, tx, cid):
    if st.get('user_character_list', cid):
        return
    mc = mdx('m_character', 0).get((cid,))
    groups = _mt_index()['groups']
    row = st.new_row('user_character_list', character_id=cid)
    row.exp = min((r.exp for r in st['user_character_list'].values()), default=0)
    if 700000 + cid * 100 + 1 in groups:
        row.mission_group_id = 700000 + cid * 100 + 1
    if 2300000 + cid * 100 + 1 in groups:
        row.overaccel_mission_group_id = 2300000 + cid * 100 + 1
    tx.upsert('user_character_list', row)
    if mc and mc[8] and (not st.get('user_character_costume_list', mc[8])):
        tx.upsert('user_character_costume_list', st.new_row('user_character_costume_list', costume_id=mc[8]))
    if mc and mc[9] and (not st.get('user_weapon_list', mc[9])):
        tx.upsert('user_weapon_list', st.new_row('user_weapon_list', weapon_id=mc[9], rarity_type=1, get_datetime=now_ms(), weapon_upgrade_type=P.Command_Enums_WeaponUpgradeType.Value('Command_Enums_WeaponUpgradeType_Rank')))
    if mc and mc[10] and (not st.get('user_skill_special_list', mc[10])):
        tx.upsert('user_skill_special_list', st.new_row('user_skill_special_list', special_skill_id=mc[10]))
        pr = st.get('user_play_record_list', PR_LB) or st.new_row('user_play_record_list', play_record_type=PR_LB)
        pr.int_value = len(st['user_skill_special_list'])
        tx.upsert('user_play_record_list', pr)

def dungeon_story_restart(st, req, ctx):
    q = req.post_pvt_dungeon_story_restart
    with st.transaction('dungeon/story/restart') as tx:
        if q.HasField('party_info'):
            _save_party_info(st, tx, q.party_info, now_ms())
    r = _resp(tx)
    r.post_pvt_dungeon_story_restart.SetInParent()
    return r
HANDLERS['/api/pvt/dungeon/story/restart'] = dungeon_story_restart
PR_LB = P.Command_Enums_PlayRecordType.Value('Command_Enums_PlayRecordType_LimitBreakCount')
RT_SPECIAL_SKILL = P.Command_Enums_RewardType.Value('Command_Enums_RewardType_SpecialSkill')

def _achieve_set_mission(st, tx, m):
    s = st.get('user_mission_set_list', m[1]) or st.new_row('user_mission_set_list', mission_set_id=m[1])
    k = m[2] // 64
    for pre in ('achieved', 'notified'):
        f = f'{pre}_mission_set_idx_flags{k}'
        setattr(s, f, _flag_or(getattr(s, f), m[2]))
    tx.upsert('user_mission_set_list', s)

LB_GROUP_TYPE = 7           # m_mission_group col 1: a character's Limit Break missions (col 11 = the LB)

def _from_never_crisis(st):
    """True for an account imported from a Never Crisis account.json (editor/nevercrisis.py notes it in info.json)."""
    try:
        meta = json.load(open(os.path.join(st.folder, 'info.json')))
    except (OSError, TypeError, ValueError):
        return False
    return any(str(x).startswith('nevercrisis:') for x in meta.get('sources') or [])

def _special_missions_received_heal(st):
    """On accounts imported from Never Crisis: a Limit Break or summon Pact / Unlock mission group whose skill
    (m_mission_group col 11) the account owns has been received. Imports from before editor/nevercrisis.py moved the
    flag to the field this game reads (FIELD_MOVES) have every is_received_special_mission off, so the game showed
    every LB and summon as having no missions done or set. A group's mission still set on its character or the
    summon slot is cleared, as receiving it does. -> groups marked."""
    if not st.folder or not _from_never_crisis(st):
        return 0
    groups = _mt_index()['groups']
    owned = {k[0] if isinstance(k, tuple) else k for k in st['user_skill_special_list'].keys()}
    marked = []
    for gid, g in groups.items():
        if g[1] in (LB_GROUP_TYPE, SUMMON_PACT_GROUP_TYPE) and g[11] and g[11] in owned:
            row = st.get('user_mission_group_list', gid)
            if row is None or not row.is_received_special_mission:
                marked.append((gid, row))
    if not marked:
        return 0
    done = {gid for gid, _ in marked}
    with st.transaction('special missions already received') as tx:
        for gid, row in marked:
            row = row or st.new_row('user_mission_group_list', mission_group_id=gid)
            row.is_received_special_mission = True
            tx.upsert('user_mission_group_list', row)
        for c in st['user_character_list'].values():
            if c.mission_group_id in done:
                c.mission_group_id = 0
                tx.upsert('user_character_list', c)
        slot = st.one('user_mission_special_summon_list')
        if slot is not None and slot.mission_group_id in done:
            slot.mission_group_id = 0
            tx.upsert('user_mission_special_summon_list', slot)
    print(f'special missions heal: {st.user_id}: {len(marked)} LB / summon mission group(s) marked received')
    return len(marked)

def _summon_pact_on_character_heal(st):
    """Saves from before the summon fix can hold a summon's Pact group as a character's LB mission; clear those."""
    groups = _mt_index()['groups']
    stray = [c for c in st['user_character_list'].values()
             if c.mission_group_id and groups.get(c.mission_group_id, (0, 0))[1] == SUMMON_PACT_GROUP_TYPE]
    if stray:
        with st.transaction('summon pact group left on a character') as tx:
            for c in stray:
                c.mission_group_id = 0
                tx.upsert('user_character_list', c)
    # Saves from before the unlock fix received a summon's Pact / Unlock group without being given the summon.
    unowned = sorted({groups[gid][10] for (gid,), gr in st['user_mission_group_list'].items()
                      if gr.is_received_special_mission and groups.get(gid, (0, 0))[1] == SUMMON_PACT_GROUP_TYPE
                      and groups[gid][10] and st.get('user_summon_list', groups[gid][10]) is None})
    if unowned:
        with st.transaction('summon unlocked but not owned') as tx:
            for sid in unowned:
                tx.upsert('user_summon_list', st.new_row('user_summon_list', summon_id=sid))
    return len(stray) + len(unowned)

def mission_special_receive(st, req, ctx):
    q = req.post_pvt_mission_special_receive
    g = mdx('m_mission_group', 0).get((q.mission_group_id,))
    if not g or not g[11]:
        raise ValueError(f'mission/special/receive: group {q.mission_group_id} has no special skill')
    sk, cid = (g[11], g[10])
    body = P.Command_ApiNetwork_Api_PostPvtMissionSpecialReceiveResponse()
    achieved = []
    with st.transaction('mission/special/receive') as tx:
        new = st.get('user_skill_special_list', sk) is None
        if new:
            tx.upsert('user_skill_special_list', st.new_row('user_skill_special_list', special_skill_id=sk))
        gr = st.get('user_mission_group_list', q.mission_group_id) or st.new_row('user_mission_group_list', mission_group_id=q.mission_group_id)
        gr.is_received_special_mission = True
        tx.upsert('user_mission_group_list', gr)
        summon_pact = g[1] == SUMMON_PACT_GROUP_TYPE
        holder = st.one('user_mission_special_summon_list') if summon_pact else st.get('user_character_list', cid)
        if holder and holder.mission_group_id == q.mission_group_id:
            holder.mission_group_id = 0
            tx.upsert('user_mission_special_summon_list' if summon_pact else 'user_character_list', holder)
        if summon_pact and cid and st.get('user_summon_list', cid) is None:
            # Finishing a summon's Unlock is what makes it owned; without the row the Summon Stream shows it as not owned.
            tx.upsert('user_summon_list', st.new_row('user_summon_list', summon_id=cid))
            mission_event(st, tx, 'ProgressSummonGet', 1)
        if summon_pact:
            if new:
                mission_event(st, tx, 'SetSummonSkillGet', 1, sk)
            _summon_missions_heal(st, tx)
        if new and not summon_pact:
            pr = st.get('user_play_record_list', PR_LB) or st.new_row('user_play_record_list', play_record_type=PR_LB)
            pr.int_value = len(st['user_skill_special_list'])
            tx.upsert('user_play_record_list', pr)
            mission_event(st, tx, 'ProgressCharacterLimitBreakSkillLevelAll', 1)
            mission_event(st, tx, 'ProgressLimitBreakLevel', 1, sk)
            for m in _mt_index().get(MTYPE.get('SetLimitBreakGet', -1), []):
                if m[5] == sk:
                    _achieve_set_mission(st, tx, m)
                    achieved.append(m[0])
        ri = body.reward_info_list.add()
        ri.reward_type = RT_SPECIAL_SKILL
        ri.target_id = sk
        ri.count = 1
    r = _resp(tx, 'post_pvt_mission_special_receive', body)
    for mid in achieved:
        r.common.user.other_info.mission_achieved_list.add().mission_id = mid
    return r
HANDLERS['/api/pvt/mission/special/receive'] = mission_special_receive

from engine import vault as _ES
GUILD_TABLES = [f.name for f in P.Command_Entity_Tables.DESCRIPTOR.fields if f.name.startswith('shared_guild_') and f.name not in ('shared_guild_member_list', 'shared_guild_member_sharing_item_list')]
GUILD_ACC = _ACC
CHAT_KEEP, CHAT_HOLD_S = (200, 9.0)
_GUILD_LOCK = threading.RLock()

def _captured(ctx):
    # Imported account and shared state remain authoritative for guilds and chat.
    return None

def _my_guild(st, cap=None):
    b = st.one('user_guild_base_list')
    if b and b.guild_id:
        return b.guild_id
    if cap is not None:
        for r in cap.common.user.update.user_guild_base_list:
            if r.guild_id:
                return r.guild_id
    return 0

def _gkv_get(name):
    with _ES._SH_LOCK:
        r = _ES._shared(GUILD_ACC).execute('SELECT val FROM kv WHERE name=?', (name,)).fetchone()
    return json.loads(r[0]) if r else None

def _gkv_set(name, val):
    with _ES._SH_LOCK:
        _ES._shared(GUILD_ACC).execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (name, json.dumps(val)))

def _guild_load(gid):
    d = _gkv_get(f'guild:{gid}')
    if not d:
        return None
    out = {}
    for t, rows in d['tables'].items():
        cls = _ES.ROW_CLASS.get(t)
        if cls is None:
            continue
        out[t] = []
        for h in rows:
            r = cls()
            r.ParseFromString(bytes.fromhex(h))
            out[t].append(r)
    return out

def _guild_save(gid, tables):
    _gkv_set(f'guild:{gid}', {'tables': {t: [r.SerializeToString().hex() for r in rows] for t, rows in tables.items()}})

def _guild_seed(st, gid, cap):
    src = {}
    if cap is not None:
        for t in GUILD_TABLES:
            rows = [r for r in getattr(cap.common.user.update, t) if r.guild_id == gid]
            if rows:
                src[t] = rows
    if 'shared_guild_base_list' not in src:
        for t in GUILD_TABLES:
            rows = [r for r in st.tables.get(t, {}).values() if r.guild_id == gid]
            if rows:
                src[t] = rows
    return src

def _guild_get(st, gid, cap):
    with _GUILD_LOCK:
        g = _guild_load(gid)
        if g is None:
            g = _guild_seed(st, gid, cap)
            if g:
                _guild_save(gid, g)
        return g

def _guild_push(st, tx, g):
    for t in GUILD_TABLES:
        tbl = st.tables.get(t)
        if tbl is None:
            continue
        for k in list(tbl.keys()):
            tbl.pop(k)
        for r in g.get(t, []):
            row = type(r)()
            row.CopyFrom(r)
            tx.upsert(t, row)

def _guild_reply(st, tx, cap, body_field=None):
    r = P.Command_ApiNetwork_Api_ApiResponse()
    if cap is not None:
        r.CopyFrom(cap)
    r.common.CopyFrom(_common(tx))
    if body_field:
        getattr(r, body_field).SetInParent()
    return r

def _guild_login_mission(st, tx, member, last_visit):
    """ProgressGuildLogin ("Log in to a guild", "... for 2 days") counts the first guild visit of each game day (or
    since joining), in every open group: the daily missions, and the panels' and events' (#108: only the daily ones
    counted). last_visit is the member's last_active_datetime before this visit."""
    if last_visit > member.join_datetime and game_day(last_visit) >= game_day(now_ms()):
        return False
    mission_event(st, tx, 'ProgressGuildLogin', 1)
    return True

GUILD_LOGIN_BONUS_EXP = 200     # Square's flat daily Guild Login Bonus from 2024-07-19 (it was 100-7,777 at random)

def _guild_login_bonus(st, tx, body):
    """gameplay-settings.json "guild_login_animation": true plays the daily Guild Login Bonus on the first guild visit
    of the day and adds its Guild EXP. Off by default, as ECR has always skipped it."""
    _grant_guild_exp(st, tx, RT_GUILD_EXP, GUILD_LOGIN_BONUS_EXP)
    ex = st.one('shared_guild_exp_list')
    if ex is not None:                               # the reply already holds the guild's rows: show the new EXP
        ex.level_exp += GUILD_LOGIN_BONUS_EXP
        tx.upsert('shared_guild_exp_list', ex)
    ri = body.guild_login_bonus_reward_infos.add()
    ri.reward_type, ri.count = RT_GUILD_EXP, GUILD_LOGIN_BONUS_EXP
    body.guild_login_bonus_direction_type = P.Command_Enums_GuildLoginBonusDirectionType_Normal

def _guild_view(field, edit=None):

    def h(st, req, ctx):
        cap = _captured(ctx)
        gid = _my_guild(st, cap)
        with st.transaction(field) as tx:
            if gid:
                with _GUILD_LOCK:
                    g = _guild_get(st, gid, cap)
                    if g and edit:
                        edit(st, req, g)
                        _guild_save(gid, g)
                if g:
                    _guild_push(st, tx, g)
                own = last_visit = None
                if g:
                    own = st.one('shared_guild_member_list')
                    if own is not None and own.guild_id == gid:
                        if field == 'post_pvt_guild_top':
                            last_visit, own.last_active_datetime = own.last_active_datetime, now_ms()
                        tx.upsert('shared_guild_member_list', own)
                    ub = st.one('user_guild_base_list')
                    if ub is not None:
                        tx.upsert('user_guild_base_list', ub)
                bonus = None
                if g and field == 'post_pvt_guild_top':
                    if last_visit is not None and _guild_login_mission(st, tx, own, last_visit) and CFG.get('guild_login_animation') is True:
                        bonus = P.Command_ApiNetwork_Api_PostPvtGuildTopResponse()
                        _guild_login_bonus(st, tx, bonus)
                    mission_event(st, tx, 'SetGuildJoin', 1)
        r = _guild_reply(st, tx, cap, field)
        if field == 'post_pvt_guild_top':
            b = r.post_pvt_guild_top
            b.ClearField('guild_login_bonus_reward_infos')
            b.ClearField('guild_login_bonus_direction_type')
            if gid and bonus is not None:
                b.guild_login_bonus_reward_infos.extend(bonus.guild_login_bonus_reward_infos)
                b.guild_login_bonus_direction_type = bonus.guild_login_bonus_direction_type
        if field == 'post_pvt_guild_get_self' and gid and g:
            base = next(iter(g.get('shared_guild_base_list', [])), None)
            if base is not None:
                gi = r.post_pvt_guild_get_self.guild_info
                if base.name:
                    gi.guild_name = base.name
                gi.introduction = base.introduction
        return r
    return h

def _edit_message(st, req, g):
    base = next(iter(g.get('shared_guild_base_list', [])), None)
    if base is None:
        raise ValueError('guild/message/edit: no guild base row')
    base.message = req.post_pvt_guild_message_edit.message
    base.message_edit_datetime = now_ms()

def _chat_load(gid):
    return _gkv_get(f'guildchat:{gid}')

def _chat_save(gid, c):
    _gkv_set(f'guildchat:{gid}', c)

def _chat_msgs(c, after=None):
    out = []
    for i, h in enumerate(c['msgs']):
        seq = c['base'] + i
        if after is not None and seq < after:
            continue
        m = P.Command_Entity_ChatMessage()
        m.ParseFromString(bytes.fromhex(h))
        out.append(m)
    return out

def _chat_token(c):
    return f"s:{c['base'] + len(c['msgs'])}"

def _chat_get(gid, cap_start=None):
    with _GUILD_LOCK:
        c = _chat_load(gid)
        if c is None:
            c = {'base': 0, 'msgs': []}
            if cap_start is not None:
                c['msgs'] = [m.SerializeToString().hex() for m in cap_start.post_pvt_chat_guild_start.chat_messages][-CHAT_KEEP:]
            _chat_save(gid, c)
        return c

def _chat_append(gid, msg):
    with _GUILD_LOCK:
        c = _chat_get(gid)
        c['msgs'].append(msg.SerializeToString().hex())
        if len(c['msgs']) > CHAT_KEEP:
            drop = len(c['msgs']) - CHAT_KEEP
            c['msgs'] = c['msgs'][drop:]
            c['base'] += drop
        _chat_save(gid, c)

def _sender(st, info):
    p = st.one('user_profile_list')
    info.display_user_id = display_id_for(st.user_id)
    if p:
        info.user_name = p.name
        info.icon_honor_id = p.icon_honor_id

def chat_guild_start(st, req, ctx):
    cap = _captured(ctx)
    gid = _my_guild(st, cap)
    with st.transaction('chat/guild/start') as tx:
        pass
    r = P.Command_ApiNetwork_Api_ApiResponse()
    r.common.CopyFrom(_common(tx))
    b = r.post_pvt_chat_guild_start
    b.SetInParent()
    if gid:
        c = _chat_get(gid, cap)
        msgs = _chat_msgs(c)
        b.chat_messages.extend(msgs)
        if msgs:
            b.oldest_message_id = msgs[0].message_id
        b.latest_chat_token = _chat_token(c)
    return r

def _chat_send(kind):

    def h(st, req, ctx):
        gid = _my_guild(st, _captured(ctx))
        t = now_ms()
        with st.transaction(f'chat/guild/send/{kind}') as tx:
            if gid:
                mission_event(st, tx, 'SetGuildSendChatMessage', 1)   # a text or a stamp, both are chat messages
        if gid:
            m = P.Command_Entity_ChatMessage()
            m.sent_datetime = t
            if kind == 'text':
                m.chat_message_type = P.Command_Enums_ChatMessageType.Value('Command_Enums_ChatMessageType_Text')
                _sender(st, m.body.text.sender_user_info)
                m.body.text.text = req.post_pvt_chat_guild_send_text.text
            else:
                m.chat_message_type = P.Command_Enums_ChatMessageType.Value('Command_Enums_ChatMessageType_Stamp')
                _sender(st, m.body.stamp.sender_user_info)
                m.body.stamp.stamp_id = req.post_pvt_chat_guild_send_stamp.stamp_id
            with _GUILD_LOCK:
                c = _chat_get(gid)
                m.message_id = f"{t}-{c['base'] + len(c['msgs'])}"
                _chat_append(gid, m)
        r = P.Command_ApiNetwork_Api_ApiResponse()
        r.common.CopyFrom(_common(tx))
        getattr(r, f'post_pvt_chat_guild_send_{kind}').SetInParent()
        return r
    return h

def chat_guild_check(st, req, ctx):
    gid = _my_guild(st, None)
    r = P.Command_ApiNetwork_Api_ApiResponse()
    b = r.post_chat_guild_check
    b.SetInParent()
    if not gid:
        return r
    tok = req.post_chat_guild_check.latest_chat_token
    c = _chat_get(gid)
    if not tok.startswith('s:'):
        b.latest_chat_token = _chat_token(c)
        return r
    try:
        after = int(tok[2:])
    except ValueError:
        after = c['base'] + len(c['msgs'])
    deadline = time.time() + (CHAT_HOLD_S if (ctx or {}).get('unlocked') else 0)
    while True:
        new = _chat_msgs(c, after)
        if new or time.time() >= deadline:
            break
        time.sleep(0.5)
        c = _chat_load(gid) or c
    b.chat_messages.extend(new)
    b.latest_chat_token = _chat_token(c)
    return r
chat_guild_check.unlocked = True
import sqlite3 as _sq3
import glob as _glob

def _guild_members_here(gid):
    out = {}
    for db in _glob.glob(os.path.join(_ACC, '*', 'save.db')):
        folder = os.path.dirname(db)
        live = getattr(State, 'LIVE', {}).get(os.path.realpath(folder))
        rows = []
        if live is not None:
            uid = str(live.user_id)
            try:
                rows = list(live.tables.get('shared_guild_member_list', {}).values())
            except RuntimeError:
                rows = []
        else:
            try:
                c = _sq3.connect('file:' + db + '?mode=ro', uri=True)
                try:
                    r = c.execute("SELECT val FROM meta WHERE key='user_id'").fetchone()
                    uid = r[0] if r else None
                    for pb, in c.execute("SELECT pb FROM rows WHERE tbl='shared_guild_member_list'"):
                        m = P.Command_Entity_SharedGuildMember()
                        m.ParseFromString(pb)
                        rows.append(m)
                finally:
                    c.close()
            except Exception as e:
                print(f'guild/member/list: {db}: {e!r}')
                continue
        for m in rows:
            if uid and m.guild_id == gid and (str(m.user_id) == str(uid)):
                out[str(uid)] = m
    return out

def _card_here(uid):
    i = _list_info(uid)
    if i is not None:
        return i
    f = _folder(uid)
    if not f:
        return None
    from engine.vault import read_summary
    sm = read_summary(f) or {}
    i = P.Command_Entity_UserListInfo()
    if sm.get('name'):
        i.player_name = sm['name']
    for k in ('top_honor_id', 'bottom_honor_id', 'background_honor_id', 'icon_honor_id'):
        setattr(i, k, sm.get(k) or 0)
    if sm.get('exp') is not None:
        i.user_rank = max((r[0] for r in md('m_user_rank') if r[2] <= sm['exp']), default=1)
    i.combat_power = sm.get('combat_power') or 0
    return i
_AWARDS = globals().get('_AWARDS', {})

def _awards_of(uid, st=None):
    if st is None:
        f = _folder(uid)
        if not f:
            return []
        st = getattr(State, 'LIVE', {}).get(os.path.realpath(f))
    if st is not None:
        return [(a.award_id, a.award_count) for a in st['user_award_list'].values()]
    import sqlite3
    from engine import vault as _es
    from engine.vault import play_state_mtime
    fp = os.path.join(f, _es.DB)
    mt = play_state_mtime(f) or os.path.getmtime(os.path.join(f, 'base.pb'))
    if _AWARDS.get(uid, (None,))[0] == mt:
        return _AWARDS[uid][1]
    if os.path.isfile(fp):
        c = sqlite3.connect(f'file:{fp}?mode=ro', uri=True, timeout=5)
        try:
            rows = c.execute("SELECT pb FROM rows WHERE tbl='user_award_list'").fetchall()
        finally:
            c.close()
        out = []
        for pb, in rows:
            r = _es.ROW_CLASS['user_award_list']()
            r.ParseFromString(pb)
            out.append((r.award_id, r.award_count))
    else:
        o = _other_state(uid)
        out = [(a.award_id, a.award_count) for a in o['user_award_list'].values()] if o is not None else []
    _AWARDS[uid] = (mt, out)
    return out

def guild_member_list(st, req, ctx):
    gid = _my_guild(st, None)
    cap = _captured(ctx)
    body = P.Command_ApiNetwork_Api_PostPvtGuildMemberListResponse()
    with st.transaction('post_pvt_guild_member_list') as tx:
        pass
    if gid:
        here = _guild_members_here(gid)
        if str(st.user_id) not in here:
            own = st.one('shared_guild_member_list')
            if own is not None and own.guild_id == gid:
                here[str(st.user_id)] = own
        disp_uid = _displays()
        uid_disp = _uid_to_display(disp_uid)
        capd = {e.user_list_info.display_user_id: e for e in (cap.post_pvt_guild_member_list.guild_member_list_infos if cap is not None else [])}
        for uid, m in here.items():
            card = _card_here(uid)
            if card is None:
                continue
            e = body.guild_member_list_infos.add()
            e.user_list_info.CopyFrom(card)
            try:
                if not len(e.user_list_info.award_info_list):
                    for aid, n in _awards_of(uid, st if str(uid) == str(st.user_id) else None):
                        ai = e.user_list_info.award_info_list.add()
                        ai.award_id = aid
                        ai.award_count = n
            except Exception as ex:
                print(f'guild/member/list awards for {uid} failed: {ex!r}')
            old = capd.get(uid_disp.get(uid, ''))
            e.guild_role_type = m.guild_role_type
            if old is not None:
                e.provide_level_exp = old.provide_level_exp
                e.provide_bonus_level_exp = old.provide_bonus_level_exp
        for d, old in capd.items():
            if d in disp_uid:
                continue
            body.guild_member_list_infos.add().CopyFrom(old)
    return _resp(tx, 'post_pvt_guild_member_list', body)
HANDLERS.update({'/api/pvt/guild/top': _guild_view('post_pvt_guild_top'), '/api/pvt/guild/sync': _guild_view('post_pvt_guild_sync'), '/api/pvt/guild/get/self': _guild_view('post_pvt_guild_get_self'), '/api/pvt/guild/shop/reset/check': _guild_view(None), '/api/pvt/guild/message/edit': _guild_view('post_pvt_guild_message_edit', _edit_message), '/api/pvt/event/guild/ranking/top': _guild_view(None), '/api/pvt/event/guild/ranking/battle/enemy/get': _guild_view(None), '/api/pvt/chat/guild/start': chat_guild_start, '/api/pvt/chat/guild/send/text': _chat_send('text'), '/api/pvt/chat/guild/send/stamp': _chat_send('stamp'), '/api/chat/guild/check': chat_guild_check, '/api/pvt/guild/member/list': guild_member_list})

TUT_GUIDE = P.Command_Enums_TutorialStepType.Value('Command_Enums_TutorialStepType_GuideMission')
TUT_GACHA_STEP = 400010101
_TUT_INITIAL_T = P.Command_Enums_TutorialStepType.Value('Command_Enums_TutorialStepType_Initial')
_TUT_INITIAL_DONE = 210
_TUT_FINISH_TIMES = tuple((P.Command_Enums_UserTimeType.Value('Command_Enums_UserTimeType_' + n) for n in ('TutorialFinish', 'BeginnerGachaTriggerDatetime')))

def _tut_step(st, resp, step_type, step):
    if not step:
        return
    row = st.get('user_tutorial_step_list', step_type) or st.new_row('user_tutorial_step_list', tutorial_step_type=step_type)
    row.current_step = step
    with st.transaction('tutorial/step') as tx:
        tx.upsert('user_tutorial_step_list', row)
        if step_type == _TUT_INITIAL_T and step >= _TUT_INITIAL_DONE:
            have = {t.user_time_type for t in st.tables['user_time_list'].values()}
            when = now_ms()
            for ty in _TUT_FINISH_TIMES:
                if ty not in have:
                    resp.common.user.update.user_time_list.add().CopyFrom(tx.upsert('user_time_list', st.new_row('user_time_list', user_time_type=ty, datetime=when)))
    lst = resp.common.user.update.user_tutorial_step_list
    for r in lst:
        if r.tutorial_step_type == step_type:
            r.CopyFrom(row)
            return
    lst.add().CopyFrom(row)
TUT_CRAFT = P.Command_Enums_TutorialStepType.Value('Command_Enums_TutorialStepType_Craft')

def tutorial_progress_step(st, req, ctx):
    q = req.post_pvt_tutorial_progress_step
    with st.transaction('tutorial/progress/step') as tx:
        if q.tutorial_step_type == TUT_CRAFT and q.step == 5 and (st.get('user_materia_recipe_list', 220000101) is None):
            grant_reward(st, tx, 3000297, 1)
            tx.upsert('user_materia_recipe_list', st.new_row('user_materia_recipe_list', materia_recipe_id=220000101))
    r = _resp(tx, 'post_pvt_tutorial_progress_step')
    _tut_step(st, r, q.tutorial_step_type, q.step)
    return r

def _tut_wrap(name, inner, has_step=True):
    wfield = f'post_pvt_tutorial_{name}'

    def h(st, req, ctx):
        w = getattr(req, wfield)
        iq = P.Command_ApiNetwork_Api_ApiRequest()
        getattr(iq, f'post_pvt_{name}').CopyFrom(getattr(w, f'{name}_request'))
        ir = HANDLERS[inner](st, iq, dict(ctx or {}))
        r = P.Command_ApiNetwork_Api_ApiResponse()
        r.common.CopyFrom(ir.common)
        b = getattr(r, wfield)
        b.SetInParent()
        getattr(b, f'{name}_response').CopyFrom(getattr(ir, f'post_pvt_{name}'))
        if name == 'story_battle_end':
            br = getattr(b, f'{name}_response').battle_result
            br.reward_infos.extend(br.first_reward_infos)
            br.ClearField('first_reward_infos')
        if has_step:
            b.tutorial_progress_step_response.SetInParent()
            _tut_step(st, r, w.tutorial_progress_step_request.tutorial_step_type, w.tutorial_progress_step_request.step)
        return r
    return h

def tutorial_gacha_draw(st, req, ctx):
    iq = P.Command_ApiNetwork_Api_ApiRequest()
    iq.post_pvt_gacha_draw.gacha_step_id = TUT_GACHA_STEP
    ir = HANDLERS['/api/pvt/gacha/draw'](st, iq, dict(ctx or {}))
    r = P.Command_ApiNetwork_Api_ApiResponse()
    r.common.CopyFrom(ir.common)
    b = r.post_pvt_tutorial_gacha_draw
    b.SetInParent()
    b.gacha_result_list.extend(ir.post_pvt_gacha_draw.gacha_result_list)
    b.gacha_direction_lot_id = ir.post_pvt_gacha_draw.gacha_direction_lot_id
    t = now_ms()
    with st.transaction('tutorial/gacha/draw') as tx:
        for gid in (902901, 902903):
            g = st.get('user_mission_group_list', gid) or st.new_row('user_mission_group_list', mission_group_id=gid)
            g.last_reset_datetime = t
            tx.upsert('user_mission_group_list', g)
    for f, rows in tx.up.ListFields():
        for row in rows:
            getattr(r.common.user.update, f.name).add().CopyFrom(row)
    _tut_step(st, r, TUT_GUIDE, req.post_pvt_tutorial_gacha_draw.step or 1)
    return r
_TUT_NAME = HANDLERS.get('/api/pvt/tutorial/register/user/name')
TUT_INITIAL = P.Command_Enums_TutorialStepType.Value('Command_Enums_TutorialStepType_Initial')

def tutorial_register_user_name_step(st, req, ctx):
    r = _TUT_NAME(st, req, ctx)
    _tut_step(st, r, TUT_INITIAL, req.post_pvt_tutorial_register_user_name.step)
    return r
HANDLERS.update({'/api/pvt/tutorial/progress/step': tutorial_progress_step, '/api/pvt/tutorial/story/battle/start': _tut_wrap('story_battle_start', '/api/pvt/story/battle/start'), '/api/pvt/tutorial/story/battle/end': _tut_wrap('story_battle_end', '/api/pvt/story/battle/end'), '/api/pvt/tutorial/dungeon/story/start': _tut_wrap('dungeon_story_start', '/api/pvt/dungeon/story/start'), '/api/pvt/tutorial/dungeon/story/treasure': _tut_wrap('dungeon_story_treasure', '/api/pvt/dungeon/story/treasure', has_step=False), '/api/pvt/tutorial/dungeon/story/end': _tut_wrap('dungeon_story_end', '/api/pvt/dungeon/story/end'), '/api/pvt/tutorial/story/result': _tut_wrap('story_result', '/api/pvt/story/result'), '/api/pvt/tutorial/gacha/draw': tutorial_gacha_draw})
if _TUT_NAME is not None and getattr(_TUT_NAME, '__name__', '') != 'tutorial_register_user_name_step':
    HANDLERS['/api/pvt/tutorial/register/user/name'] = tutorial_register_user_name_step

def notice_check(st, req, ctx):
    with st.transaction('notice/check') as tx:
        pass
    r = _resp(tx)
    b = r.post_pvt_notice_check
    b.SetInParent()
    gid = _my_guild(st, None)
    if gid:
        c = _chat_load(gid)
        if c and c['msgs']:
            m = P.Command_Entity_ChatMessage()
            m.ParseFromString(bytes.fromhex(c['msgs'][-1]))
            b.chat_guild_latest_message_id = m.message_id
    try:
        if _soc().get(str(st.user_id), {}).get('in'):
            b.is_receive_friend_request = True
    except Exception as e:
        print('notice/check: social read failed:', e)
    return r

def _empty(field):

    def h(st, req, ctx):
        with st.transaction(field) as tx:
            pass
        r = _resp(tx)
        getattr(r, field).SetInParent()
        return r
    return h

def inquiry_log_send(st, req, ctx):
    import uuid
    with st.transaction('inquiry/log/send') as tx:
        pass
    r = _resp(tx, 'post_inquiry_log_send')
    r.post_inquiry_log_send.uuid = str(uuid.uuid4())
    return r

def transfer_info(st, req, ctx):
    cap = _captured(ctx)
    with st.transaction('transfer/info') as tx:
        pass
    r = P.Command_ApiNetwork_Api_ApiResponse()
    if cap is not None:
        r.CopyFrom(cap)
    r.common.CopyFrom(_common(tx))
    b = r.post_pvt_transfer_info
    b.SetInParent()
    b.paid_stone_count = next((s.count for s in st.other_info.user_stone_list if s.stone_id == 1), 0) if st.other_info else 0
    return r
HANDLERS.update({'/api/pvt/notice/check': notice_check, '/api/pvt/highwind/top': _empty('post_pvt_highwind_top'), '/api/pvt/personal/message/list': _empty('post_pvt_personal_message_list'), '/api/pvt/store/purchase/restart/steam': _empty('post_pvt_store_purchase_restart_steam'), '/api/pvt/log/send': _empty('post_pvt_log_send'), '/api/inquiry/log/send': inquiry_log_send, '/api/pvt/transfer/info': transfer_info})

def _deny(add):
    field = 'post_pvt_user_deny_add' if add else 'post_pvt_user_deny_remove'

    def h(st, req, ctx):
        disp = getattr(req, field).display_user_id
        uid = str(st.user_id)
        with _SOCIAL_LOCK:
            soc = _soc()
            me = _me(soc, uid)
            other = _displays().get(disp)
            deny = me.setdefault('deny', [])
            if other and other != uid:
                if add and other not in deny:
                    deny.append(other)
                    you = _me(soc, other)
                    for a, b in ((me, other), (you, uid)):
                        for k in ('friends', 'out', 'in'):
                            if b in a[k]:
                                a[k].remove(b)
                elif not add and other in deny:
                    deny.remove(other)
                _jsave(_SOC, soc)
        with st.transaction(field) as tx:
            pass
        r = _resp(tx)
        getattr(r, field).SetInParent()
        return r
    return h

def user_deny_list(st, req, ctx):
    me = _soc().get(str(st.user_id), {})
    with st.transaction('user/deny/list') as tx:
        pass
    r = _resp(tx)
    b = r.post_pvt_user_deny_list
    b.SetInParent()
    for u in me.get('deny', []):
        i = _list_info(u)
        if i is not None:
            b.user_list_infos.add().CopyFrom(i)
    return r
CHAT_PAGE = 30

def chat_guild_list(st, req, ctx):
    gid = _my_guild(st, _captured(ctx))
    oid = req.post_pvt_chat_guild_list.oldest_message_id
    with st.transaction('chat/guild/list') as tx:
        pass
    r = _resp(tx)
    b = r.post_pvt_chat_guild_list
    b.SetInParent()
    if gid:
        msgs = _chat_msgs(_chat_get(gid))
        idx = next((i for i, m in enumerate(msgs) if m.message_id == oid), 0)
        page = msgs[max(0, idx - CHAT_PAGE):idx]
        b.chat_messages.extend(page)
        if page:
            b.oldest_message_id = page[0].message_id
    return r
HANDLERS.update({'/api/pvt/chat/guild/list': chat_guild_list, '/api/pvt/chat/guild/report': _empty('post_pvt_chat_guild_report'), '/api/pvt/debug/error': _empty('post_pvt_debug_error'), '/api/pvt/advertising/setting/update': _empty('post_pvt_advertising_setting_update'), '/api/pvt/advertisement': _empty('post_pvt_advertisement'), '/api/pvt/serial/register': _empty('post_pvt_serial_register'), '/api/pvt/personal/message/detail': _empty('post_pvt_personal_message_detail'), '/api/pvt/user/deny/add': _deny(True), '/api/pvt/user/deny/remove': _deny(False), '/api/pvt/user/deny/list': user_deny_list})
_OWN = {9: ('user_character_costume_list', 'costume_id'), 19: ('user_home_background_list', 'home_background_id'), 7: ('user_materia_recipe_list', 'materia_recipe_id'), 14: ('user_skill_special_list', 'special_skill_id'), 33: ('user_skill_overaccel_list', 'overaccel_skill_id'), 37: ('user_ability_enemy_list', 'ability_enemy_id')}

# Gear Exchange rows that sell one costume (the rows added to shop 301001) show as bought once the costume is owned.
# The client greys a row out only from its purchase count, so a costume pulled from a draw would otherwise still look
# buyable there. The original costume-and-weapon rows are left alone: owning the costume does not give the weapon.
GEAR_SHOP = 301001
_GEAR_IDX = {}

def _gear_rows_by_costume():
    si = md('m_shop_item')
    if _GEAR_IDX.get('src') is not si:
        rels = _rs_index()['rel']
        rw = mdx('m_reward', 0)
        by = {}
        for it in si:
            if it[1] != GEAR_SHOP:
                continue
            rr = rels.get(it[6], [])
            r = rw.get((rr[0][2],)) if len(rr) == 1 else None
            if r and r[1] == 9:
                by.setdefault(r[2], []).append(it[0])
        _GEAR_IDX.update(src=si, by=by)
    return _GEAR_IDX['by']

def _gear_mark_owned(st, tx, costume_ids):
    by = _gear_rows_by_costume()
    n = 0
    for c in costume_ids:
        for sid in by.get(c, ()):
            us = st.get('user_shop_item_list', sid)
            if us is not None and us.purchase_count:
                continue
            us = us or st.new_row('user_shop_item_list', shop_item_id=sid)
            us.purchase_count = 1
            tx.upsert('user_shop_item_list', us)
            n += 1
    return n

def _gear_owned_heal(st):
    """Costumes owned before their Gear Exchange row existed (or granted by a path that skips grant_reward)."""
    owned = [r.costume_id for r in st['user_character_costume_list'].values()]
    by = _gear_rows_by_costume()
    if not any(c in by for c in owned):
        return 0
    with st.transaction('gear exchange: owned costumes') as tx:
        n = _gear_mark_owned(st, tx, owned)
    if n:
        print(f'gear exchange heal: {st.user_id} {n} row(s) marked owned')
    return n

# #95: title plates once won only by ranking placement are sold once each (recipe "shop_item_additions"). A plate the
# player already has shows as bought there, as an owned costume does in the Gear Exchange: one-time rows (max 1, no
# refill) selling one title.
_PLATE_IDX = {}

def _plate_rows_by_honor():
    si = md('m_shop_item')
    if _PLATE_IDX.get('src') is not si:
        rels = _rs_index()['rel']
        rw = mdx('m_reward', 0)
        by = {}
        for it in si:
            if it[3] != 1 or (len(it) > 17 and it[17]):
                continue
            rr = rels.get(it[6], [])
            r = rw.get((rr[0][2],)) if len(rr) == 1 else None
            if r and r[1] == RT_HONOR:
                by.setdefault(r[2], []).append(it[0])
        _PLATE_IDX.update(src=si, by=by)
    return _PLATE_IDX['by']

def _plate_mark_owned(st, tx, honor_ids):
    by = _plate_rows_by_honor()
    n = 0
    for h in honor_ids:
        for sid in by.get(h, ()):
            us = st.get('user_shop_item_list', sid)
            if us is not None and us.purchase_count:
                continue
            us = us or st.new_row('user_shop_item_list', shop_item_id=sid)
            us.purchase_count = 1
            tx.upsert('user_shop_item_list', us)
            n += 1
    return n

def _plate_owned_heal(st):
    owned = [r.honor_id for r in st['user_honor_list'].values()]
    by = _plate_rows_by_honor()
    if not any(h in by for h in owned):
        return 0
    with st.transaction('title plates: owned plates') as tx:
        n = _plate_mark_owned(st, tx, owned)
    if n:
        print(f'title plate heal: {st.user_id} {n} row(s) marked owned')
    return n

def _guild_mutate(st, fn):
    gid = _my_guild(st, None)
    if not gid:
        return False
    with _GUILD_LOCK:
        g = _guild_load(gid)
        if g is None:
            return False
        fn(g, gid)
        _guild_save(gid, g)
    return True

def _grant_guild_exp(st, tx, rtype, count):

    def fn(g, gid):
        rows = g.setdefault('shared_guild_exp_list', [])
        if not rows:
            r = P.Command_Entity_SharedGuildExp()
            r.guild_id = gid
            rows.append(r)
        if rtype == RT_GUILD_EXP:
            rows[0].level_exp += count
        else:
            rows[0].bonus_level_exp += count
    _guild_mutate(st, fn)

def _grant_reward_more(st, tx, rtype, target, count):
    t = now_ms()
    if rtype in _OWN:
        table, col = _OWN[rtype]
        if st.get(table, target) is None:
            tx.upsert(table, st.new_row(table, **{col: target}))
        if rtype == 9:
            _gear_mark_owned(st, tx, (target,))
        if rtype == 33:
            _oacc_fill(st, tx, target)
        return True
    if rtype == 29:
        if st.get('user_highwind_key_item_list', target) is None:
            tx.upsert('user_highwind_key_item_list', st.new_row('user_highwind_key_item_list', highwind_key_item_id=target, get_datetime=t, rank_upgrade_type=1))
        return True
    if rtype == 27:
        cs = mdx('m_chat_stamp', 0).get((target,))
        if not cs:
            return False
        row = st.get('user_chat_stamp_group_list', cs[1]) or st.new_row('user_chat_stamp_group_list', chat_stamp_group_id=cs[1])
        row.chat_stamp_idx_flags0 |= 1 << cs[2]
        tx.upsert('user_chat_stamp_group_list', row)
        return True
    if rtype == RT_EVENT_IDLING_LEVEL:
        _idling_level_up(st, tx, target, count)
        return True
    if rtype == 21:
        row = st.get('user_season_pass_list', target) or st.new_row('user_season_pass_list', season_pass_id=target)
        row.progress_point += count
        tx.upsert('user_season_pass_list', row)
        return True
    if rtype == 34:
        mm = mdx('m_memoria', 0).get((target,))
        if not mm:
            return False
        if st.get('user_memoria_list', target) is not None:
            print(f'grant_reward: memoria {target} already owned - overlap exchange not modelled')
            return True
        tx.upsert('user_memoria_list', st.new_row('user_memoria_list', memoria_id=target, fragment_count=mm[8], fragment_first_get_datetime=t, equipable_datetime=t))
        return True
    if rtype == 24:

        def fn(g, gid):
            rows = g.setdefault('shared_guild_item_list', [])
            r = next((x for x in rows if x.guild_item_id == target), None)
            if r is None:
                r = P.Command_Entity_SharedGuildItem()
                r.guild_id = gid
                r.guild_item_id = target
                r.get_datetime = t
                rows.append(r)
            r.count += count
            r.total_obtain_count += count
            r.last_get_datetime = t
        return _guild_mutate(st, fn)
    if rtype == 11:
        return True
    return False

def season_pass_receive(st, req, ctx):
    q = req.post_pvt_season_pass_receive
    sp = mdx('m_season_pass', 0).get((q.season_pass_id,))
    body = P.Command_ApiNetwork_Api_PostPvtSeasonPassReceiveResponse()
    with st.transaction('season/pass/receive') as tx:
        row = st.get('user_season_pass_list', q.season_pass_id) or st.new_row('user_season_pass_list', season_pass_id=q.season_pass_id)
        if sp:
            level = row.progress_point // max(1, sp[5]) + 1
            item = next((r[0] for r in md('m_item_season_pass') if r[1] == q.season_pass_id), None)
            premium = bool(item and st.get('user_item_list', item) and (st.get('user_item_list', item).count > 0))
            for info in q.season_pass_reward_info_list:
                s = mdx('m_season_pass_step', 0).get((info.season_pass_step_id,))
                if not s or s[1] != sp[1] or s[4] > level or (s[2] == 2 and (not premium)):
                    continue
                pre = 'received' if s[2] == 1 else 'extra_received'
                f = f'{pre}_step_idx_flags{s[3] // 64}'
                bit = 1 << s[3] % 64
                if not hasattr(row, f) or getattr(row, f) & bit:
                    continue
                setattr(row, f, _flag_or(getattr(row, f), s[3]))
                if s[8]:
                    grant_reward_set(st, tx, s[8], 1, body.reward_info_list)
        tx.upsert('user_season_pass_list', row)
    return _resp(tx, 'post_pvt_season_pass_receive', body)
HANDLERS['/api/pvt/season/pass/receive'] = season_pass_receive

def _season_premium_heal(st):
    """The premium track was bought with real money, which can't happen any more: every account owns each season's
    premium pass item (m_item_season_pass), so the premium rewards of any season that is open can be claimed."""
    want = [r[0] for r in md('m_item_season_pass') if not (st.get('user_item_list', r[0]) and st.get('user_item_list', r[0]).count > 0)]
    if not want:
        return 0
    with st.transaction('season pass: premium for every season') as tx:
        for item in want:
            add_item(st, tx, item, 1)
    print(f'season pass premium: {st.user_id} {len(want)} pass(es) unlocked')
    return len(want)
_ES.KEYS['user_event_drama_selection_list'] = ['event_drama_selection_id']
_FULLP = {}

def _other_state(uid):
    f = _folder(uid)
    if not f:
        return None
    live = getattr(State, 'LIVE', {}).get(os.path.realpath(f))
    if live is not None:
        return live
    from engine.vault import play_state_mtime
    mt = play_state_mtime(f) or os.path.getmtime(os.path.join(f, 'base.pb'))
    if _FULLP.get(uid, (None,))[0] != mt:
        _FULLP[uid] = (mt, State.load(f))
    return _FULLP[uid][1]

def _full_party_info(o, into):
    party = next((p for p in o['user_party_list'].values() if p.is_active), None)
    if party is None:
        return False
    mem = sorted((m for m in o['user_party_member_list'].values() if m.party_member_id // 10 == party.party_id), key=lambda m: m.idx)
    W = lambda wid: o.get('user_weapon_list', wid) if wid else None
    M = lambda mid: o.get('user_materia_list', mid) if mid else None
    for m in mem:
        if not m.character_id:
            continue
        fm = into.party_member_list.add()
        fm.idx = m.idx
        fm.is_active = m.is_active
        fm.party_role_type = m.party_role_type
        c = o.get('user_character_list', m.character_id)
        if c:
            fm.character.CopyFrom(c)
        for dst, row in (('weapon0', W(m.weapon_id0)), ('weapon1', W(m.weapon_id1)), ('sub_weapon0', W(m.sub_weapon_id0)), ('sub_weapon1', W(m.sub_weapon_id1)), ('sub_weapon2', W(m.sub_weapon_id2)), ('legendary_weapon0', W(m.legendary_weapon_id0)), ('active_materia0', M(m.active_materia_id0)), ('active_materia1', M(m.active_materia_id1)), ('active_materia2', M(m.active_materia_id2))):
            if row is not None:
                getattr(fm, dst).CopyFrom(row)
        for f in ('special_skill_id', 'costume_id', 'display_weapon_id', 'display_costume_id', 'overaccel_skill_id', 'is_costume_skill_active', 'sub_costume_id0', 'sub_costume_id1'):
            setattr(fm, f, getattr(m, f))
    if party.memoria_id0:
        mr = o.get('user_memoria_list', party.memoria_id0)
        if mr:
            into.party_memoria.memoria0.CopyFrom(mr)
    into.user_growth_board_group_list.extend(o['user_growth_board_group_list'].values())
    into.user_highwind_key_item_effect_group_list.extend(o['user_highwind_key_item_effect_group_list'].values())
    return True

def damage_challenge_ranking_list(st, req, ctx):
    q = req.post_pvt_damage_challenge_ranking_list
    bid = str(q.damage_challenge_battle_id)
    board = _scores_sync(st)
    rows = sorted(((u, sc.get(bid, 0)) for u, sc in board.items() if sc.get(bid, 0) > 0), key=lambda x: -x[1])
    with st.transaction('damage/challenge/ranking/list') as tx:
        pass
    r = _resp(tx)
    b = r.post_pvt_damage_challenge_ranking_list
    b.SetInParent()
    start = max(1, q.start_rank or 1)
    prev, rank = (None, 0)
    for i, (u, sc) in enumerate(rows):
        if sc != prev:
            rank = i + 1
            prev = sc
        if i + 1 < start:
            continue
        info = _list_info(u)
        e = b.high_score_user_info_list.add()
        e.rank = rank
        e.high_score = sc
        if info is not None:
            e.display_user_id = info.display_user_id
            e.player_name = info.player_name
            e.top_honor_id, e.bottom_honor_id, e.background_honor_id, e.icon_honor_id = (info.top_honor_id, info.bottom_honor_id, info.background_honor_id, info.icon_honor_id)
            try:
                o = st if str(u) == str(st.user_id) else _other_state(u)
                if o is not None:
                    _full_party_info(o, e.party_info)
                    for a in o['user_award_list'].values():
                        ai = e.award_info_list.add()
                        ai.award_id = a.award_id
                        ai.award_count = a.award_count
            except Exception as ex:
                print(f'ranking party for {u} failed: {ex!r}')
        if len(b.high_score_user_info_list) >= 50:
            break
    return r

def event_select_drama(st, req, ctx):
    q = req.post_pvt_event_select_drama
    with st.transaction('event/select/drama') as tx:
        tbl = st.tables['user_event_drama_selection_list']
        for k in [k for k, v in tbl.items() if k != (v.event_drama_selection_id,)]:
            v = tbl.pop(k)
            if (v.event_drama_selection_id,) not in tbl:
                tbl[v.event_drama_selection_id,] = v
        row = st.get('user_event_drama_selection_list', q.event_drama_selection_id) or st.new_row('user_event_drama_selection_list', event_drama_selection_id=q.event_drama_selection_id)
        row.selection_idx = q.selection_idx
        row.selected_idx_flag |= 1 << q.selection_idx
        tx.upsert('user_event_drama_selection_list', row)
    r = _resp(tx)
    r.post_pvt_event_select_drama.SetInParent()
    return r

def _party_set(field):

    def h(st, req, ctx):
        src = getattr(req, field)
        iq = P.Command_ApiNetwork_Api_ApiRequest()
        dst = iq.post_pvt_party_solo_set_upsert
        for f, v in src.ListFields():
            if f.name == 'user_party_armoury_list' or f.name not in dst.DESCRIPTOR.fields_by_name:
                continue
            if f.message_type is not None and getattr(f, 'is_repeated', False):
                getattr(dst, f.name).extend(v)
            elif f.message_type is not None:
                getattr(dst, f.name).CopyFrom(v)
            else:
                setattr(dst, f.name, v)
        ir = HANDLERS['/api/pvt/party/solo/set/upsert'](st, iq, ctx)
        with st.transaction(field) as tx:
            for a in src.user_party_armoury_list:
                row = P.Command_Entity_UserPartyArmoury()
                row.CopyFrom(a)
                row.user_id = st.user_id
                tx.upsert('user_party_armoury_list', row)
        r = P.Command_ApiNetwork_Api_ApiResponse()
        r.common.CopyFrom(ir.common)
        for f, rows in tx.up.ListFields():
            for row in rows:
                getattr(r.common.user.update, f.name).add().CopyFrom(row)
        getattr(r, field).SetInParent()
        return r
    return h

def _edit_guild(st, req, g):
    q = req.post_pvt_guild_edit
    base = next(iter(g.get('shared_guild_base_list', [])), None)
    if base is None:
        raise ValueError('guild/edit: no guild base row')
    base.name = q.name
    base.introduction = q.introduction
    base.disable_recommend_search = q.disable_recommend_search
    base.is_manual_approval = q.is_manual_approval
    base.guild_policy_type = q.guild_policy_type
    flags = 0
    for s in q.guild_style_type_list:
        flags |= 1 << s
    base.guild_style_types_idx_flags0 = flags
    if q.emblem_guild_item_id:
        base.emblem_guild_item_id = q.emblem_guild_item_id
    if q.home_background_guild_item_id:
        base.home_background_guild_item_id = q.home_background_guild_item_id

def memoria_analysis_enhance(st, req, ctx):
    q = req.post_pvt_memoria_analysis_enhance
    body = P.Command_ApiNetwork_Api_PostPvtMemoriaAnalysisEnhanceResponse()
    with st.transaction('memoria/analysis/enhance') as tx:
        m = st.get('user_memoria_list', q.memoria_id)
        mm = mdx('m_memoria', 0).get((q.memoria_id,))
        if m is None or mm is None:
            raise ValueError(f'memoria/analysis/enhance: memoria {q.memoria_id} not owned / unknown')
        body.before_user_memoria.CopyFrom(m)
        cap = max((r[2] for r in md('m_memoria_parameter_analysis_point') if r[0] == mm[15]), default=0)
        pts = {r[0]: r[1] for r in md('m_item_memoria_analysis_enhance_ingredient')}
        for item_id, n in q.ingredient_item_map.items():
            per = pts.get(item_id, 0)
            have = st.get('user_item_list', item_id)
            n = min(n, have.count if have else 0)
            use = 0
            while use < n and per and (m.analysis_point < cap):
                m.analysis_point = min(cap, m.analysis_point + per)
                use += 1
            if use:
                add_item(st, tx, item_id, -use)
            if n - use:
                body.unused_ingredient_item_map[item_id] = n - use
        tx.upsert('user_memoria_list', m)
        body.after_user_memoria.CopyFrom(m)
        _memoria_analysis_missions(st, tx, m, mm)
    return _resp(tx, 'post_pvt_memoria_analysis_enhance', body)

def _memoria_analysis_level(mm, pts):
    return max((r[1] for r in md('m_memoria_parameter_analysis_point') if r[0] == mm[15] and r[2] <= pts), default=0)

def _memoria_analysis_missions(st, tx, m, mm):
    lv = _memoria_analysis_level(mm, m.analysis_point)
    if lv:
        mission_event(st, tx, 'ProgressAbsoluteMemoriaAnalysisLevel', lv, m.memoria_id)
HANDLERS.update({'/api/pvt/damage/challenge/ranking/list': damage_challenge_ranking_list, '/api/pvt/event/select/drama': event_select_drama, '/api/pvt/tutorial/story/select/drama': _tut_wrap('story_select_drama', '/api/pvt/story/select/drama'), '/api/pvt/party/event/guild/ranking/set/upsert': _party_set('post_pvt_party_event_guild_ranking_set_upsert'), '/api/pvt/party/armoury/event/guild/ranking/set/upsert': _party_set('post_pvt_party_armoury_event_guild_ranking_set_upsert'), '/api/pvt/guild/edit': _guild_view('post_pvt_guild_edit', _edit_guild), '/api/pvt/memoria/analysis/enhance': memoria_analysis_enhance})
# Memoria crystals per event win: weights for 0, 1, 2, ... crystals from one battle at the boost
# memoria_crystal_odds_boost (9x). Square's odds were never in the tables; this follows how players remember a 9x win on
# the live game (sometimes 0, usually 1-3, rarely 4-6), about 2.2 a win, so 30 crystals take about 13 runs at 9x. Lower
# boosts roll in proportion (a 3x win rolls with a 3-in-9 chance), so every boost costs the same stamina per crystal.
# Once the memoria is complete, extra crystals become Memoria Medals (1,000 each, and one Large Memorial Shard of 1,000
# medals maxes a memoria's analysis), so the drop falls back to memoria_complete_crystal_chance per boost step: about
# 1.1 a 9x win. Earning the memoria is quick; levelling it stays a grind, alongside the analysis points from stamina.
MEMORIA_CRYSTAL_ODDS = [12, 25, 27, 18, 10, 5, 3]
MEMORIA_CRYSTAL_ODDS_BOOST = 9
MEMORIA_COMPLETE_CRYSTAL_CHANCE = 0.12

def _memoria_crystals(units, complete=False):
    if complete:
        p = CFG.get('memoria_complete_crystal_chance')
        p = p if isinstance(p, (int, float)) and 0 <= p <= 1 else MEMORIA_COMPLETE_CRYSTAL_CHANCE
        return sum((1 for _ in range(max(0, units)) if random.random() < p))
    odds = CFG.get('memoria_crystal_odds')
    if not (isinstance(odds, list) and odds and all((isinstance(w, (int, float)) and w >= 0 for w in odds)) and sum(odds) > 0):
        odds = MEMORIA_CRYSTAL_ODDS
    per = CFG.get('memoria_crystal_odds_boost')
    per = per if isinstance(per, int) and per > 0 else MEMORIA_CRYSTAL_ODDS_BOOST
    rolls, part = divmod(max(0, units), per)
    rolls += 1 if part and random.random() < part / per else 0
    return sum(random.choices(range(len(odds)), weights=odds, k=rolls))
_MCFG = {}

def _mcfg(name):
    if not _MCFG:
        idx = {'MemoriaAnalysisPointStaminaCoefficient': 137, 'MemoriaOverlapExchangeItemId': 136}
        row = md('m_config')[0]
        _MCFG.update({k: row[i] for k, i in idx.items()})
    return _MCFG[name]

def _memoria_ap_cap(mem):
    rows = [r for r in md('m_memoria_parameter_analysis_point') if r[0] == mem[15]]
    return max((r[2] for r in rows), default=0)

def _memoria_add_fragments(st, tx, memoria_id, n, now):
    mem = mdx('m_memoria', 0).get((memoria_id,))
    if not mem or n <= 0:
        return 0
    row = st.get('user_memoria_list', memoria_id) or st.new_row('user_memoria_list', memoria_id=memoria_id)
    need = mem[8]
    room = max(0, need - row.fragment_count)
    add = min(n, room)
    extra = n - add
    if add:
        if not row.fragment_first_get_datetime:
            row.fragment_first_get_datetime = now
        row.fragment_count += add
        if row.fragment_count >= need and (not row.equipable_datetime):
            row.equipable_datetime = now
        tx.upsert('user_memoria_list', row)
    if extra:
        per = next((r[2] for r in md('m_memoria_rarity_setting') if r[1] == mem[1]), 0)
        if per:
            add_item(st, tx, _mcfg('MemoriaOverlapExchangeItemId'), per * extra)
    return n

def _memoria_win(st, tx, party_id, stamina, ap_ok, ap_coef, frag_memoria, event_battle_id, units, now, br=None):
    # br = the reply's battle_result. Square's result screen shows memoria progress from memoria_fragment_obtain_info
    # (fragments and the stamina counter, before and after; fixed_obtain_fragment_count when the counter wraps) and
    # memoria_analysis_point_obtain_infos (the party memoria's points, before and after): 23 of 23 captured event wins
    # carry both. Neither was sent, so the screen showed no progress (#12).
    if frag_memoria and stamina > 0:
        rows = [r for r in md('m_event_memoria') if r[2] == frag_memoria]
        em = next((r for r in rows if r[1] == event_battle_id // 1000), rows[0] if rows else None)
        mem = mdx('m_memoria', 0).get((frag_memoria,))
        have = st.get('user_memoria_list', frag_memoria)
        complete = bool(mem and have and have.fragment_count >= mem[8])
        got = _memoria_crystals(units, complete)
        fixed = 0
        c = c0 = None
        if em:
            c = st.get('user_event_memoria_list', em[0]) or st.new_row('user_event_memoria_list', event_memoria_id=em[0])
            c0 = c.consumed_stamina_count
            c.consumed_stamina_count += stamina
            c.total_consumed_stamina_count += stamina
            while em[3] and c.consumed_stamina_count >= em[3]:
                c.consumed_stamina_count -= em[3]
                got += 1
                fixed += 1
            tx.upsert('user_event_memoria_list', c)
        have = st.get('user_memoria_list', frag_memoria)
        before = have.fragment_count if have else 0
        _memoria_add_fragments(st, tx, frag_memoria, got, now)
        # #63: the result screen shows "memoria obtained" whenever the info is present with after >= the memoria's
        # fragment count (MemoriaFragmentObtainResponseInfo.IsAcquiredMemoria, game 4.0.0 RVA 0xEDA940, has no
        # before-check), so a memoria already complete before this win gets no info: its crystals go to the overlap
        # exchange item as before, and the window no longer opens on every win.
        if br is not None and not complete:
            after = st.get('user_memoria_list', frag_memoria)
            fi = br.memoria_fragment_obtain_info
            fi.memoria_id = frag_memoria
            fi.before_fragment_count = before
            fi.after_fragment_count = after.fragment_count if after else before
            if c is not None:
                fi.before_consumed_stamina_count = c0
                fi.after_consumed_stamina_count = c.consumed_stamina_count
            if fixed:
                fi.fixed_obtain_fragment_count = fixed
    if ap_ok and stamina > 0:
        party = st.get('user_party_list', party_id)
        mid = party.memoria_id0 if party else 0
        mem = mdx('m_memoria', 0).get((mid,)) if mid else None
        row = st.get('user_memoria_list', mid) if mem else None
        if row and row.fragment_count >= mem[8]:
            gain = stamina * _mcfg('MemoriaAnalysisPointStaminaCoefficient') * (ap_coef or 1000) // 1000 // 1000
            new = min(_memoria_ap_cap(mem), row.analysis_point + gain)
            old = row.analysis_point
            if new != row.analysis_point:
                row.analysis_point = new
                tx.upsert('user_memoria_list', row)
            if br is not None:
                ai = br.memoria_analysis_point_obtain_infos.add()
                ai.memoria_id = mid
                ai.before_analysis_point = old
                ai.after_analysis_point = row.analysis_point

def _area_category(area_id):
    sa = mdx('m_solo_area', 0).get((area_id,))
    g = mdx('m_solo_area_group', 0).get((sa[1],)) if sa else None
    return g[1] if g else 0

import threading as _me_thr
_ME = _me_thr.local()
_me_orig_event = mission_event

def mission_event(st, tx, mtype, delta=1, param=0, reached=None, sub=None):
    t = MTYPE.get(mtype, 0) if isinstance(mtype, str) else mtype
    fired = getattr(_ME, 'fired', None)
    if fired is not None:
        fired.add(t)
    return _me_orig_event(st, tx, mtype, delta, param, reached, sub)

def _lvl_char(cid, exp):
    return max((r[1] for r in md('m_character_level') if r[0] == cid and r[2] <= exp), default=1)

def _lvl_user(exp):
    return max((r[0] for r in md('m_user_rank') if r[2] <= exp), default=1)

def _lvl_weapon(wid, exp):
    mw = mdx('m_weapon', 0).get((wid,))
    if not mw:
        return 1
    return max((r[1] for r in md('m_weapon_level') if r[0] == mw[3] and r[2] <= exp), default=1)

def _me_snap(st):
    T = st.tables
    return {'char': {k: v.exp for k, v in T['user_character_list'].items()}, 'weap': {k: (v.exp, v.rarity_type) for k, v in T['user_weapon_list'].items()}, 'wob': {k[0]: (v.weapon_upgrade_type, v.upgrade_count) for k, v in T['user_weapon_list'].items()}, 'honor': set(T['user_honor_list'].keys()), 'costume': set(T['user_character_costume_list'].keys()), 'materia': set(T['user_materia_list'].keys()), 'chocobo': set(T['user_chocobo_list'].keys()), 'item': {k: v.total_obtain_count for k, v in T['user_item_list'].items()}, 'uexp': st.one('user_status_list').exp if st.one('user_status_list') else 0, 'mcol': max((c.max_level for c in T['user_materia_collection_list'].values()), default=0), 'mcolq': {k[0]: (c.quality_one_obtain_count, c.quality_two_obtain_count, c.quality_three_obtain_count, c.quality_four_obtain_count, c.quality_five_obtain_count) for k, c in T['user_materia_collection_list'].items()}, 'tower': {k[0]: v.clear_floor_seq for k, v in T['user_tower_list'].items()}, 'adun': {k[0]: v.high_rank_type for k, v in T['user_another_dungeon_list'].items()}, 'oacc': {k[0] for k in T['user_skill_overaccel_list']}, 'memo': {k[0] for k, v in T['user_memoria_list'].items() if v.equipable_datetime}, 'mis': {k: v.progress_count for k, v in T['user_mission_list'].items()}}

def _me_done_at(mid):
    steps = _mission_steps(mid)
    return steps[-1][1] if steps else 1

def _me_battle_input(req):
    for f, v in req.ListFields():
        if f.name != 'common' and hasattr(v, 'battle_input') and hasattr(v, 'battle_result_type'):
            return v
        if f.name != 'common':
            for g, w in v.ListFields():
                if hasattr(w, 'battle_input') and hasattr(w, 'battle_result_type'):
                    return w
    return None
import bisect as _bisect
_WLV = {}

def _lvl_weapon_fast(wid, exp):
    mw = mdx('m_weapon', 0).get((wid,))
    if not mw:
        return 1
    g = _WLV.get(mw[3])
    if g is None:
        rows = sorted(((r[2], r[1]) for r in md('m_weapon_level') if r[0] == mw[3]))
        lv = []
        best = 0
        for _, l in rows:
            best = max(best, l)
            lv.append(best)
        g = _WLV[mw[3]] = ([r[0] for r in rows], lv)
    i = _bisect.bisect_right(g[0], exp)
    return g[1][i - 1] if i else 1
_CRISIS_DUNGEONS = set()
_OB_TOP = {}
_MATERIA_TYPE = {}

def _me_recount(st, tx, pre, post, fired, force=False):
    t = now_ms()
    idx = _mt_index()

    def put(m, val):
        gid = _set_group(m[1])
        if not val or not _group_open(gid, t):
            return
        if _is_guide(gid) and (not _mission_window_ok(m, t, True)):
            return
        row = st.get('user_mission_list', m[0]) or st.new_row('user_mission_list', mission_id=m[0])
        if val <= row.progress_count:
            return
        old = row.progress_count
        row.progress_count = val
        _announce_step(st, tx, m, row, old, t)
        tx.upsert('user_mission_list', row)
    if force or post['weap'] != pre.get('weap'):
        lv = {k[0]: _lvl_weapon_fast(k[0], v[0]) for k, v in post['weap'].items()}
        if 41 not in fired:
            for m in idx.get(41, []):
                put(m, sum((1 for x in lv.values() if x >= m[8])))
        if 131 not in fired:
            bychar = {}
            for wid, x in lv.items():
                mw = mdx('m_weapon', 0).get((wid,))
                bychar.setdefault(mw[2] if mw else 0, []).append(x)
            for m in idx.get(131, []):
                put(m, sum((1 for x in bychar.get(m[5], ()) if x >= m[8])))
    if (force or post['wob'] != pre.get('wob')) and 130 not in fired:
        # "Overboost N weapons to OB m[8]" (CharacterWeaponUpgradeCollect; m[5] a character or 0 = any, the steps are
        # weapon counts) was never sent (#57). Overboost is upgrade_count while the weapon is on the Rank track; a weapon
        # on the Limit track has passed the top Rank (m_weapon_upgrade_consumption: Rank tops out at 10, the highest
        # threshold, Limit follows to 20, and weapon/limit/reset puts it back at the Rank top), so it counts as that.
        if not _OB_TOP:
            for r in md('m_weapon_upgrade_consumption'):
                if r[1] == P.Command_Enums_WeaponUpgradeType_Rank:
                    _OB_TOP[r[0]] = max(_OB_TOP.get(r[0], 0), r[2])
        bychar = {}
        for wid, (ut, n) in post['wob'].items():
            mw = mdx('m_weapon', 0).get((wid,))
            ob = _OB_TOP.get(mw[15], 0) if mw and ut == P.Command_Enums_WeaponUpgradeType_Limit else n
            bychar.setdefault(mw[2] if mw else 0, []).append(ob)
        for m in idx.get(130, []):
            obs = bychar.get(m[5], ()) if m[5] else [x for v in bychar.values() for x in v]
            put(m, sum((1 for x in obs if x >= m[8])))
    if force and 21 not in fired:
        # "Raise <weapon> to Lv. N" (AbsoluteWeaponLevel) only moved on an in-game enhance, so a weapon levelled or
        # added in the Account Editor never counted (#53). Same catch-up as AbsoluteCharacterLevel below.
        wl = {k[0]: _lvl_weapon_fast(k[0], v[0]) for k, v in post['weap'].items()}
        for m in idx.get(21, []):
            put(m, wl.get(m[5], 0) if m[5] else max(wl.values(), default=0))
    if (force or post['char'] != pre.get('char')) and 42 not in fired:
        lv = [_lvl_char(k[0], exp) for k, exp in post['char'].items()]
        for m in idx.get(42, []):
            put(m, sum((1 for x in lv if x >= m[8])))
    if force and 1 not in fired:
        # "Raise X to Lv. N" (AbsoluteCharacterLevel) only moved on an in-game level-up, so a level set in the Account
        # Editor, or a character that joins already levelled (Zack, with the roster's lowest exp), never counted (#42, #43).
        bych = {k[0]: _lvl_char(k[0], exp) for k, exp in post['char'].items()}
        for m in idx.get(1, []):
            put(m, bych.get(m[5], 0) if m[5] else max(bych.values(), default=0))
    if (force or post['mcol'] != pre.get('mcol')) and 16 not in fired:
        for m in idx.get(16, []):
            put(m, post['mcol'])
    if (force or post['materia'] != pre.get('materia') or post['mcolq'] != pre.get('mcolq')) and not {14, 139} <= fired:
        # "Obtain N materia types" (MateriaCollect) counted materia items, one per new user_materia row, and "N materia
        # types obtained at quality x or higher" (MateriaQualityCollect, x = m[8]) was never sent (#59). A type is
        # m_materia_evolve col 1, the materia id, so a materia's evolve stages are one type. Obtained = owned now, or a
        # user_materia_collection_list row (kept after the materia is sold); its quality is the best of the owned
        # materia's quality_type and the collection's per-quality obtain counts.
        if not _MATERIA_TYPE:
            _MATERIA_TYPE.update({r[0]: r[1] for r in md('m_materia_evolve')})
        best = {}
        for mt in st.tables['user_materia_list'].values():
            best[mt.materia_id] = max(best.get(mt.materia_id, 0), int(mt.quality_type))
        for eid, qs in post['mcolq'].items():
            typ = _MATERIA_TYPE.get(eid)
            if typ:
                best[typ] = max(best.get(typ, 0), max((i + 1 for i, n in enumerate(qs) if n), default=0))
        if 14 not in fired:
            for m in idx.get(14, []):
                put(m, len(best))
        if 139 not in fired:
            for m in idx.get(139, []):
                put(m, sum((1 for q in best.values() if q >= m[8])))
    if (force or post['costume'] != pre.get('costume')) and 132 not in fired:
        # "<character>'s Gear Obtained" (CharacterCostumeCollect, m[5] = character) was never sent (#60): the owned
        # costumes whose m_character_costume col 1 is that character. The starting costume is a row like any other
        # (written when the character joins), so it counts: a new account shows 1 for each of its characters.
        cos = mdx('m_character_costume', 0)
        n = collections.Counter((cos[k][1] for k in post['costume'] if k in cos))
        for m in idx.get(132, []):
            put(m, n.get(m[5], 0))
    if (force or post['costume'] != pre.get('costume')) and 78 not in fired:
        # "Obtain N type(s) of gear" (ProgressCostumeGet): the costumes owned
        for m in idx.get(78, []):
            put(m, len(post['costume']))
    if (force or post['memo'] != pre.get('memo')) and 191 not in fired:
        # "Obtain N memoria type(s)" (ProgressMemoriaCollect): memoria whose fragments are complete (equipable)
        for m in idx.get(191, []):
            put(m, len(post['memo']))
    if (force or post['tower'] != pre.get('tower')) and 104 not in fired:
        for m in idx.get(104, []):
            put(m, post['tower'].get(m[5], 0))
    if (force or post['adun'] != pre.get('adun')) and 142 not in fired:
        if not _CRISIS_DUNGEONS:
            _CRISIS_DUNGEONS.update((r[0] for r in md('m_another_dungeon') if r[6] == 1))
        for m in idx.get(142, []):
            put(m, sum((1 for k, rk in post['adun'].items() if k in _CRISIS_DUNGEONS and rk >= m[8])))
    if force and 127 not in fired:
        # Highwind wins never moved "Highwind Boss Battles Cleared" (#58): catch up to the recorded wins of each battle
        # type (user_highwind_battle_list.win_count by m_highwind_battle col 4).
        hw = {}
        for r in st.tables['user_highwind_battle_list'].values():
            hb = mdx('m_highwind_battle', 0).get((r.highwind_battle_id,))
            if hb:
                hw[hb[4]] = hw.get(hb[4], 0) + r.win_count
        for m in idx.get(127, []):
            put(m, hw.get(m[5], 0))
    if 186 not in fired:
        for sk in post['oacc'] if force else post['oacc'] - pre.get('oacc', set()):
            _me_orig_event(st, tx, 186, 1, sk)

def _title_recount(st):
    with st.transaction('title: counted missions') as tx:
        _me_recount(st, tx, {}, _me_snap(st), set(), force=True)
    return tx.n_up

def _me_post(path, st, req, resp, pre, fired):
    post = _me_snap(st)

    def ev(t, d=1, prm=0):
        if t in fired or not d:
            return
        _me_orig_event(st, tx, t, d, prm)
    with st.transaction('missions') as tx:
        dl = 0
        for k, exp in post['char'].items():
            if pre['char'].get(k) != exp:
                a, b = (_lvl_char(k[0], pre['char'].get(k, 0)), _lvl_char(k[0], exp))
                ev(1, b, k[0])
                dl += max(0, b - a)
        ev(56, dl)
        if post['uexp'] != pre['uexp']:
            ev(31, _lvl_user(post['uexp']))
        dw = 0
        new_w = []
        for k, (exp, rar) in post['weap'].items():
            old = pre['weap'].get(k)
            if old is None:
                new_w.append((k[0], rar))
            if old is None or old[0] != exp:
                a, b = (_lvl_weapon(k[0], old[0]) if old else 0, _lvl_weapon(k[0], exp))
                ev(21, b, k[0])
                dw += max(0, b - a)
        ev(57, dw)
        ev(30, len(new_w))
        if new_w and 145 not in fired:
            for m in _mt_index().get(145, []):
                n = 0
                for wid, rar in new_w:
                    mw = mdx('m_weapon', 0).get((wid,))
                    if (not m[5] or (mw and mw[2] == m[5])) and rar >= (m[8] or 0):
                        n += 1
                if n:
                    row = st.get('user_mission_list', m[0])
                    if not _group_open(_set_group(m[1]), now_ms()):
                        continue
                    row = row or st.new_row('user_mission_list', mission_id=m[0])
                    row.progress_count += n
                    tx.upsert('user_mission_list', row)
        new_h = post['honor'] - pre['honor']
        ev(76, len(new_h))
        for hk in new_h:
            h = mdx('m_honor', 0).get(hk)
            if h:
                ev(129, 1, h[1])
        if path == '/api/pvt/shop/exchange':
            ev(70, len(post['chocobo'] - pre['chocobo']))
        for k, tot in post['item'].items():
            d = tot - pre['item'].get(k, 0)
            if d > 0:
                it = mdx('m_item', 0).get(k)
                if it:
                    ev(103, d, it[1])
        if path.endswith('/party/solo/set/upsert') or path.endswith('/party/multi/set/upsert') or path.endswith('/battle/start'):
            best = max((p.combat_power for p in st.tables['user_party_list'].values()), default=0)
            for f, v in req.ListFields():
                bsi = getattr(v, 'battle_start_input', None) if f.name != 'common' else None
                if bsi is not None and len(bsi.party_member_info_list):
                    best = max(best, sum((x.combat_power for x in bsi.party_member_info_list)))
            if best:
                ev(45, best)
                prt = 13 if 'multi' in path else 4
                pr = st.get('user_play_record_list', prt) or st.new_row('user_play_record_list', play_record_type=prt)
                if best > pr.int_value:
                    pr.int_value = best
                    tx.upsert('user_play_record_list', pr)
        b = _me_battle_input(req) if path.endswith('/battle/end') else None
        if b is not None and b.battle_result_type == WIN:
            bi = b.battle_input
            is_event = '/event/' in path
            for cr in bi.character_results:
                if not is_event:
                    ev(148, cr.total_damage, cr.character_id)
                    ev(147, cr.max_blow_damage, cr.character_id)
                ev(89, cr.special_skill_count, cr.character_id)
            ev(87, bi.phase_break_count)
            e = st.one('user_battle_entry_list')
            if e is not None:
                party = st.get('user_party_list', e.party_id)
                if party is not None and party.memoria_id0:
                    ev(197, 1)
                if e.stamina_boost_type:
                    ev(94, 1)
        if path.endswith('/weapon/upgrade'):
            ev(51, 1)
        _me_recount(st, tx, pre, post, fired)
        if 5 not in fired:
            seen = dict(pre['mis'])
            for _ in range(3):
                done = collections.Counter()
                for k, row in st.tables['user_mission_list'].items():
                    b, a = (seen.get(k, 0), row.progress_count)
                    if a <= b:
                        continue
                    seen[k] = a
                    m = _mt_index()['by_id'].get(k[0])
                    if m and b < _me_done_at(k[0]) <= a:
                        done[m[1]] += 1
                if not done:
                    break
                for sid, c in done.items():
                    _me_orig_event(st, tx, 5, c, sid)
    for mid, step in (getattr(tx, 'achieved', None) or {}).items():
        a = resp.common.user.other_info.mission_achieved_list.add()
        a.mission_id = mid
        a.progress_count = step
    if tx.n_up:
        for f, rows in tx.up.ListFields():
            lst = getattr(resp.common.user.update, f.name)
            for row in rows:
                k = _ES.row_key(f.name, row)
                for i, x in enumerate(lst):
                    if _ES.row_key(f.name, x) == k:
                        lst[i].CopyFrom(row)
                        break
                else:
                    lst.add().CopyFrom(row)

def _me_wrap(fn, path):
    if getattr(fn, '_me_wrapped', False):
        return fn

    def h(st, req, ctx):
        if getattr(_ME, 'fired', None) is not None:
            return fn(st, req, ctx)
        pre = _me_snap(st)
        _ME.fired = set()
        try:
            resp = fn(st, req, ctx)
        finally:
            fired = _ME.fired
            _ME.fired = None
        try:
            _me_post(path, st, req, resp, pre, fired)
        except Exception as e:
            print(f'mission engine: {path}: {e!r}')
        return resp
    h._me_wrapped = True
    for a in ('unlocked',):
        if hasattr(fn, a):
            setattr(h, a, getattr(fn, a))
    return h

def _me_wrap_all():
    for p in list(HANDLERS):
        if p in ('/api/chat/guild/check',) or getattr(HANDLERS[p], 'unlocked', False):
            continue
        HANDLERS[p] = _me_wrap(HANDLERS[p], p)
_me_wrap_all()
_RESP_FIELDS = {f.name for f in P.Command_ApiNetwork_Api_ApiResponse.DESCRIPTOR.fields if f.message_type is not None}

def _body_wrap(fn):

    def h(st, req, ctx):
        r = fn(st, req, ctx)
        try:
            for fd, _ in req.ListFields():
                if fd.name != 'common' and fd.name in _RESP_FIELDS and (not r.HasField(fd.name)):
                    getattr(r, fd.name).SetInParent()
        except Exception as e:
            print('reply-section fill skipped:', repr(e))
        return r
    for a in ('unlocked',):
        if hasattr(fn, a):
            setattr(h, a, getattr(fn, a))
    return h
for _p in list(HANDLERS):
    HANDLERS[_p] = _body_wrap(HANDLERS[_p])
_WIN = P.Command_Enums_BattleResultType.Value('Command_Enums_BattleResultType_Win')
_BT_AREA = P.Command_Enums_BattleType.Value('Command_Enums_BattleType_AreaSolo')
_BT_EVENT = P.Command_Enums_BattleType.Value('Command_Enums_BattleType_EventSolo')

def _pr_counts(path, req):
    if path == '/api/pvt/area/solo/battle/end':
        q = req.post_pvt_area_solo_battle_end
        if q.battle_result_type != _WIN:
            return {}
        a = _area_battle(q.area_battle_id)
        return {'SoloBattleClearCount': 1, **({'ConsumptionStaminaBattleClearCount': 1} if a and a[7] else {})}
    if path == '/api/pvt/event/solo/battle/end':
        q = req.post_pvt_event_solo_battle_end
        if q.battle_result_type != _WIN:
            return {}
        o = _stage(_stage_copies().get(q.event_battle_id, q.event_battle_id))
        return {'SoloBattleClearCount': 1, **({'ConsumptionStaminaBattleClearCount': 1} if o and o[8] else {})}
    if path == '/api/pvt/battle/skip':
        bt = req.post_pvt_battle_skip.battle_type
        if bt == _BT_AREA:
            return {'SoloBattleClearCount': 2, 'ConsumptionStaminaBattleClearCount': 2}
        if bt == _BT_EVENT:
            return {'SoloBattleClearCount': 2}
    return {}

def _pr_wrap(fn, path):

    def h(st, req, ctx):
        r = fn(st, req, ctx)
        try:
            add = _pr_counts(path, req)
            if add:
                with st.transaction('play records') as tx:
                    for k, n in add.items():
                        _pr_add(st, tx, k, n)
                up = r.common.user.update.user_play_record_list
                keep = [x for x in up if x.play_record_type not in {row.play_record_type for row in tx.up.user_play_record_list}]
                del up[:]
                up.extend(keep)
                up.extend(tx.up.user_play_record_list)
        except Exception as e:
            print(f'play records skipped on {path}: {e!r}')
        return r
    if hasattr(fn, 'unlocked'):
        h.unlocked = fn.unlocked
    return h
for _p in ('/api/pvt/area/solo/battle/end', '/api/pvt/event/solo/battle/end', '/api/pvt/battle/skip'):
    if _p in HANDLERS:
        HANDLERS[_p] = _pr_wrap(HANDLERS[_p], _p)

_DR_STAGE = _DR_EVENT = _DR_COEF = None

def _dr_stage(esb_id):
    global _DR_STAGE
    if _DR_STAGE is None:
        _DR_STAGE = {r[0]: r for r in md('m_event_damage_ranking_battle_solo_battle')}
    return _DR_STAGE.get(esb_id)

def _dr_event(ecb_id):
    global _DR_EVENT
    if _DR_EVENT is None:
        _DR_EVENT = {r[0]: r for r in md('m_event_damage_ranking_battle')}
    return _DR_EVENT.get(ecb_id)

def _dr_coef(cid):
    global _DR_COEF
    if _DR_COEF is None:
        _DR_COEF = {r[0]: r[1] for r in md('m_event_damage_ranking_battle_difficulty_coefficient')}
    return _DR_COEF.get(cid, 1000)

def _cb_row(ecb_id):
    return next((r for r in md('m_event_crisis_battle') if r[0] == ecb_id), None)

def event_damage_ranking_battle_current_rank(st, req, ctx):
    ecb_id = req.post_pvt_event_damage_ranking_battle_current_rank.event_crisis_battle_id
    rows = _crisis_ranked(_crisis_load(st), ecb_id)
    r = _crisis_ack(st, 'post_pvt_event_damage_ranking_battle_current_rank')
    r.post_pvt_event_damage_ranking_battle_current_rank.rank = next((i + 1 for i, (u, e) in enumerate(rows) if u == str(st.user_id)), 0)
    return r

def event_damage_ranking_battle_group_ranking(st, req, ctx):
    q = req.post_pvt_event_damage_ranking_battle_group_ranking
    rows = _crisis_ranked(_crisis_load(st), q.event_crisis_battle_id)
    r = _crisis_ack(st, 'post_pvt_event_damage_ranking_battle_group_ranking')
    body = r.post_pvt_event_damage_ranking_battle_group_ranking
    start = q.start_rank or 1
    for i, (u, e) in enumerate(rows):
        if i + 1 < start:
            continue
        _rank_user_info(body.ranking_user_infos, e, i + 1)
        if len(body.ranking_user_infos) >= 50:
            break
    body.max_rank = len(rows)
    return r

def event_damage_ranking_battle_friend_ranking(st, req, ctx):
    ecb_id = req.post_pvt_event_damage_ranking_battle_friend_ranking.event_crisis_battle_id
    rows = _crisis_ranked(_crisis_load(st), ecb_id)
    r = _crisis_ack(st, 'post_pvt_event_damage_ranking_battle_friend_ranking')
    body = r.post_pvt_event_damage_ranking_battle_friend_ranking
    for i, (u, e) in enumerate(_friend_rows(st, rows)[:50]):
        _rank_user_info(body.ranking_user_infos, e, i + 1)
    return r

def event_damage_ranking_battle_ranking_reward_receive(st, req, ctx):
    ecb_id = req.post_pvt_event_damage_ranking_battle_ranking_reward_receive.event_crisis_battle_id
    rows = _crisis_ranked(_crisis_load(st), ecb_id)
    rank = next((i + 1 for i, (u, e) in enumerate(rows) if u == str(st.user_id)), 0)
    with st.transaction('event/damage/ranking/battle/ranking/reward/receive') as tx:
        r = _resp(tx, 'post_pvt_event_damage_ranking_battle_ranking_reward_receive')
        body = r.post_pvt_event_damage_ranking_battle_ranking_reward_receive
        body.rank = rank
        if rank > 0:
            ecb = st.get('user_event_crisis_battle_list', ecb_id) or st.new_row('user_event_crisis_battle_list', event_crisis_battle_id=ecb_id)
            if not ecb.is_received_ranking_reward:
                cb = _cb_row(ecb_id)
                rg = cb[2] if cb else ecb_id
                tiers = sorted((row for row in md('m_event_crisis_battle_ranking') if row[0] == rg), key=lambda x: -x[1])
                rs = next((row[2] for row in tiers if rank >= row[1]), None)
                if rs:
                    grant_reward_set(st, tx, rs, 1, body.reward_info_list)
                ecb.is_received_ranking_reward = True
                ecb.final_rank = rank
                tx.upsert('user_event_crisis_battle_list', ecb)
    return r
HANDLERS.update({'/api/pvt/event/damage/ranking/battle/current/rank': event_damage_ranking_battle_current_rank, '/api/pvt/event/damage/ranking/battle/group/ranking': event_damage_ranking_battle_group_ranking, '/api/pvt/event/damage/ranking/battle/friend/ranking': event_damage_ranking_battle_friend_ranking, '/api/pvt/event/damage/ranking/battle/ranking/reward/receive': event_damage_ranking_battle_ranking_reward_receive})
_user_title_orig = HANDLERS['/api/pvt/user/title']

def _login_bonus_not_taken(st, t):
    ut = st.get('user_time_list', P.Command_Enums_UserTimeType.Value('Command_Enums_UserTimeType_LoginBonus'))
    return not (ut and ut.datetime and game_day(ut.datetime) == game_day(t))

def user_title_no_popup(st, req, ctx):
    r = _user_title_orig(st, req, ctx)
    r.post_pvt_user_title.is_daily_first_login = False
    # has_login_bonus is what makes the game call login/bonus, and login/bonus is what moves the daily "Log in" mission
    # (ProgressLogin). It was never set, so the game never called it: no login bonus and the daily mission stayed at 0
    # (#44). The weekly login missions move at the title screen, which is why those worked.
    r.post_pvt_user_title.has_login_bonus = _login_bonus_not_taken(st, (ctx or {}).get('now_ms') or now_ms())
    return r
HANDLERS['/api/pvt/user/title'] = user_title_no_popup
import base64 as _gb64
import hashlib as _ghl

def _guild_display_id(gid):
    return _gb64.b32encode(_ghl.sha1(f'guild:{gid}'.encode()).digest()).decode('ascii')[:13]

def _guild_level_of(exp):
    return max((r[0] for r in md('m_guild_level') if r[1] <= exp), default=1)

def _guild_facts(st, gid, g):
    base = next(iter(g.get('shared_guild_base_list', [])), None)
    ex = next(iter(g.get('shared_guild_exp_list', [])), None)
    members = _guild_members_here(gid)
    own = st.one('shared_guild_member_list')
    if own is not None and own.guild_id == gid:
        members[str(st.user_id)] = own
    master = next((u for u, m in members.items() if m.guild_role_type == P.Command_Enums_GuildRoleType_Master), None)
    mname = ''
    if master:
        c = _card_here(master)
        mname = c.player_name if c is not None else ''
    return {'base': base, 'exp': ex.level_exp if ex is not None else 0, 'members': members, 'master_name': mname, 'last_active': max((m.last_active_datetime for m in members.values()), default=0)}

def _guild_record(st, ctx):
    cap = _captured(ctx)
    gid = _my_guild(st, cap)
    if not gid:
        return (0, None)
    with _GUILD_LOCK:
        g = _guild_get(st, gid, cap)
    return (gid, g)
_guild_top_view = HANDLERS['/api/pvt/guild/top']

def guild_top_filled(st, req, ctx):
    r = _guild_top_view(st, req, ctx)
    b = r.post_pvt_guild_top
    if b.display_guild_id:
        return r
    gid, g = _guild_record(st, ctx)
    if not g:
        return r
    f = _guild_facts(st, gid, g)
    b.member_count = len(f['members'])
    b.guild_master_name = f['master_name']
    b.display_guild_id = _guild_display_id(gid)
    b.guild_shop_exchange_bonus_exp_result.SetInParent()
    return r
_guild_self_view = HANDLERS['/api/pvt/guild/get/self']

def guild_get_self_filled(st, req, ctx):
    r = _guild_self_view(st, req, ctx)
    b = r.post_pvt_guild_get_self
    if b.guild_info.display_guild_id:
        return r
    gid, g = _guild_record(st, ctx)
    if not g:
        return r
    f = _guild_facts(st, gid, g)
    base = f['base']
    n = len(f['members'])
    gi, di = (b.guild_info, b.guild_detail_info)
    gi.display_guild_id = _guild_display_id(gid)
    gi.guild_level = _guild_level_of(f['exp'])
    gi.member_count = n
    gi.is_self_guild = True
    di.member_count = n
    di.last_active_datetime = f['last_active']
    di.guild_exp = f['exp']
    di.guild_master_name = f['master_name']
    if base is not None:
        if base.name:
            gi.guild_name = base.name
        gi.introduction = base.introduction
        gi.emblem_guild_item_id = base.emblem_guild_item_id
        gi.is_manual_approval = base.is_manual_approval
        gi.guild_policy_type = base.guild_policy_type
        del gi.guild_style_type_list[:]
        for i in range(1, 11):
            if base.guild_style_types_idx_flags0 & 1 << i:
                gi.guild_style_type_list.append(i)
        di.home_background_guild_item_id = base.home_background_guild_item_id
        di.guild_created_datetime = base.created_datetime
    for a in g.get('shared_guild_achievement_list', []):
        x = di.guild_achievement_list.add()
        x.guild_achievement_id = a.guild_achievement_id
        x.progress_count = a.progress_count
    btype = {row[0]: row[1] for row in md('m_guild_bonus')}
    for bo in g.get('shared_guild_bonus_list', []):
        x = di.guild_bonus_list.add()
        x.guild_bonus_type = btype.get(bo.guild_bonus_id, bo.guild_bonus_id)
        x.level = bo.level
    return r
_guild_members_view = HANDLERS['/api/pvt/guild/member/list']

def guild_member_list_filled(st, req, ctx):
    r = _guild_members_view(st, req, ctx)
    b = r.post_pvt_guild_member_list
    if len(b.guild_member_list_infos):
        return r
    own = st.one('shared_guild_member_list')
    if own is None:
        return r
    card = _card_here(str(st.user_id))
    if card is None:
        card = P.Command_Entity_UserListInfo()
        from engine.vault import read_summary
        sm = read_summary(st.folder) or {} if st.folder else {}
        card.player_name = sm.get('name') or ''
    e = b.guild_member_list_infos.add()
    e.user_list_info.CopyFrom(card)
    e.guild_role_type = own.guild_role_type
    return r
HANDLERS.update({'/api/pvt/guild/top': guild_top_filled, '/api/pvt/guild/get/self': guild_get_self_filled, '/api/pvt/guild/member/list': guild_member_list_filled})

# Custom notices: gameplay-settings.json "notices", a list of {"title", "body" (text, or a list of lines), "type"
# (an AnnouncementType name such as Info, Update, ImportantInfo; default Info), "date" ("YYYY-MM-DD" or
# "YYYY-MM-DD HH:MM", local time), "id" (default its place in the list)}. A notice has no end date: it shows for as long
# as it is listed. The game draws a notice's page from JSON blocks (Command.OutGame.Announcement
# ResponseAnnouncementDetailData); each notice here is one body block.
_NOTICE_BODY, _NOTICE_TOP_LEFT = 2, 257          # AnnouncementBlockType.Body, TextAlignmentOptions.TopLeft

def _notices():
    out = []
    kinds = {k.split('_')[-1].lower(): v for k, v in P.Command_Enums_AnnouncementType.items()}
    for n, raw in enumerate(CFG.get('notices') or (), 1):
        if not isinstance(raw, dict) or not raw.get('title'):
            continue
        kind = str(raw.get('type') or 'Info').replace(' ', '')
        when = 0
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
            try:
                when = int(time.mktime(time.strptime(str(raw.get('date', '')), fmt)) * 1000)
                break
            except ValueError:
                pass
        if not when:
            when = int(os.path.getmtime(CFG_PATH) * 1000) if os.path.isfile(CFG_PATH) else now_ms()
        body = raw.get('body', '')
        out.append({'id': int(raw.get('id') or n), 'type': kinds.get(kind.lower(), kinds['info']),
                    'title': str(raw['title']), 'when': when,
                    'body': '\n'.join(map(str, body)) if isinstance(body, list) else str(body)})
    return sorted(out, key=lambda x: -x['when'])

def _notice_summary(summary, n):
    summary.announcement_id, summary.announcement_type, summary.title = n['id'], n['type'], n['title']
    summary.start_datetime = n['when']

def announcement_list(st, req, ctx):
    q = req.post_announcement_list
    with st.transaction('announcement/list') as tx:
        pass
    r = _resp(tx)
    r.post_announcement_list.SetInParent()
    shown = [n for n in _notices() if not q.announcement_type_list or n['type'] in q.announcement_type_list]
    r.post_announcement_list.total = len(shown)
    for n in shown[q.offset:q.offset + q.limit if q.limit else None]:
        _notice_summary(r.post_announcement_list.announcement_summary_list.add(), n)
    return r
HANDLERS['/api/announcement/list'] = announcement_list
import importlib as _il
from engine import eventkit as _eex
_il.reload(_eex)
_eex.install(globals())
_BD_KEY = {'user_event_crisis_battle_solo_battle_list': 'selected_event_crisis_battle_buff_debuff_id_list', 'user_event_damage_ranking_battle_solo_battle_list': 'selected_event_damage_ranking_battle_buff_debuff_id_list'}

def _bd_json(table, ids):
    return json.dumps({_BD_KEY[table]: [int(x) for x in ids]}, separators=(',', ':'))

def _bd_select(st, table, esb_id, ids, label, field):
    with st.transaction(label) as tx:
        sb = st.get(table, esb_id) or st.new_row(table, event_solo_battle_id=esb_id)
        sb.buff_debuff_selection_info = _bd_json(table, ids)
        tx.upsert(table, sb)
    return _resp(tx, field)

def event_crisis_battle_buff_debuff_select(st, req, ctx):
    q = req.post_pvt_event_crisis_battle_buff_debuff_select
    return _bd_select(st, 'user_event_crisis_battle_solo_battle_list', q.event_solo_battle_id, q.event_crisis_battle_buff_debuff_ids, 'event/crisis/battle/buff/debuff/select', 'post_pvt_event_crisis_battle_buff_debuff_select')

def event_damage_ranking_battle_buff_debuff_select(st, req, ctx):
    q = req.post_pvt_event_damage_ranking_battle_buff_debuff_select
    return _bd_select(st, 'user_event_damage_ranking_battle_solo_battle_list', q.event_solo_battle_id, q.event_crisis_battle_buff_debuff_ids, 'event/damage/ranking/battle/buff/debuff/select', 'post_pvt_event_damage_ranking_battle_buff_debuff_select')

def _bd_heal(st):
    n = 0
    for table in _BD_KEY:
        try:
            rows = list(st[table].values())
        except Exception:
            continue
        bad = [r for r in rows if (r.buff_debuff_selection_info or '').lstrip().startswith('[')]
        if not bad:
            continue
        with st.transaction('battle-effect selection heal') as tx:
            for r in bad:
                try:
                    ids = json.loads(r.buff_debuff_selection_info)
                except Exception:
                    ids = []
                r.buff_debuff_selection_info = _bd_json(table, ids if isinstance(ids, list) else [])
                tx.upsert(table, r)
                n += 1
    return n
_bd_title_orig = HANDLERS['/api/pvt/user/title']

def _bd_title(st, req, ctx):
    try:
        n = _bd_heal(st)
        if n:
            print(f'battle-effect selection heal: {st.user_id} {n} row(s) rewritten')
    except Exception as e:
        print(f'battle-effect selection heal skipped: {e!r}')
    return _bd_title_orig(st, req, ctx)
for _a in ('unlocked',):
    if hasattr(_bd_title_orig, _a):
        setattr(_bd_title, _a, getattr(_bd_title_orig, _a))
HANDLERS['/api/pvt/user/title'] = _bd_title
HANDLERS['/api/pvt/event/crisis/battle/buff/debuff/select'] = event_crisis_battle_buff_debuff_select
HANDLERS['/api/pvt/event/damage/ranking/battle/buff/debuff/select'] = event_damage_ranking_battle_buff_debuff_select
for _p in [p for p in HANDLERS if p.startswith('/api/pvt/event/damage/ranking/battle/')] + ['/api/pvt/event/crisis/battle/buff/debuff/select']:
    if not getattr(HANDLERS[_p], '_late_wrapped', False):
        HANDLERS[_p] = _body_wrap(_me_wrap(HANDLERS[_p], _p))
        HANDLERS[_p]._late_wrapped = True
_CR_ALL = {'t': 0.0, 'rows': {}}

def _crisis_all_totals():
    if time.time() - _CR_ALL['t'] < 20:
        return _CR_ALL['rows']
    out = {}
    for db in _glob.glob(os.path.join(_ACC, '*', 'save.db')):
        folder = os.path.dirname(db)
        uid = None
        rows = []
        live = getattr(State, 'LIVE', {}).get(os.path.realpath(folder))
        if live is not None:
            uid = str(live.user_id)
            try:
                rows = list(live.tables.get('user_event_crisis_battle_list', {}).values())
            except RuntimeError:
                rows = []
        else:
            try:
                c = _sq3.connect('file:' + db + '?mode=ro', uri=True)
                try:
                    r = c.execute("SELECT val FROM meta WHERE key='user_id'").fetchone()
                    uid = str(r[0]) if r else None
                    for pb, in c.execute("SELECT pb FROM rows WHERE tbl='user_event_crisis_battle_list'"):
                        m = P.Command_Entity_UserEventCrisisBattle()
                        m.ParseFromString(pb)
                        rows.append(m)
                finally:
                    c.close()
            except Exception as e:
                print(f'battle ranking: {db}: {e!r}')
                continue
        if not uid:
            continue
        for m in rows:
            if m.total_point > 0 or m.max_high_score > 0:
                out.setdefault(m.event_crisis_battle_id, {})[uid] = (m.total_point, m.max_high_score)
    _CR_ALL['t'] = time.time()
    _CR_ALL['rows'] = out
    return out

def _crisis_ranked(board, ecb_id):
    """The Battle Ranking score (#75) is each stage's best run once - the ranking detail's total_score, the sum of the stage
    high scores it lists - not the total points every win adds to. An account with no runs here (an import) keeps its
    saved total. Damage Ranking ranks by the best single run."""
    k = str(ecb_id)
    by_hs = _dr_event(int(ecb_id)) is not None
    vals = dict(_crisis_all_totals().get(int(ecb_id), {}))
    for u, e in board.items():
        b = e.get(k) or {}
        t0, h0 = vals.get(u, (0, 0))
        vals[u] = (max(t0, b.get('tot', 0)), max(h0, b.get('hs', 0)))
    rows = []
    for u, (tot, hs) in vals.items():
        be = board.get(u, {}).get(k, {})
        v = hs if by_hs else sum(be.get('stages', {}).values()) or tot
        if v <= 0:
            continue
        c = _card_here(u)
        name = (c.player_name if c is not None else '') or be.get('name', '')
        hon = [c.top_honor_id, c.bottom_honor_id, c.background_honor_id, c.icon_honor_id] if c is not None else be.get('hon') or [0, 0, 0, 0]
        rows.append((u, {'pt': v, 'tot': tot, 'hs': hs, 'name': name, 'hon': hon, 'duid': _syn_duid(u), 'stages': be.get('stages', {}), 'sd': be.get('sd', {})}))
    rows.sort(key=lambda x: -x[1]['pt'])
    return rows

_CBD = _CBD_GRP = _CBD_FIX = None

def _crisis_stage_level(st, esb_id):
    """Stage level and score bonus of a crisis stage run (#66). The client's model (il2cpp field names): the stage's
    DefaultStageLevel (m_event_crisis_battle_solo_battle col 3) plus StageLevelIncreaseValue (m_event_crisis_battle_buff_debuff
    col 3) of every chosen effect (selectable group, col 1) and fixed effect (col 2); the score bonus is the same effects'
    ScoreCoefficientPermil (col 2) - the Battle Effects window's Current Stage Level and Total Bonus. Counting the fixed
    effects is inferred."""
    global _CBD, _CBD_GRP, _CBD_FIX
    if _CBD is None:
        _CBD = {r[0]: r for r in md('m_event_crisis_battle_buff_debuff')}
        _CBD_GRP = {}
        for r in md('m_event_crisis_battle_buff_debuff_group_rel'):
            _CBD_GRP.setdefault(r[0], set()).add(r[1])
        _CBD_FIX = {}
        for r in md('m_event_crisis_battle_fixed_buff_debuff_group_rel'):
            _CBD_FIX.setdefault(r[0], []).append(r[1])
    row = _crisis_stage(esb_id)
    if not row:
        return (0, 0)
    sb = st.get('user_event_crisis_battle_solo_battle_list', esb_id)
    sel = []
    if sb is not None and sb.buff_debuff_selection_info:
        try:
            sel = json.loads(sb.buff_debuff_selection_info).get(_BD_KEY['user_event_crisis_battle_solo_battle_list'], []) or []
        except Exception:
            sel = []
    ok = _CBD_GRP.get(row[1], set())
    ids = [int(x) for x in sel if int(x) in ok] + list(_CBD_FIX.get(row[2], []))
    return (row[3] + sum(_CBD[i][3] for i in ids if i in _CBD), sum(_CBD[i][2] for i in ids if i in _CBD))

def _crisis_score(st, tx, q, body, dmg, chars, win, now):
    body.event_crisis_battle_result.SetInParent()
    dr = _dr_stage(q.event_battle_id)
    cs = None if dr else _crisis_stage(q.event_battle_id)
    if not (win and dmg > 0 and (dr or cs)):
        return
    ecb_id = q.event_battle_id // 1000
    lvl, bonus = (0, 0) if dr else _crisis_stage_level(st, q.event_battle_id)
    score = dmg * _dr_coef(dr[2]) // 1000 if dr else dmg * (1000 + bonus) // 1000
    board = _crisis_load(st)
    ent = board.setdefault(str(st.user_id), {}).setdefault(str(ecb_id), {'stages': {}})
    ent.pop('sq', None)
    k = str(q.event_battle_id)
    stage_best = score > ent['stages'].get(k, 0)
    if stage_best:
        ent['stages'][k] = score
        cids = [c.character_id for c in chars[:3]] + [0, 0, 0]
        ent.setdefault('sd', {})[k] = [score, cids[0], cids[1], cids[2], int(now)]
    ecb = st.get('user_event_crisis_battle_list', ecb_id) or st.new_row('user_event_crisis_battle_list', event_crisis_battle_id=ecb_id)
    before, before_hs = (ecb.total_point, ecb.max_high_score)
    ecb.total_point = before + score
    ecb.max_high_score = max(before_hs, score)
    before_hsl = ecb.high_stage_level
    ecb.high_stage_level = max(ecb.high_stage_level, len(ent['stages']) if dr else lvl)
    tx.upsert('user_event_crisis_battle_list', ecb)
    tbl = 'user_event_damage_ranking_battle_solo_battle_list' if dr else 'user_event_crisis_battle_solo_battle_list'
    sb = st.get(tbl, q.event_battle_id) or st.new_row(tbl, event_solo_battle_id=q.event_battle_id)
    if stage_best:
        for i, c in enumerate(chars[:3]):
            setattr(sb, f'high_score_character_id{i}', c.character_id)
        sb.last_update_high_score_datetime = now
    tx.upsert(tbl, sb)
    name, hon = _crisis_profile(st)
    ent.update({'pt': ecb.max_high_score if dr else sum(ent['stages'].values()), 'tot': ecb.total_point, 'hs': ecb.max_high_score, 'name': name, 'hon': hon, 'duid': _syn_duid(st.user_id)})
    _crisis_save(st, board)
    _CR_ALL['t'] = 0.0
    cr = body.event_crisis_battle_result
    cr.before_total_point = before
    cr.after_total_point = ecb.total_point
    cr.clear_stage_level = len(ent['stages']) if dr else lvl
    if not dr:
        cb = _cb_row(ecb_id)
        slg = cb[4] if cb else ecb_id
        for row in md('m_event_crisis_battle_stage_level_reward'):
            if row[0] == slg and before_hsl < row[1] <= ecb.high_stage_level:
                grant_reward_set(st, tx, row[2], 1, cr.stage_level_reward_infos)
    if dr:
        cb = _cb_row(ecb_id)
        tpg = cb[3] if cb else ecb_id
    else:
        tpg = ecb_id
    for row in md('m_event_crisis_battle_total_point_reward'):
        if row[0] == tpg and before < row[1] <= ecb.total_point:
            grant_reward_set(st, tx, row[2], 1, cr.total_point_reward_infos)
    ev = _dr_event(ecb_id) if dr else None
    if ev:
        for row in md('m_event_damage_ranking_battle_high_score_reward'):
            if row[0] == ev[5] and before_hs < row[1] <= ecb.max_high_score:
                grant_reward_set(st, tx, row[2], 1, cr.high_score_reward_infos)

def _crisis_detail(q, rows, body, field, st=None):
    """Each stage carries its best run's party and time (#67: no party, Dec 31 1969): from the board, else - the player's
    own entry, wins before this change - from their solo-battle rows, which always kept them."""
    hit = next(((u, e) for u, e in rows if e['duid'] == q.display_user_id), None)
    if not hit:
        return
    u, tgt = hit
    setattr(body, field, tgt['hs'] if field == 'max_high_score' else tgt.get('pt', tgt['tot']))
    sd = tgt.get('sd') or {}
    own = st is not None and str(st.user_id) == str(u)
    tbl = 'user_event_damage_ranking_battle_solo_battle_list' if field == 'max_high_score' else 'user_event_crisis_battle_solo_battle_list'
    for sid, hs in sorted(tgt.get('stages', {}).items(), key=lambda x: -x[1]):
        d = body.battle_details.add()
        d.event_solo_battle_id = int(sid)
        d.high_score = hs
        d.battle_rank_type = RANK_S
        x = sd.get(sid)
        if x:
            d.character_id0, d.character_id1, d.character_id2, d.last_update_high_score_datetime = (x[1], x[2], x[3], x[4])
        elif own:
            sb = st.get(tbl, int(sid))
            if sb is not None:
                d.character_id0, d.character_id1, d.character_id2 = (sb.high_score_character_id0, sb.high_score_character_id1, sb.high_score_character_id2)
                d.last_update_high_score_datetime = sb.last_update_high_score_datetime

def event_crisis_battle_ranking_detail(st, req, ctx):
    q = req.post_pvt_event_crisis_battle_ranking_detail
    r = _crisis_ack(st, 'post_pvt_event_crisis_battle_ranking_detail')
    _crisis_detail(q, _crisis_ranked(_crisis_load(st), q.event_crisis_battle_id), r.post_pvt_event_crisis_battle_ranking_detail, 'total_score', st)
    return r

def event_damage_ranking_battle_ranking_detail(st, req, ctx):
    q = req.post_pvt_event_damage_ranking_battle_ranking_detail
    r = _crisis_ack(st, 'post_pvt_event_damage_ranking_battle_ranking_detail')
    _crisis_detail(q, _crisis_ranked(_crisis_load(st), q.event_crisis_battle_id), r.post_pvt_event_damage_ranking_battle_ranking_detail, 'max_high_score', st)
    return r
for _p, _f in (('/api/pvt/event/crisis/battle/ranking/detail', event_crisis_battle_ranking_detail), ('/api/pvt/event/damage/ranking/battle/ranking/detail', event_damage_ranking_battle_ranking_detail)):
    HANDLERS[_p] = _body_wrap(_me_wrap(_f, _p))
    HANDLERS[_p]._late_wrapped = True

def _solo_continue(name, path, resp_cls):

    def h(st, req, ctx):
        q = getattr(req, name)
        cost = md('m_config')[0][26]
        with st.transaction(path) as tx:
            e = st.one('user_battle_entry_list')
            free = _blue(st)
            paid = next((x.count for x in st.other_info.user_stone_list if x.stone_id == 1), 0) if st.other_info else 0
            if e and e.entry_count == q.entry_count and (free + paid >= cost):
                use = min(free, cost)
                if use:
                    give_stone(st, tx, 2, -use)
                if cost - use:
                    give_stone(st, tx, 1, -(cost - use))
                e.continue_count += 1
                tx.upsert('user_battle_entry_list', e)
            else:
                print(f'{path}: refused (entry {(e.entry_count if e else None)} vs {q.entry_count}, crystals {free}+{paid} < {cost}?)')
        return _resp(tx, name, resp_cls())
    return h
for _n, _p in (('post_pvt_area_solo_battle_continue', 'area/solo/battle/continue'), ('post_pvt_event_solo_battle_continue', 'event/solo/battle/continue'), ('post_pvt_tower_battle_continue', 'tower/battle/continue'), ('post_pvt_highwind_battle_continue', 'highwind/battle/continue'), ('post_pvt_daily_quest_continue', 'daily/quest/continue')):
    _cls = getattr(P, 'Command_ApiNetwork_Api_' + ''.join((w.capitalize() for w in _n.split('_'))) + 'Response')
    HANDLERS['/api/pvt/' + _p] = _solo_continue(_n, _p, _cls)
G_JOIN = P.Command_Enums_UserGuildStatusType.Value('Command_Enums_UserGuildStatusType_Join')

def _all_guilds():
    with _ES._SH_LOCK:
        names = [r[0] for r in _ES._shared(GUILD_ACC).execute("SELECT name FROM kv WHERE name LIKE 'guild:%'")]
    out = []
    for n in names:
        try:
            out.append(int(n.split(':', 1)[1]))
        except ValueError:
            pass
    return sorted(out)

def _gdisp(gid):
    return _gkv_get(f'gdisp:{gid}') or _guild_display_id(gid)

def _gid_by_display(d):
    for gid in _all_guilds():
        if d and d in (_guild_display_id(gid), _gkv_get(f'gdisp:{gid}')):
            return gid
    return 0

def _gwatch(st):
    return [int(x) for x in _gkv_get(f'gwatch:{st.user_id}') or []]

def _ginfo(st, gid, g, info, detail=None, watch=()):
    f = _guild_facts(st, gid, g)
    base = f['base']
    n = len(f['members'])
    info.display_guild_id = _gdisp(gid)
    info.guild_level = _guild_level_of(f['exp'])
    info.member_count = n
    info.is_self_guild = _my_guild(st, None) == gid
    info.is_registered_watch_list = gid in watch
    if base is not None:
        info.guild_name = base.name
        info.introduction = base.introduction
        info.emblem_guild_item_id = base.emblem_guild_item_id
        info.is_manual_approval = base.is_manual_approval
        info.guild_policy_type = base.guild_policy_type
        for i in range(1, 11):
            if base.guild_style_types_idx_flags0 & 1 << i:
                info.guild_style_type_list.append(i)
    if detail is not None:
        detail.member_count = n
        detail.last_active_datetime = f['last_active']
        detail.guild_exp = f['exp']
        detail.guild_master_name = f['master_name']
        if base is not None:
            detail.home_background_guild_item_id = base.home_background_guild_item_id
            detail.guild_created_datetime = base.created_datetime
        for a in g.get('shared_guild_achievement_list', []):
            x = detail.guild_achievement_list.add()
            x.guild_achievement_id = a.guild_achievement_id
            x.progress_count = a.progress_count
        btype = {row[0]: row[1] for row in md('m_guild_bonus')}
        for bo in g.get('shared_guild_bonus_list', []):
            x = detail.guild_bonus_list.add()
            x.guild_bonus_type = btype.get(bo.guild_bonus_id, bo.guild_bonus_id)
            x.level = bo.level

def _guild_list(st, out, keep=lambda base: True):
    mine = _my_guild(st, None)
    watch = _gwatch(st)
    for gid in _all_guilds():
        if gid == mine:
            continue
        with _GUILD_LOCK:
            g = _guild_load(gid)
        if not g:
            continue
        base = next(iter(g.get('shared_guild_base_list', [])), None)
        if base is None or not keep(base):
            continue
        _ginfo(st, gid, g, out.add(), watch=watch)

def guild_search_top(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtGuildSearchTopResponse()
    with st.transaction('guild/search/top') as tx:
        pass
    _guild_list(st, body.guild_info_list)
    body.guild_watch_list_count = len(_gwatch(st))
    return _resp(tx, 'post_pvt_guild_search_top', body)

def guild_search(st, req, ctx):
    q = req.post_pvt_guild_search
    body = P.Command_ApiNetwork_Api_PostPvtGuildSearchResponse()
    pol, sty = (set(q.guild_policy_type_list), set(q.guild_style_type_list))

    def keep(base):
        if q.is_auto_approval != q.is_manual_approval and base.is_manual_approval != q.is_manual_approval:
            return False
        if pol and base.guild_policy_type not in pol:
            return False
        return not sty or any((base.guild_style_types_idx_flags0 & 1 << s for s in sty))
    with st.transaction('guild/search') as tx:
        pass
    _guild_list(st, body.guild_info_list, keep)
    return _resp(tx, 'post_pvt_guild_search', body)

def guild_get(st, req, ctx):
    q = req.post_pvt_guild_get
    body = P.Command_ApiNetwork_Api_PostPvtGuildGetResponse()
    with st.transaction('guild/get') as tx:
        pass
    gid = _gid_by_display(q.display_guild_id)
    with _GUILD_LOCK:
        g = _guild_load(gid) if gid else None
    if g:
        _ginfo(st, gid, g, body.guild_info, body.guild_detail_info, _gwatch(st))
    else:
        print(f'guild/get: no guild {q.display_guild_id!r} on this server')
    body.guild_watch_list_count = len(_gwatch(st))
    return _resp(tx, 'post_pvt_guild_get', body)

def guild_watch_list(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtGuildWatchListResponse()
    w = _gwatch(st)
    with st.transaction('guild/watch/list') as tx:
        pass
    for gid in w:
        with _GUILD_LOCK:
            g = _guild_load(gid)
        if g:
            _ginfo(st, gid, g, body.guild_info_list.add(), watch=w)
    return _resp(tx, 'post_pvt_guild_watch_list', body)

def _watch_edit(add):
    name = 'post_pvt_guild_register_watch_list' if add else 'post_pvt_guild_delete_watch_list'

    def h(st, req, ctx):
        gid = _gid_by_display(getattr(req, name).display_guild_id)
        with st.transaction(name) as tx:
            pass
        if gid:
            with _GUILD_LOCK:
                w = [x for x in _gwatch(st) if x != gid] + ([gid] if add else [])
                w = w[:md('m_config_guild')[0][12]]
                _gkv_set(f'gwatch:{st.user_id}', w)
        cls = 'Register' if add else 'Delete'
        return _resp(tx, name, getattr(P, f'Command_ApiNetwork_Api_PostPvtGuild{cls}WatchListResponse')())
    return h

def _gjoin(gid):
    return {str(k): v for k, v in (_gkv_get(f'gjoin:{gid}') or {}).items()}

def _gjoin_pending_of(uid):
    return [(gid, _gjoin(gid)[str(uid)]) for gid in _all_guilds() if str(uid) in _gjoin(gid)]

def _gjoin_drop(uid, gids=None):
    for gid in gids if gids is not None else _all_guilds():
        d = _gjoin(gid)
        if str(uid) in d:
            d.pop(str(uid))
            _gkv_set(f'gjoin:{gid}', d)

def _guild_join_rows(st, tx, gid, g, now):
    ub = st.one('user_guild_base_list') or st.new_row('user_guild_base_list')
    ub.guild_id = gid
    ub.user_guild_status_type = G_JOIN
    tx.upsert('user_guild_base_list', ub)
    for old in list(st.tables.get('shared_guild_member_list', {}).values()):
        tx.delete('shared_guild_member_list', old)
    tx.upsert('shared_guild_member_list', st.new_row('shared_guild_member_list', guild_id=gid, join_datetime=now, last_active_datetime=now))
    _guild_push(st, tx, g)

def guild_join_list_user(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtGuildJoinListUserResponse()
    w = _gwatch(st)
    with st.transaction('guild/join/list/user') as tx:
        pass
    for gid, _t in _gjoin_pending_of(st.user_id):
        with _GUILD_LOCK:
            g = _guild_load(gid)
        if g:
            info = body.guild_list_infos.add().guild_info
            _ginfo(st, gid, g, info, watch=w)
            info.is_requested_join = True
    return _resp(tx, 'post_pvt_guild_join_list_user', body)

def _guild_own_rows(st, tx):
    own = st.one('shared_guild_member_list')
    if own is not None:
        tx.upsert('shared_guild_member_list', own)
    ub = st.one('user_guild_base_list')
    if ub is not None:
        tx.upsert('user_guild_base_list', ub)

def guild_refresh(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtGuildRefreshResponse()
    gid = _my_guild(st, None)
    now = now_ms()
    with st.transaction('guild/refresh') as tx:
        ub = st.one('user_guild_base_list')
        if ub is not None and ub.user_guild_status_type == G_FORCE_LEAVED:
            body.before_user_guild_status_type = G_FORCE_LEAVED
            cfg = md('m_config_guild')[0]
            if not ub.leave_last_reset_datetime or now - ub.leave_last_reset_datetime >= cfg[6] * 3600000:
                ub.resettable_leave_count = 0
                ub.leave_last_reset_datetime = ub.leave_datetime or now
            ub.resettable_leave_count += 1
            ub.total_leave_count += 1
            ub.user_guild_status_type = 0
            tx.upsert('user_guild_base_list', ub)
            for t in ('user_guild_bonus_list', 'user_guild_play_record_list'):
                for r in list(st.tables.get(t, {}).values()):
                    tx.delete(t, r)
        elif gid:
            with _GUILD_LOCK:
                g = _guild_load(gid)
            if g:
                _guild_push(st, tx, g)
                _guild_own_rows(st, tx)
                body.before_user_guild_status_type = body.after_user_guild_status_type = st.one('user_guild_base_list').user_guild_status_type
    return _resp(tx, 'post_pvt_guild_refresh', body)

def guild_join_request(st, req, ctx):
    q = req.post_pvt_guild_join_request
    now = now_ms()
    cfg = md('m_config_guild')[0]
    with st.transaction('guild/join/request') as tx:
        mine = _my_guild(st, None)
        gid = _gid_by_display(q.display_guild_id)
        with _GUILD_LOCK:
            g = _guild_load(gid) if gid and (not mine) else None
            base = next(iter(g.get('shared_guild_base_list', [])), None) if g else None
            if g and len(_guild_members_here(gid)) >= cfg[7]:
                print(f'guild/join/request: guild {gid} is full')
                g = None
            if g and base is not None and base.is_manual_approval:
                held = _gjoin_pending_of(st.user_id)
                if gid in (x[0] for x in held):
                    print(f'guild/join/request: {st.user_id} already asked guild {gid}')
                elif cfg[10] and len(held) >= cfg[10]:
                    print(f'guild/join/request: {st.user_id} already holds {len(held)} requests (cap {cfg[10]})')
                else:
                    d = _gjoin(gid)
                    d[str(st.user_id)] = now
                    _gkv_set(f'gjoin:{gid}', d)
                    print(f'guild/join/request: {st.user_id} asked to join guild {gid} (manual approval)')
            elif g:
                _guild_join_rows(st, tx, gid, g, now)
                _gjoin_drop(st.user_id)
                print(f'guild/join/request: {st.user_id} joined guild {gid}')
            else:
                print(f'guild/join/request: refused ({q.display_guild_id!r} -> guild {gid}; already in {mine})')
    return _resp(tx, 'post_pvt_guild_join_request', P.Command_ApiNetwork_Api_PostPvtGuildJoinRequestResponse())

def guild_join_cancel(st, req, ctx):
    gid = _gid_by_display(req.post_pvt_guild_join_cancel.display_guild_id)
    with st.transaction('guild/join/cancel') as tx:
        pass
    if gid:
        with _GUILD_LOCK:
            _gjoin_drop(st.user_id, [gid])
    r = _resp(tx)
    r.post_pvt_guild_join_cancel.SetInParent()
    return r

def _gjoin_officer(st):
    gid = _my_guild(st, None)
    own = next((m for m in st.tables.get('shared_guild_member_list', {}).values() if m.guild_id == gid), None) if gid else None
    ok = own is not None and own.guild_role_type in (P.Command_Enums_GuildRoleType.Value('Command_Enums_GuildRoleType_Master'), P.Command_Enums_GuildRoleType.Value('Command_Enums_GuildRoleType_SubMaster'))
    return (gid, own if ok else None)

def guild_join_approve(st, req, ctx):
    q = req.post_pvt_guild_join_approve
    now = now_ms()
    with st.transaction('guild/join/approve') as tx:
        pass
    gid, own = _gjoin_officer(st)
    uid = str(_displays().get(q.display_user_id, ''))
    with _GUILD_LOCK:
        g = _guild_load(gid) if gid and own is not None else None
        if g is None or not uid or uid not in _gjoin(gid):
            print(f"guild/join/approve: refused ({st.user_id} guild {gid} officer {own is not None}, target {q.display_user_id!r} -> {uid or '?'})")
        elif len(_guild_members_here(gid)) >= md('m_config_guild')[0][7]:
            print(f'guild/join/approve: guild {gid} is full')
        else:

            def join(o, otx):
                if _my_guild(o, None):
                    print(f'guild/join/approve: {uid} is already in guild {_my_guild(o, None)}')
                    return False
                _guild_join_rows(o, otx, gid, g, now)
                return True
            ok = _gm_edit_other(uid, join)
            _gjoin_drop(uid, None if ok else [gid])
            if ok:
                print(f'guild/join/approve: {st.user_id} let {uid} into guild {gid}')
    r = _resp(tx)
    r.post_pvt_guild_join_approve.SetInParent()
    return r

def guild_join_reject(st, req, ctx):
    q = req.post_pvt_guild_join_reject
    with st.transaction('guild/join/reject') as tx:
        pass
    gid, own = _gjoin_officer(st)
    uid = str(_displays().get(q.display_user_id, ''))
    if gid and own is not None and uid:
        with _GUILD_LOCK:
            _gjoin_drop(uid, [gid])
        print(f'guild/join/reject: {st.user_id} turned down {uid} for guild {gid}')
    else:
        print(f'guild/join/reject: refused ({st.user_id} guild {gid} officer {own is not None}, target {q.display_user_id!r})')
    r = _resp(tx)
    r.post_pvt_guild_join_reject.SetInParent()
    return r
_guild_top_rec = HANDLERS['/api/pvt/guild/top']

def guild_top_recording(st, req, ctx):
    r = _guild_top_rec(st, req, ctx)
    try:
        gid = _my_guild(st, None)
        cap = _captured(ctx)
        d = cap.post_pvt_guild_top.display_guild_id if cap is not None else ''
        if gid and d and (_gkv_get(f'gdisp:{gid}') != d):
            with _GUILD_LOCK:
                _gkv_set(f'gdisp:{gid}', d)
    except Exception as e:
        print('guild/top: display id not recorded:', repr(e))
    return r
HANDLERS.update({'/api/pvt/guild/search/top': guild_search_top, '/api/pvt/guild/search': guild_search, '/api/pvt/guild/get': guild_get, '/api/pvt/guild/watch/list': guild_watch_list, '/api/pvt/guild/register/watch/list': _watch_edit(True), '/api/pvt/guild/delete/watch/list': _watch_edit(False), '/api/pvt/guild/join/list/user': guild_join_list_user, '/api/pvt/guild/refresh': guild_refresh, '/api/pvt/guild/join/request': guild_join_request, '/api/pvt/guild/top': guild_top_recording})
import random as _grand
G_MASTER = P.Command_Enums_GuildRoleType.Value('Command_Enums_GuildRoleType_Master')

def _gkv_del(*names):
    with _ES._SH_LOCK:
        for n in names:
            _ES._shared(GUILD_ACC).execute('DELETE FROM kv WHERE name=?', (n,))

def _guild_rows_out(st, tx):
    for t in GUILD_TABLES + ['shared_guild_member_list', 'user_guild_bonus_list', 'user_guild_play_record_list']:
        for r in list(st.tables.get(t, {}).values()):
            tx.delete(t, r)

def guild_leave(st, req, ctx):
    now = now_ms()
    cfg = md('m_config_guild')[0]
    with st.transaction('guild/leave') as tx:
        gid = _my_guild(st, None)
        ub = st.one('user_guild_base_list')
        own = next((m for m in st.tables.get('shared_guild_member_list', {}).values() if m.guild_id == gid), None)
        others = [u for u in _guild_members_here(gid) if u != str(st.user_id)] if gid else []
        master = own is not None and own.guild_role_type == G_MASTER
        if not gid or ub is None:
            print(f'guild/leave: {st.user_id} is not in a guild')
        elif master and others:
            print(f'guild/leave: refused, {st.user_id} is master of {gid} with {len(others)} other member(s) here')
        else:
            _guild_rows_out(st, tx)
            if not ub.leave_last_reset_datetime or now - ub.leave_last_reset_datetime >= cfg[6] * 3600000:
                ub.resettable_leave_count = 0
                ub.leave_last_reset_datetime = now
            ub.resettable_leave_count += 1
            ub.total_leave_count += 1
            ub.guild_id = 0
            ub.user_guild_status_type = 0
            ub.leave_datetime = now
            tx.upsert('user_guild_base_list', ub)
            if master and (not others):
                with _GUILD_LOCK:
                    _gkv_del(f'guild:{gid}', f'guildchat:{gid}', f'gdisp:{gid}')
                print(f'guild/leave: {st.user_id} left guild {gid}, its last member and master - guild removed')
            else:
                print(f'guild/leave: {st.user_id} left guild {gid}')
    return _resp(tx, 'post_pvt_guild_leave', P.Command_ApiNetwork_Api_PostPvtGuildLeaveResponse())

def _new_guild_id():
    taken = set(_all_guilds())
    gid = _grand.randrange(10 ** 17, 9 * 10 ** 17)
    while gid in taken:
        gid = _grand.randrange(10 ** 17, 9 * 10 ** 17)
    return gid

def _new_guild_tables(gid, name, now, cfg, manual):
    return {'shared_guild_base_list': [P.Command_Entity_SharedGuildBase(guild_id=gid, name=name, is_manual_approval=manual, guild_style_types_idx_flags0=1, emblem_guild_item_id=cfg[4], home_background_guild_item_id=cfg[5], created_datetime=now, message_edit_datetime=now)], 'shared_guild_exp_list': [P.Command_Entity_SharedGuildExp(guild_id=gid)], 'shared_guild_bonus_list': [P.Command_Entity_SharedGuildBonus(guild_id=gid, guild_bonus_id=cfg[13], level=1, last_reset_datetime=now)], 'shared_guild_item_list': [P.Command_Entity_SharedGuildItem(guild_id=gid, guild_item_id=i, count=1, total_obtain_count=1, get_datetime=now, last_get_datetime=now) for i in (cfg[4], cfg[5])], 'shared_guild_achievement_list': [P.Command_Entity_SharedGuildAchievement(guild_id=gid, guild_achievement_id=102, progress_count=1)]}

def guild_create(st, req, ctx):
    q = req.post_pvt_guild_create
    now = now_ms()
    cfg = md('m_config_guild')[0]
    cost = cfg[8]
    with st.transaction('guild/create') as tx:
        free = _blue(st)
        paid = next((x.count for x in st.other_info.user_stone_list if x.stone_id == 1), 0) if st.other_info else 0
        if _my_guild(st, None) or free + paid < cost:
            print(f'guild/create: refused ({st.user_id} in guild {_my_guild(st, None)}, crystals {free}+{paid} vs {cost})')
            return _resp(tx, 'post_pvt_guild_create', P.Command_ApiNetwork_Api_PostPvtGuildCreateResponse())
        with _GUILD_LOCK:
            gid = _new_guild_id()
            g = _new_guild_tables(gid, q.guild_name or 'Guild', now, cfg, True)
            _guild_save(gid, g)
        use = min(free, cost)
        if use:
            give_stone(st, tx, 2, -use)
        if cost - use:
            give_stone(st, tx, 1, -(cost - use))
        for r in list(st.tables.get('shared_guild_member_list', {}).values()):
            tx.delete('shared_guild_member_list', r)
        tx.upsert('shared_guild_member_list', st.new_row('shared_guild_member_list', guild_id=gid, guild_role_type=G_MASTER, join_datetime=now, last_active_datetime=now))
        ub = st.one('user_guild_base_list') or st.new_row('user_guild_base_list')
        ub.guild_id = gid
        ub.user_guild_status_type = G_JOIN
        tx.upsert('user_guild_base_list', ub)
        tx.upsert('user_guild_bonus_list', st.new_row('user_guild_bonus_list', guild_bonus_id=cfg[13], level=1))
        _guild_push(st, tx, g)
        with _GUILD_LOCK:
            _gjoin_drop(st.user_id)
        print(f'guild/create: {st.user_id} created guild {gid} {q.guild_name!r}')
    return _resp(tx, 'post_pvt_guild_create', P.Command_ApiNetwork_Api_PostPvtGuildCreateResponse())

def guild_message_read(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtGuildMessageReadResponse()
    now = now_ms()
    with st.transaction('guild/message/read') as tx:
        gid = _my_guild(st, None)
        with _GUILD_LOCK:
            g = _guild_load(gid) if gid else None
        if g:
            own = next((m for m in st.tables.get('shared_guild_member_list', {}).values() if m.guild_id == gid), None)
            if own is not None:
                own.message_read_datetime = now
            _guild_push(st, tx, g)
            _guild_own_rows(st, tx)
            base = next(iter(g.get('shared_guild_base_list', [])), None)
            edited = base.message_edit_datetime if base is not None else 0
            here = _guild_members_here(gid)
            if own is not None:
                here[str(st.user_id)] = own
            body.guild_message_read_count = sum((1 for m in here.values() if m.message_read_datetime >= edited))
    return _resp(tx, 'post_pvt_guild_message_read', body)
_join_list_view = _guild_view('post_pvt_guild_join_list_guild')

def guild_join_list_guild(st, req, ctx):
    r = _join_list_view(st, req, ctx)
    gid, own = _gjoin_officer(st)
    if gid and own is not None:
        ids = _uid_to_display(_displays())
        for uid, t in sorted(_gjoin(gid).items(), key=lambda kv: kv[1]):
            info = _list_info(uid, ids.get(uid))
            if info is None:
                continue
            x = r.post_pvt_guild_join_list_guild.join_request_user_list_infos.add()
            x.user_list_info.CopyFrom(info)
            x.request_datetime = t
    return r
HANDLERS.update({'/api/pvt/guild/leave': guild_leave, '/api/pvt/guild/create': guild_create, '/api/pvt/guild/message/read': guild_message_read, '/api/pvt/guild/join/list/guild': guild_join_list_guild, '/api/pvt/guild/join/cancel': guild_join_cancel, '/api/pvt/guild/join/approve': guild_join_approve, '/api/pvt/guild/join/reject': guild_join_reject})
GB_HS_TOP = 10

def _gb_rows_here():
    out = {}
    for db in _glob.glob(os.path.join(_ACC, '*', 'save.db')):
        folder = os.path.dirname(db)
        live = getattr(State, 'LIVE', {}).get(os.path.realpath(folder))
        try:
            if live is not None:
                uid = str(live.user_id)
                hs = {r.event_guild_ranking_battle_id: r.high_score for r in list(live.tables.get('user_event_guild_ranking_battle_list', {}).values())}
                b = live.one('user_guild_base_list')
                gid = b.guild_id if b is not None else 0
            else:
                c = _sq3.connect('file:' + db + '?mode=ro', uri=True)
                try:
                    r = c.execute("SELECT val FROM meta WHERE key='user_id'").fetchone()
                    uid = str(r[0]) if r else None
                    hs = {}
                    for pb, in c.execute("SELECT pb FROM rows WHERE tbl='user_event_guild_ranking_battle_list'"):
                        m = P.Command_Entity_UserEventGuildRankingBattle()
                        m.ParseFromString(pb)
                        hs[m.event_guild_ranking_battle_id] = m.high_score
                    gid = 0
                    for pb, in c.execute("SELECT pb FROM rows WHERE tbl='user_guild_base_list'"):
                        m = P.Command_Entity_UserGuildBase()
                        m.ParseFromString(pb)
                        gid = m.guild_id or gid
                finally:
                    c.close()
        except RuntimeError:
            continue
        except Exception as e:
            print(f'guild high scores: {db}: {e!r}')
            continue
        if uid:
            out[uid] = (gid, hs)
    return out

def _gb_high_score_list(field, own_guild_only):

    def h(st, req, ctx):
        bid = getattr(req, field).event_guild_ranking_battle_id
        here = _gb_rows_here()
        mine = st.one('user_guild_base_list')
        here[str(st.user_id)] = (mine.guild_id if mine is not None else 0, {r.event_guild_ranking_battle_id: r.high_score for r in st['user_event_guild_ranking_battle_list'].values()})
        my_gid = here[str(st.user_id)][0]
        rows = [(u, gid, hs.get(bid, 0)) for u, (gid, hs) in here.items() if hs.get(bid, 0) > 0 and (not own_guild_only or (my_gid and gid == my_gid))]
        rows.sort(key=lambda x: (-x[2], int(x[0]) if x[0].isdigit() else 0))
        with st.transaction(field.replace('post_pvt_', '').replace('_', '/')) as tx:
            pass
        r = _resp(tx)
        b = getattr(r, field)
        b.SetInParent()
        names = {}
        for i, (u, gid, sc) in enumerate(rows[:GB_HS_TOP]):
            e = b.high_score_user_info_list.add()
            e.rank = i + 1
            e.high_score = sc
            info = _list_info(u)
            if info is not None:
                e.display_user_id = info.display_user_id
                e.player_name = info.player_name
                e.top_honor_id, e.bottom_honor_id, e.background_honor_id, e.icon_honor_id = (info.top_honor_id, info.bottom_honor_id, info.background_honor_id, info.icon_honor_id)
            if gid:
                try:
                    if gid not in names:
                        g = _guild_load(gid) or {}
                        base = next(iter(g.get('shared_guild_base_list', [])), None)
                        names[gid] = base.name if base is not None else ''
                    e.display_guild_id = _gdisp(gid)
                    e.guild_name = names[gid]
                except Exception as ex:
                    print(f'guild high scores: guild {gid} for {u}: {ex!r}')
            try:
                o = st if str(u) == str(st.user_id) else _other_state(u)
                if o is not None:
                    _full_party_info(o, e.party_info)
                    for a in o['user_award_list'].values():
                        ai = e.award_info_list.add()
                        ai.award_id = a.award_id
                        ai.award_count = a.award_count
            except Exception as ex:
                print(f'guild high scores: party for {u} failed: {ex!r}')
        return r
    return h
HANDLERS.update({'/api/pvt/event/guild/ranking/battle/high/score/list': _gb_high_score_list('post_pvt_event_guild_ranking_battle_high_score_list', False), '/api/pvt/event/guild/ranking/battle/guild/high/score/list': _gb_high_score_list('post_pvt_event_guild_ranking_battle_guild_high_score_list', True)})

# Guild Battle mock fights for an old event picked in the launcher (engine/seasons.py opens its dates). The game asks
# Square's battle server for the boss's shared HP; the PC patch (tools/scoreboss.py patch_guild_practice) fights in the
# game instead and packs the damage into the end request's battle id, as the Max Damage Challenge does. Every mock
# fight starts at full HP and scores its damage; the best score is the in-game high score.
GB_DAMAGE_SHIFT = 20
GB_FULL = 10000
GB_GUILD_NAME = 'Reawakened'
GB_SUCCESS = P.Command_Enums_GuildBattleResponseType.Value('Command_Enums_GuildBattleResponseType_Success')
GB_COMPLETE = P.Command_Enums_EventGuildRankingBattleEndStatusType.Value('Command_Enums_EventGuildRankingBattleEndStatusType_Complete')

def _gb_open():
    try:
        from engine import seasons
        return seasons.chosen_guild_battle(storage.home())
    except Exception as e:
        print(f'guild battle: setting unreadable: {e!r}')
        return 0

def _gb_battles(event_id):
    return sorted((r for r in md('m_event_guild_ranking_battle') if r[1] == event_id), key=lambda r: r[0])

def _gb_boss_hp(battle_id):
    """The boss's full HP as the game adds it up: each wave's boss enemies (all of them when none is marked boss) at
    their level, BaseHp x HpCoefficient / 1000."""
    b = mdx('m_battle', 0).get((battle_id,))
    if not b:
        return 0
    enemies, levels = (mdx('m_enemy', 0), mdx('m_enemy_level_parameter_base', 0, 1))
    total = 0
    for w in (r for r in md('m_battle_wave') if r[0] == b[2]):
        group = [r for r in md('m_battle_enemy') if r[0] == w[2]]
        for be in [r for r in group if r[5] == 2] or group:
            e = enemies.get((be[2],))
            if not e:
                continue
            lv = levels.get((be[9] or e[15], be[3]))
            if lv:
                total += lv[2] * e[7] // 1000
    return total

def _gb_info(info, bid):
    info.event_guild_ranking_battle_id = bid
    info.enemy_remain_hp_rate_permyriad = GB_FULL         # a mock fight: nobody is in a live battle

_gb_top_view = _guild_view(None)

def guild_battle_top(st, req, ctx):
    r = _gb_top_view(st, req, ctx)
    b = r.post_pvt_event_guild_ranking_top
    b.SetInParent()
    gid = _my_guild(st, None)
    if gid:
        b.display_guild_id = _gdisp(gid)
    for row in _gb_battles(_gb_open()):
        _gb_info(b.event_guild_ranking_battle_info_list.add(), row[0])
    return r

def guild_battle_enemy_get(st, req, ctx):
    with st.transaction('event/guild/ranking/battle/enemy/get') as tx:
        pass
    body = P.Command_ApiNetwork_Api_PostPvtEventGuildRankingBattleEnemyGetResponse(enemy_remain_hp_rate_permyriad=GB_FULL)
    return _resp(tx, 'post_pvt_event_guild_ranking_battle_enemy_get', body)

def guild_ranking_list(st, req, ctx):
    with st.transaction('event/guild/ranking/list') as tx:
        pass
    return _resp(tx, 'post_pvt_event_guild_ranking_list', P.Command_ApiNetwork_Api_PostPvtEventGuildRankingListResponse())

def guild_battle_practice_start(st, req, ctx):
    q = req.post_pvt_event_guild_ranking_battle_practice_start
    row = mdx('m_event_guild_ranking_battle', 0).get((q.event_guild_ranking_battle_id,))
    body = P.Command_ApiNetwork_Api_PostPvtEventGuildRankingBattlePracticeStartResponse()
    with st.transaction('event/guild/ranking/battle/practice/start') as tx:
        if row:
            e = _new_entry(st, tx, row[3], row[0], 'Command_Enums_BattleType_GuildRanking', q.party_id, 0, now_ms(), ctx=ctx)
            body.entry_count = e.entry_count
            body.battle_enemy_info.SetInParent()
            body.guild_battle_response_type = GB_SUCCESS
        else:
            print(f'guild battle: practice start for unknown fight {q.event_guild_ranking_battle_id}')
    return _resp(tx, 'post_pvt_event_guild_ranking_battle_practice_start', body)

def guild_battle_practice_end(st, req, ctx):
    q = req.post_pvt_event_guild_ranking_battle_practice_end
    raw = q.event_guild_ranking_battle_id
    bid, damage = (raw & (1 << GB_DAMAGE_SHIFT) - 1, raw >> GB_DAMAGE_SHIFT)
    damage = damage or q.achieve_damage
    q.event_guild_ranking_battle_id = bid
    row = mdx('m_event_guild_ranking_battle', 0).get((bid,))
    body = P.Command_ApiNetwork_Api_PostPvtEventGuildRankingBattlePracticeEndResponse()
    with st.transaction('event/guild/ranking/battle/practice/end') as tx:
        if row:
            _end_entry(st, tx, row[3])
            hp = _gb_boss_hp(row[3])
            s = body.battle_score_result
            s.before_enemy_remain_hp = hp
            s.achieve_damage = damage
            s.actual_damage = min(damage, hp) if hp else damage     # member_damage stays 0: no one else fights a mock
            s.battle_score = s.total_score = damage
            for c in st['user_character_list'].values():
                s.before_user_characters.add().CopyFrom(c)
                s.after_user_characters.add().CopyFrom(c)
            if damage > 0:
                ub = st.get('user_event_guild_ranking_battle_list', bid) or st.new_row('user_event_guild_ranking_battle_list', event_guild_ranking_battle_id=bid)
                if damage > ub.practice_battle_best_total_score:
                    ub.practice_battle_best_score = ub.practice_battle_best_total_score = damage
                    ub.high_score = max(ub.high_score, damage)
                    tx.upsert('user_event_guild_ranking_battle_list', ub)
        else:
            print(f'guild battle: practice end for unknown fight {bid}')
        body.battle_end_status_type = GB_COMPLETE
        body.battle_input.CopyFrom(q.battle_input)
        body.battle_input.total_damage = damage
    return _resp(tx, 'post_pvt_event_guild_ranking_battle_practice_end', body)

def _gb_guild_heal(st):
    """A Guild Battle picked in the launcher needs a guild: a player in none joins the shared Reawakened guild (made
    on first need, its first player the master), so the Guild Battle button and its menus open."""
    event = _gb_open()
    if not event or _my_guild(st, None):
        return
    now = now_ms()
    cfg = md('m_config_guild')[0]
    with st.transaction('guild battle guild') as tx:
        with _GUILD_LOCK:
            gid = _gkv_get('gb_guild') or 0
            g = _guild_load(gid) if gid else None
            if g is not None and len(_guild_members_here(gid)) >= cfg[7]:
                print(f'guild battle: the {GB_GUILD_NAME} guild is full, {st.user_id} stays out')
                return
            role = 0
            if g is None:
                gid = _new_guild_id()
                g = _new_guild_tables(gid, GB_GUILD_NAME, now, cfg, False)
                _guild_save(gid, g)
                _gkv_set('gb_guild', gid)
                role = G_MASTER
            _gjoin_drop(st.user_id)
        _guild_join_rows(st, tx, gid, g, now)
        if role:
            own = st.one('shared_guild_member_list')
            own.guild_role_type = role
            tx.upsert('shared_guild_member_list', own)
        tx.upsert('user_guild_bonus_list', st.new_row('user_guild_bonus_list', guild_bonus_id=cfg[13], level=1))
        if mdx('m_event_base', 0).get((event,)):
            r = st.get('user_event_guild_ranking_base_list', event) or st.new_row('user_event_guild_ranking_base_list', event_base_id=event)
            r.guild_id = gid
            tx.upsert('user_event_guild_ranking_base_list', r)
        print(f'guild battle: {st.user_id} joined the {GB_GUILD_NAME} guild {gid}')
HANDLERS.update({'/api/pvt/event/guild/ranking/top': guild_battle_top, '/api/pvt/event/guild/ranking/battle/enemy/get': guild_battle_enemy_get, '/api/pvt/event/guild/ranking/list': guild_ranking_list, '/api/pvt/event/guild/ranking/battle/practice/start': guild_battle_practice_start, '/api/pvt/event/guild/ranking/battle/practice/end': guild_battle_practice_end})

def weapon_limit_reset(st, req, ctx):
    q = req.post_pvt_weapon_limit_reset
    per = md('m_config')[0][143] or 200
    rank_top = {}
    for r in md('m_weapon_upgrade_consumption'):
        if r[1] == 1:
            rank_top[r[0]] = max(rank_top.get(r[0], 0), r[2])
    body = P.Command_ApiNetwork_Api_PostPvtWeaponLimitResetResponse()
    LIMIT, RANK = (P.Command_Enums_WeaponUpgradeType_Limit, P.Command_Enums_WeaponUpgradeType_Rank)
    with st.transaction('weapon/limit/reset') as tx:
        refund = {}
        for wid in q.weapon_ids:
            w = st.get('user_weapon_list', wid)
            mw = _weapon_row(wid)
            if not w or not mw or w.weapon_upgrade_type != LIMIT or (w.upgrade_count <= 0) or (not mw[18]):
                continue
            refund[mw[18]] = refund.get(mw[18], 0) + per * w.upgrade_count
            if not q.is_dry_run:
                w.weapon_upgrade_type = RANK
                w.upgrade_count = rank_top.get(mw[15], 10)
                tx.touch('user_weapon_list', w)
        for item_id, n in refund.items():
            if not q.is_dry_run:
                add_item(st, tx, item_id, n)
            ri = body.reward_info_list.add()
            ri.reward_type = RT_ITEM
            ri.target_id = item_id
            ri.count = n
    r = _resp(tx, 'post_pvt_weapon_limit_reset', body)
    r.post_pvt_weapon_limit_reset.SetInParent()
    return r

def weapon_medal_exchange(st, req, ctx):
    q = req.post_pvt_weapon_medal_exchange
    rate = md('m_config')[0][51] or 1
    body = P.Command_ApiNetwork_Api_PostPvtWeaponMedalExchangeResponse()
    with st.transaction('weapon/medal/exchange') as tx:
        total = 0
        for item_id, n in q.weapon_medal_map.items():
            row = st.get('user_item_list', item_id)
            if n > 0 and row is not None and (row.count >= n):
                add_item(st, tx, item_id, -n)
                total += n * rate
            elif n > 0:
                print(f'weapon/medal/exchange: {st.user_id} has {(row.count if row else 0)} of {item_id}, asked {n}: skipped')
        if total:
            add_item(st, tx, 14, total)
            ri = body.reward_info_list.add()
            ri.reward_type = RT_ITEM
            ri.target_id = 14
            ri.count = total
    r = _resp(tx, 'post_pvt_weapon_medal_exchange', body)
    r.post_pvt_weapon_medal_exchange.SetInParent()
    return r

def dungeon_another_continue(st, req, ctx):
    with st.transaction('dungeon/another/continue') as tx:
        lost = [x for x in st['user_dungeon_entry_list'].values() if x.dungeon_result_type == P.Command_Enums_DungeonResultType_Lose]
        de = lost[0] if len(lost) == 1 else st.get('user_dungeon_entry_list', P.Command_Enums_DungeonType_Another)
        dt = de.dungeon_type if de is not None else P.Command_Enums_DungeonType_Another
        if de is not None:
            de.dungeon_progress_type = 1
            de.dungeon_result_type = 0
            tx.upsert('user_dungeon_entry_list', de)
            ae = st.get('user_another_dungeon_entry_list', dt)
            if ae is not None:
                tx.touch('user_another_dungeon_entry_list', ae)
        else:
            print(f'dungeon/another/continue: {st.user_id} has no dungeon entry of type {dt}')
    r = _resp(tx)
    r.post_pvt_dungeon_another_continue.SetInParent()
    return r

def _copy_same_fields(src, dst):
    names = set(dst.DESCRIPTOR.fields_by_name)
    for f, v in src.ListFields():
        if f.name in names and f.name != 'user_id':
            if f.message_type is not None and getattr(f, 'is_repeated', False):
                getattr(dst, f.name).extend(v)
            elif f.message_type is not None:
                getattr(dst, f.name).CopyFrom(v)
            else:
                setattr(dst, f.name, v)

def _dungeon_save_load(field, saving):

    def h(st, req, ctx):
        dt = getattr(req, field).dungeon_type
        with st.transaction(field.replace('post_pvt_', '').replace('_', '/')) as tx:
            if dt == P.Command_Enums_DungeonType_Another:
                entry = next((r for r in st.tables.get('user_another_dungeon_entry_list', {}).values() if r.dungeon_type == dt), None)
                save = next((r for r in st.tables.get('user_another_dungeon_save_list', {}).values() if r.dungeon_type == dt), None)
                if saving and entry is not None:
                    if save is None:
                        save = st.new_row('user_another_dungeon_save_list', dungeon_type=dt)
                    else:
                        save.Clear()
                        save.user_id = st.user_id
                        save.dungeon_type = dt
                    _copy_same_fields(entry, save)
                    tx.upsert('user_another_dungeon_save_list', save)
                    entry.save_datetime = now_ms()
                    tx.upsert('user_another_dungeon_entry_list', entry)
                elif not saving and save is not None:
                    keep = entry.save_datetime if entry is not None else 0
                    if entry is None:
                        entry = st.new_row('user_another_dungeon_entry_list', dungeon_type=dt)
                    else:
                        entry.Clear()
                        entry.user_id = st.user_id
                        entry.dungeon_type = dt
                    _copy_same_fields(save, entry)
                    entry.save_datetime = keep
                    tx.upsert('user_another_dungeon_entry_list', entry)
                else:
                    print(f"{field}: nothing to {('save' if saving else 'load')} for {st.user_id} (entry {entry is not None}, save {save is not None})")
        r = _resp(tx)
        getattr(r, field).SetInParent()
        return r
    return h
HANDLERS.update({'/api/pvt/weapon/limit/reset': weapon_limit_reset, '/api/pvt/weapon/medal/exchange': weapon_medal_exchange, '/api/pvt/dungeon/another/continue': dungeon_another_continue, '/api/pvt/dungeon/save': _dungeon_save_load('post_pvt_dungeon_save', True), '/api/pvt/dungeon/load': _dungeon_save_load('post_pvt_dungeon_load', False)})
G_SUBMASTER = P.Command_Enums_GuildRoleType.Value('Command_Enums_GuildRoleType_SubMaster')
G_NONE = P.Command_Enums_GuildRoleType.Value('Command_Enums_GuildRoleType_None')
G_FORCE_LEAVED = P.Command_Enums_UserGuildStatusType.Value('Command_Enums_UserGuildStatusType_ForceLeaved')

def _gm_own(st, gid):
    return next((m for m in st.tables.get('shared_guild_member_list', {}).values() if m.guild_id == gid), None)

def _gm_edit_other(uid, fn):
    f = _folder(uid)
    if not f:
        return False
    live = getattr(State, 'LIVE', {}).get(os.path.realpath(f))
    if live is not None:
        if not live.lock.acquire(timeout=10):
            print(f'guild: {uid} is busy, not changed')
            return False
        try:
            with live.transaction('guild: changed by another member') as tx:
                ok = fn(live, tx)
            if ok:
                live.save()
            return ok
        finally:
            live.lock.release()
    o = State.load(f)
    with o.transaction('guild: changed by another member') as tx:
        ok = fn(o, tx)
    if ok:
        o.save()
    return ok

def _gm_bonus_mirror(st, tx, g):
    for b in g.get('shared_guild_bonus_list', []):
        u = st.get('user_guild_bonus_list', b.guild_bonus_id)
        if u is None or u.level != b.level:
            u = u or st.new_row('user_guild_bonus_list', guild_bonus_id=b.guild_bonus_id)
            u.level = b.level
            tx.upsert('user_guild_bonus_list', u)

def guild_leave_force(st, req, ctx):
    q = req.post_pvt_guild_leave_force
    now = now_ms()
    with st.transaction('guild/leave/force') as tx:
        gid = _my_guild(st, None)
        me = _gm_own(st, gid) if gid else None
        uid = str(_displays().get(q.display_user_id, ''))
        if not gid or me is None or (not uid) or (uid == str(st.user_id)):
            print(f"guild/leave/force: refused ({st.user_id} guild {gid}, target {q.display_user_id!r} -> {uid or '?'})")
        else:

            def kick(o, otx):
                m = _gm_own(o, gid)
                if m is None:
                    print(f'guild/leave/force: {uid} is not in guild {gid}')
                    return False
                if me.guild_role_type != G_MASTER and (not (me.guild_role_type == G_SUBMASTER and m.guild_role_type == G_NONE)):
                    print(f"guild/leave/force: refused, {st.user_id} (role {me.guild_role_type}) can't remove {uid} (role {m.guild_role_type})")
                    return False
                for t in GUILD_TABLES + ['shared_guild_member_list']:
                    for row in list(o.tables.get(t, {}).values()):
                        otx.delete(t, row)
                ub = o.one('user_guild_base_list') or o.new_row('user_guild_base_list')
                ub.guild_id = 0
                ub.user_guild_status_type = G_FORCE_LEAVED
                ub.leave_datetime = now
                otx.upsert('user_guild_base_list', ub)
                return True
            if _gm_edit_other(uid, kick):
                print(f'guild/leave/force: {st.user_id} removed {uid} from guild {gid}')
        if gid:
            with _GUILD_LOCK:
                g = _guild_load(gid)
            if g:
                _guild_push(st, tx, g)
    r = _resp(tx)
    r.post_pvt_guild_leave_force.SetInParent()
    return r

def guild_edit_member_role(st, req, ctx):
    q = req.post_pvt_guild_edit_member_role
    cap = md('m_config_guild')[0][14] or 4
    with st.transaction('guild/edit/member/role') as tx:
        gid = _my_guild(st, None)
        me = _gm_own(st, gid) if gid else None
        uid = str(_displays().get(q.display_user_id, ''))
        role = q.guild_role_type
        if not gid or me is None or me.guild_role_type != G_MASTER or (not uid) or (uid == str(st.user_id)) or (role not in (G_NONE, G_SUBMASTER, G_MASTER)):
            print(f"guild/edit/member/role: refused ({st.user_id} guild {gid} role {(me.guild_role_type if me else None)}, target {q.display_user_id!r} -> {uid or '?'}, role {role})")
        else:
            others = _guild_members_here(gid)
            subs = sum((1 for u, m in others.items() if u not in (uid, str(st.user_id)) and m.guild_role_type == G_SUBMASTER))
            if role == G_SUBMASTER and subs >= cap:
                print(f'guild/edit/member/role: refused, guild {gid} already has {subs} SubMasters (cap {cap})')
            else:

                def setrole(o, otx):
                    m = _gm_own(o, gid)
                    if m is None:
                        print(f'guild/edit/member/role: {uid} is not in guild {gid}')
                        return False
                    m.guild_role_type = role
                    otx.upsert('shared_guild_member_list', m)
                    return True
                if _gm_edit_other(uid, setrole):
                    if role == G_MASTER:
                        me.guild_role_type = G_NONE
                        tx.upsert('shared_guild_member_list', me)
                    print(f'guild/edit/member/role: {st.user_id} set {uid} to role {role} in guild {gid}')
    r = _resp(tx)
    r.post_pvt_guild_edit_member_role.SetInParent()
    return r

def guild_bonus_level_up(st, req, ctx):
    q = req.post_pvt_guild_bonus_level_up
    with st.transaction('guild/bonus/level/up') as tx:
        gid = _my_guild(st, None)
        me = _gm_own(st, gid) if gid else None
        if not gid or me is None or me.guild_role_type not in (G_MASTER, G_SUBMASTER):
            print(f'guild/bonus/level/up: refused ({st.user_id} guild {gid} role {(me.guild_role_type if me else None)})')
        else:
            gb = mdx('m_guild_bonus', 0).get((q.guild_bonus_id,))
            steps = {r[1]: r[2] for r in md('m_guild_bonus_level') if gb and r[0] == gb[2]}
            with _GUILD_LOCK:
                g = _guild_load(gid)
                if g is None or gb is None or q.guild_bonus_level not in steps:
                    print(f'guild/bonus/level/up: refused (guild record {g is not None}, bonus {q.guild_bonus_id}, level {q.guild_bonus_level})')
                else:
                    rows = g.setdefault('shared_guild_bonus_list', [])
                    b = next((x for x in rows if x.guild_bonus_id == q.guild_bonus_id), None)
                    if b is None:
                        b = P.Command_Entity_SharedGuildBonus(guild_id=gid, guild_bonus_id=q.guild_bonus_id, level=0)
                        rows.append(b)
                    exp_rows = g.setdefault('shared_guild_exp_list', [])
                    if not exp_rows:
                        exp_rows.append(P.Command_Entity_SharedGuildExp(guild_id=gid))
                    cost = sum((steps.get(l, 0) for l in range(b.level + 1, q.guild_bonus_level + 1)))
                    if q.guild_bonus_level <= b.level or exp_rows[0].bonus_level_exp < cost:
                        print(f'guild/bonus/level/up: refused (bonus {q.guild_bonus_id} at {b.level}, asked {q.guild_bonus_level}, cost {cost}, pool {exp_rows[0].bonus_level_exp})')
                    else:
                        exp_rows[0].bonus_level_exp -= cost
                        b.level = q.guild_bonus_level
                        _guild_save(gid, g)
                        print(f'guild/bonus/level/up: guild {gid} bonus {q.guild_bonus_id} -> {b.level} for {cost} bonus EXP')
                if g is not None:
                    _guild_push(st, tx, g)
                    _gm_bonus_mirror(st, tx, g)
    r = _resp(tx)
    r.post_pvt_guild_bonus_level_up.SetInParent()
    return r

def _gm_mirror_wrap(fn):

    def h(st, req, ctx):
        r = fn(st, req, ctx)
        try:
            gid = _my_guild(st, None)
            if gid:
                with _GUILD_LOCK:
                    g = _guild_load(gid)
                if g is not None:
                    with st.transaction('guild: bonus levels follow the guild') as tx:
                        _gm_bonus_mirror(st, tx, g)
                    if len(tx.up.user_guild_bonus_list):
                        have = {x.guild_bonus_id: x for x in r.common.user.update.user_guild_bonus_list}
                        for row in tx.up.user_guild_bonus_list:
                            if row.guild_bonus_id in have:
                                have[row.guild_bonus_id].CopyFrom(row)
                            else:
                                r.common.user.update.user_guild_bonus_list.add().CopyFrom(row)
        except Exception as e:
            print(f'guild bonus mirror failed for {st.user_id}: {e!r}')
        return r
    return h
for _p in ('/api/pvt/guild/top', '/api/pvt/guild/sync', '/api/pvt/guild/get/self'):
    if _p in HANDLERS:
        HANDLERS[_p] = _gm_mirror_wrap(HANDLERS[_p])
HANDLERS.update({'/api/pvt/guild/leave/force': guild_leave_force, '/api/pvt/guild/edit/member/role': guild_edit_member_role, '/api/pvt/guild/bonus/level/up': guild_bonus_level_up})
import threading as _thr_c
RT_INSTANT = P.Command_Enums_RewardType.Value('Command_Enums_RewardType_InstantItem')
_II = _thr_c.local()

def _ii_load(de):
    try:
        d = json.loads(de.instant_item_info) if de is not None and de.instant_item_info else {}
    except ValueError:
        print(f'instant items: unreadable instant_item_info {de.instant_item_info!r}, starting empty')
        d = {}
    return d if isinstance(d, dict) else {}

def _ii_dump(d, with_list):
    out = {'instant_item_map': {k: d.get('instant_item_map', {})[k] for k in sorted(d.get('instant_item_map', {}))}}
    u = d.get('used_instant_item_map') or {}
    if u:
        out['used_instant_item_map'] = {k: u[k] for k in sorted(u)}
    if with_list and d.get('used_instant_item_list'):
        out['used_instant_item_list'] = d['used_instant_item_list']
    return json.dumps(out, separators=(',', ':')) + '\n'
_grant_reward_before_c = grant_reward

def grant_reward(st, tx, reward_id, count, infos=None):
    rw = mdx('m_reward', 0).get((reward_id,))
    if not rw or rw[1] != RT_INSTANT:
        return _grant_reward_before_c(st, tx, reward_id, count, infos)
    dt = getattr(_II, 'dt', None)
    de = st.get('user_dungeon_entry_list', dt) if dt else None
    if de is None:
        print(f'grant_reward: InstantItem {rw[2]} x{count} for {st.user_id} outside a dungeon - dropped')
    else:
        d = _ii_load(de)
        m = d.setdefault('instant_item_map', {})
        k = str(rw[2])
        m[k] = m.get(k, 0) + count
        de.instant_item_info = _ii_dump(d, False)
        tx.upsert('user_dungeon_entry_list', de)
    if infos is not None:
        ri = infos.add()
        ri.reward_type = RT_INSTANT
        ri.target_id = rw[2]
        ri.count = count

def _ii_treasure(fn):

    def h(st, req, ctx):
        _II.dt = req.post_pvt_dungeon_another_treasure.dungeon_type
        try:
            return fn(st, req, ctx)
        finally:
            _II.dt = None
    return h

def _ii_clear_first(field):

    def wrap(fn):

        def h(st, req, ctx):
            de = st.get('user_dungeon_entry_list', getattr(req, field).dungeon_type)
            if de is not None:
                de.instant_item_info = ''
            return fn(st, req, ctx)
        return h
    return wrap

def dungeon_another_item(st, req, ctx):
    q = req.post_pvt_dungeon_another_item
    with st.transaction('dungeon/another/item') as tx:
        de = st.get('user_dungeon_entry_list', q.dungeon_type)
        d = _ii_load(de)
        m = d.setdefault('instant_item_map', {})
        k = str(q.instant_item_id)
        if de is None or m.get(k, 0) <= 0:
            print(f'dungeon/another/item: refused, {st.user_id} holds {m.get(k, 0)} of {k} (entry {de is not None})')
        else:
            m[k] -= 1
            u = d.setdefault('used_instant_item_map', {})
            u[k] = u.get(k, 0) + 1
            d.setdefault('used_instant_item_list', []).append({'instant_item_id': q.instant_item_id, 'character_id': q.character_id})
            de.instant_item_info = _ii_dump(d, True)
            tx.upsert('user_dungeon_entry_list', de)
    r = _resp(tx)
    r.post_pvt_dungeon_another_item.SetInParent()
    return r

def limited_release_start(st, req, ctx):
    q = req.post_pvt_limited_release_start
    now = now_ms()
    with st.transaction('limited/release/start') as tx:
        for i in q.limited_release_ids:
            row = st.get('user_limited_release_list', i)
            if row is None:
                tx.upsert('user_limited_release_list', st.new_row('user_limited_release_list', limited_release_id=i, release_start_datetime=now))
            else:
                tx.touch('user_limited_release_list', row)
    r = _resp(tx)
    r.post_pvt_limited_release_start.SetInParent()
    return r

def dungeon_delete(st, req, ctx):
    q = req.post_pvt_dungeon_delete
    with st.transaction('dungeon/delete') as tx:
        de = st.get('user_dungeon_entry_list', q.dungeon_type)
        if de is not None:
            de.dungeon_progress_type = DUNGEON_END
            de.instant_item_info = ''
            tx.upsert('user_dungeon_entry_list', de)
        else:
            print(f'dungeon/delete: {st.user_id} has no entry for dungeon type {q.dungeon_type}')
    r = _resp(tx)
    r.post_pvt_dungeon_delete.SetInParent()
    return r

def guild_battle_member_list(st, req, ctx):
    with st.transaction('event/guild/ranking/battle/member/list') as tx:
        pass
    r = _resp(tx)
    b = r.post_pvt_event_guild_ranking_battle_member_list
    b.SetInParent()
    gid = _my_guild(st, None)
    if not gid:
        return r
    members = _guild_members_here(gid)
    members.setdefault(str(st.user_id), None)
    for u in sorted(members, key=lambda x: int(x) if x.isdigit() else 0):
        info = _list_info(u)
        if info is None:
            continue
        e = b.member_list_infos.add()
        e.user_list_info.CopyFrom(info)
        try:
            o = st if u == str(st.user_id) else _other_state(u)
            for a in o['user_award_list'].values() if o is not None else ():
                ai = e.user_list_info.award_info_list.add()
                ai.award_id = a.award_id
                ai.award_count = a.award_count
        except Exception as ex:
            print(f'guild battle member list: awards for {u} failed: {ex!r}')
    return r

def announcement_detail(st, req, ctx):
    with st.transaction('announcement/detail') as tx:
        pass
    r = _resp(tx)
    r.post_announcement_detail.SetInParent()
    n = next((n for n in _notices() if n['id'] == req.post_announcement_detail.id), None)
    if n:
        d = r.post_announcement_detail.announcement_detail
        _notice_summary(d.summary, n)
        d.data = json.dumps({'announcementDetailParameters': [{
            'announcementBlockType': _NOTICE_BODY, 'commonBodyText': n['body'], 'isShowBodyBackGround': False,
            'commonBodyTextAlignmentOptions': _NOTICE_TOP_LEFT, 'headerText': '', 'bannerResourceId': 0, 'gachaId': 0,
            'announceId': n['id'], 'bannerImagePath': ''}]})
    return r
HANDLERS.update({'/api/pvt/dungeon/another/item': dungeon_another_item, '/api/pvt/limited/release/start': limited_release_start, '/api/pvt/dungeon/delete': dungeon_delete, '/api/pvt/event/guild/ranking/battle/member/list': guild_battle_member_list, '/api/announcement/detail': announcement_detail, '/api/pvt/dungeon/another/treasure': _ii_treasure(HANDLERS['/api/pvt/dungeon/another/treasure']), '/api/pvt/dungeon/another/start': _ii_clear_first('post_pvt_dungeon_another_start')(HANDLERS['/api/pvt/dungeon/another/start']), '/api/pvt/dungeon/another/end': _ii_clear_first('post_pvt_dungeon_another_end')(HANDLERS['/api/pvt/dungeon/another/end'])})

def _gate_cost(lock_id):
    lk = mdx('m_dungeon_trigger_lock', 0).get((lock_id,))
    if not lk:
        return None
    out = []
    for rel in md('m_consumption_set_consumption_rel'):
        if rel[1] == lk[2]:
            rw = mdx('m_reward', 0).get((rel[2],))
            if rw:
                out.append((rw[1], rw[2], rel[3]))
    return out

def _gate_open(st, tx, dt, lock_id, entry_count, what):
    de = st.get('user_dungeon_entry_list', dt)
    cost = _gate_cost(lock_id)
    if de is None or cost is None:
        print(f'{what}: refused, {st.user_id} entry {de is not None}, gate {lock_id} known {cost is not None}')
        return
    d = _ii_load(de)
    m = d.setdefault('instant_item_map', {})
    for rtype, target, n in cost:
        have = m.get(str(target), 0) if rtype == RT_INSTANT else st.get('user_item_list', target).count if rtype == RT_ITEM and st.get('user_item_list', target) else 0
        if rtype not in (RT_INSTANT, RT_ITEM) or have < n:
            print(f'{what}: refused, gate {lock_id} costs type {rtype} {target} x{n}, {st.user_id} holds {have}')
            return
    for rtype, target, n in cost:
        if rtype == RT_INSTANT:
            m[str(target)] -= n
            u = d.setdefault('used_instant_item_map', {})
            u[str(target)] = u.get(str(target), 0) + n
            d.setdefault('used_instant_item_list', []).extend(({'instant_item_id': target} for _ in range(n)))
        else:
            add_item(st, tx, target, -n)
    if any((c[0] == RT_INSTANT for c in cost)):
        de.instant_item_info = _ii_dump(d, True)
        tx.upsert('user_dungeon_entry_list', de)
    row = st.get('user_dungeon_trigger_lock_list', lock_id) or st.new_row('user_dungeon_trigger_lock_list', dungeon_trigger_lock_id=lock_id)
    row.entry_count = entry_count or de.entry_count
    tx.upsert('user_dungeon_trigger_lock_list', row)
    print(f'{what}: {st.user_id} opened gate {lock_id}')

def dungeon_another_trigger_lock(st, req, ctx):
    q = req.post_pvt_dungeon_another_trigger_lock
    with st.transaction('dungeon/another/trigger/lock') as tx:
        _gate_open(st, tx, q.dungeon_type or P.Command_Enums_DungeonType_Another, q.dungeon_trigger_lock_id, q.entry_count, 'dungeon/another/trigger/lock')
    r = _resp(tx)
    r.post_pvt_dungeon_another_trigger_lock.SetInParent()
    return r

def dungeon_story_trigger_lock(st, req, ctx):
    q = req.post_pvt_dungeon_story_trigger_lock
    with st.transaction('dungeon/story/trigger/lock') as tx:
        _gate_open(st, tx, P.Command_Enums_DungeonType_Story, q.dungeon_trigger_lock_id, q.entry_count, 'dungeon/story/trigger/lock')
    r = _resp(tx)
    r.post_pvt_dungeon_story_trigger_lock.SetInParent()
    return r
HANDLERS.update({'/api/pvt/dungeon/another/trigger/lock': dungeon_another_trigger_lock, '/api/pvt/dungeon/story/trigger/lock': dungeon_story_trigger_lock})

def _bd_dump(d):
    return json.dumps(d, separators=(',', ':')) + '\n'

def _bd_load(ae):
    try:
        d = json.loads(ae.buff_debuff_info) if ae.buff_debuff_info else {}
    except ValueError:
        print(f'buff/debuff: unreadable buff_debuff_info {ae.buff_debuff_info!r}, starting empty')
        d = {}
    return d if isinstance(d, dict) else {}

def _bd_reply_row(r, row):
    lst = r.common.user.update.user_another_dungeon_entry_list
    for x in lst:
        if x.dungeon_type == row.dungeon_type:
            x.CopyFrom(row)
            return
    lst.add().CopyFrom(row)

def _bd_after_start(fn):

    def h(st, req, ctx):
        r = fn(st, req, ctx)
        q = req.post_pvt_dungeon_another_start
        ad = mdx('m_another_dungeon', 0).get((q.dungeon_id,))
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        if ad and ad[9] and (ae is not None):
            with st.transaction('dungeon/another/start: buff/debuff base') as tx:
                ae.buff_debuff_info = _bd_dump({'buff_debuff_group_id_list': [ad[9]]})
                tx.upsert('user_another_dungeon_entry_list', ae)
            _bd_reply_row(r, ae)
        return r
    return h

def _bd_after_battle_end(fn):

    def h(st, req, ctx):
        q = req.post_pvt_another_battle_end
        _II.dt = q.dungeon_type
        try:
            r = fn(st, req, ctx)
        finally:
            _II.dt = None
        ab = mdx('m_another_battle', 0).get((q.another_battle_id,))
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        if ab and ab[4] and (ae is not None) and (q.battle_result_type == P.Command_Enums_BattleResultType_Win):
            with st.transaction('another/battle/end: buff/debuff pick offered') as tx:
                ae.unselected_dungeon_buff_debuff_select_id = ab[4]
                tx.upsert('user_another_dungeon_entry_list', ae)
            _bd_reply_row(r, ae)
        return r
    return h

def dungeon_another_buff_debuff_select(st, req, ctx):
    q = req.post_pvt_dungeon_another_buff_debuff_select
    sel = mdx('m_dungeon_buff_debuff_select', 0).get((q.dungeon_buff_debuff_select_id,))
    with st.transaction('dungeon/another/buff/debuff/select') as tx:
        rows = list(st.tables.get('user_another_dungeon_entry_list', {}).values())
        ae = next((x for x in rows if x.unselected_dungeon_buff_debuff_select_id == q.dungeon_buff_debuff_select_id), None)
        if sel is None or ae is None or (not 0 <= q.select_idx <= 2) or (not sel[1 + q.select_idx]):
            print(f'dungeon/another/buff/debuff/select: refused ({st.user_id} select {q.dungeon_buff_debuff_select_id} idx {q.select_idx}, known {sel is not None}, run {ae is not None})')
        else:
            d = _bd_load(ae)
            d.setdefault('buff_debuff_group_id_list', []).append(sel[1 + q.select_idx])
            ae.buff_debuff_info = _bd_dump(d)
            ae.unselected_dungeon_buff_debuff_select_id = 0
            tx.upsert('user_another_dungeon_entry_list', ae)
    r = _resp(tx)
    r.post_pvt_dungeon_another_buff_debuff_select.SetInParent()
    return r
HANDLERS.update({'/api/pvt/dungeon/another/start': _bd_after_start(HANDLERS['/api/pvt/dungeon/another/start']), '/api/pvt/another/battle/end': _bd_after_battle_end(HANDLERS['/api/pvt/another/battle/end']), '/api/pvt/dungeon/another/buff/debuff/select': dungeon_another_buff_debuff_select})

# ---- per-fight scores (GitLab #61/#62): a won Another fight whose m_battle row has a BattleScoreId (col 18) gets Square's
# per-category score_result (engine/battle_score.py, fitted on 46 recorded fights) and win_score = its final score, and
# the run's six total_*_score fields grow by it (the game shows their sum as the Dungeon Score). Fights without a score id
# (Crisis normal fights) come back unscored, as recorded. Replaces the stand-in (final score = damage, every rank S).
# Wraps before the run-counter step below, so the boss rank it stores is the real one.
from engine import battle_score as _BS
_BS_TOTALS = (('death_count_score', 'total_death_count_score'), ('max_blow_damage_score', 'total_max_blow_damage_score'),
              ('taken_damage_score', 'total_taken_damage_score'), ('battle_time_score', 'total_battle_time_score'),
              ('weak_element_hit_count_score', 'total_weak_element_hit_count_score'), ('notes_break_count_score', 'total_notes_break_count_score'))

def _bs_top(dungeon_id):
    return max((r[2] for r in md('m_another_dungeon_rank') if r[0] == dungeon_id), default=0)

def _bs_after_battle_end(fn):
    def h(st, req, ctx):
        r = fn(st, req, ctx)
        q = req.post_pvt_another_battle_end
        body = r.post_pvt_another_battle_end
        ab = mdx('m_another_battle', 0).get((q.another_battle_id,))
        win = q.battle_result_type == P.Command_Enums_BattleResultType_Win
        bt = mdx('m_battle', 0).get((_another_bid(q.another_battle_id, q.boss_enhance_stage),)) if ab else None
        res = _BS.score(q.battle_input, bt[18] if bt else 0, _bs_top(ab[1])) if (ab and win) else None
        body.ClearField('score_result')
        # score_result stays present (all zero, rank None) on an unscored fight or a loss: the client reads it on the
        # result screen and hangs there without it (Crisis normal fights soft-locked after winning)
        body.score_result.SetInParent()
        if res is None:
            body.win_score = 0
            return r
        for k, v in res.items():
            setattr(body.score_result, k, v)
        body.win_score = res['final_score']
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        if ae is not None:
            with st.transaction('another/battle/end: run score') as tx:
                for k, t in _BS_TOTALS:
                    setattr(ae, t, getattr(ae, t) + res[k])
                tx.upsert('user_another_dungeon_entry_list', ae)
            _bd_reply_row(r, ae)
        return r
    return h
HANDLERS['/api/pvt/another/battle/end'] = _bs_after_battle_end(HANDLERS['/api/pvt/another/battle/end'])

def _cr_after_battle_end(fn):

    def h(st, req, ctx):
        r = fn(st, req, ctx)
        q = req.post_pvt_another_battle_end
        ab = mdx('m_another_battle', 0).get((q.another_battle_id,))
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        if not ab or ae is None or q.battle_result_type != P.Command_Enums_BattleResultType_Win:
            return r
        ad = mdx('m_another_dungeon', 0).get((ab[1],))
        bi = q.battle_input
        bit = 1 << ab[7]
        with st.transaction('another/battle/end: run counters') as tx:
            ae.total_atb_cost += bi.total_atb_cost
            ae.another_battle_idx_flag |= bit
            ae.entry_another_battle_idx_flag |= bit
            ae.max_battle_time_sec = max(ae.max_battle_time_sec, bi.total_battle_msec // 1000)
            ae.max_blow_damage = max(ae.max_blow_damage, bi.max_blow_damage)
            if ab[5] in (2, 3):
                ae.total_boss_count += 1
                ae.boss_enhance_stage = min(ae.boss_enhance_stage + 1, ad[5] if ad else ae.boss_enhance_stage + 1)
                rank = r.post_pvt_another_battle_end.score_result.final_score_battle_rank_type
                ae.min_battle_rank_type = rank
                if rank == RANK_S:
                    ae.rank_s_idx_flag |= bit
            else:
                ae.min_battle_rank_type = 0
            tx.upsert('user_another_dungeon_entry_list', ae)
        _bd_reply_row(r, ae)
        return r
    return h
HANDLERS['/api/pvt/another/battle/end'] = _cr_after_battle_end(HANDLERS['/api/pvt/another/battle/end'])

def _fight_results(entry):
    """Won fights of the current Another run, as stored in battle_result_info ({} if empty or unreadable)."""
    if not entry.battle_result_info:
        return {}
    try:
        stored = json.loads(entry.battle_result_info).get('battle_result_map')
    except (ValueError, AttributeError):
        print(f'battle_result_info for {entry.dungeon_type} could not be read; rebuilding it')
        return {}
    return stored if isinstance(stored, dict) else {}

def _with_fight_result(fn):
    # The client looks each won fight up in battle_result_info before offering the post-fight choice (trance
    # abilities in Dungeon Ranking, buffs in Crisis); with no entry it skips the choice. Official servers kept one
    # entry per won fight for the run, keyed by another_battle_id in ascending order, compact JSON plus a newline.
    def handler(st, req, ctx):
        r = fn(st, req, ctx)
        q = req.post_pvt_another_battle_end
        if not q.another_battle_id or q.battle_result_type != P.Command_Enums_BattleResultType_Win:
            return r
        entry = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        if entry is None:
            return r
        results = _fight_results(entry)
        results[str(q.another_battle_id)] = {'score': int(r.post_pvt_another_battle_end.score_result.final_score), 'battle_result_type': 1}
        ordered = dict(sorted(results.items(), key=lambda kv: int(kv[0])))
        with st.transaction('another/battle/end: fight result') as tx:
            entry.battle_result_info = json.dumps({'battle_result_map': ordered}, separators=(',', ':')) + '\n'
            tx.upsert('user_another_dungeon_entry_list', entry)
        _bd_reply_row(r, entry)
        return r
    return handler
HANDLERS['/api/pvt/another/battle/end'] = _with_fight_result(HANDLERS['/api/pvt/another/battle/end'])

def _libra_row(stage_id):
    s = _stage(stage_id)
    lid = s[18] if s and len(s) > 18 else 0
    if not lid:
        o = _stage_copies().get(stage_id)
        s = _stage(o) if o else None
        lid = s[18] if s and len(s) > 18 else 0
    return mdx('m_libra', 0).get((lid,)) if lid else None

def _libra_pool(lib):
    pre = lib[0] // 1000
    return [r[0] for r in md('m_libra_card') if r[0] // 1000 == pre]

def libra_card_draw(st, req, ctx):
    q = req.post_pvt_libra_card_draw
    lib = _libra_row(q.event_battle_id)
    with st.transaction('libra/card/draw') as tx:
        if lib:
            row = st.get('user_event_solo_battle_list', q.event_battle_id) or st.new_row('user_event_solo_battle_list', event_solo_battle_id=q.event_battle_id)
            if row.libra_card_id0:
                row.libra_card_lot_reset_count += 1
            pool = _libra_pool(lib)
            n = max(0, min(int(lib[2] or 0), 3, len(pool)))
            got = random.sample(pool, n) + [0, 0, 0]
            row.libra_card_id0, row.libra_card_id1, row.libra_card_id2 = (got[0], got[1], got[2])
            row.selected_libra_card_id = 0
            tx.upsert('user_event_solo_battle_list', row)
        else:
            print(f'libra/card/draw: stage {q.event_battle_id} has no Libra')
    r = _resp(tx, 'post_pvt_libra_card_draw')
    r.post_pvt_libra_card_draw.SetInParent()
    return r

def libra_card_select(st, req, ctx):
    q = req.post_pvt_libra_card_select
    with st.transaction('libra/card/select') as tx:
        row = st.get('user_event_solo_battle_list', q.event_battle_id)
        if row and q.libra_card_id in (row.libra_card_id0, row.libra_card_id1, row.libra_card_id2) and q.libra_card_id:
            row.selected_libra_card_id = q.libra_card_id
            tx.upsert('user_event_solo_battle_list', row)
        else:
            print(f'libra/card/select: card {q.libra_card_id} was not drawn for stage {q.event_battle_id}')
    r = _resp(tx, 'post_pvt_libra_card_select')
    r.post_pvt_libra_card_select.SetInParent()
    return r
HANDLERS['/api/pvt/libra/card/draw'] = libra_card_draw
HANDLERS['/api/pvt/libra/card/select'] = libra_card_select
HANDLERS['/api/pvt/dungeon/another/party/separate'] = _empty('post_pvt_dungeon_another_party_separate')



# ---- one player, no battle server -----------------------------------------------------------------------------------
# Co-op needs other players and the battle server. The screens that list or wait on them answer empty; the calls that
# would start a co-op fight are refused by the service (COOP_REFUSED) with an error the game shows.
import base64 as _b64

COOP_REFUSED = (
    "/api/pvt/room/matching/start", "/api/match/room/matching/restart", "/api/match/room/matching/status",
    "/api/pvt/room/private/create", "/api/match/room/private/search", "/api/pvt/room/private/enter",
    "/api/pvt/room/private/invite/send", "/api/pvt/room/private/invite/guild/send", "/api/pvt/room/private/invite/approve",
    "/api/pvt/event/multi/battle/start", "/api/pvt/area/multi/battle/start",
    "/api/pvt/event/multi/battle/continue", "/api/pvt/area/multi/battle/continue",
    "/api/pvt/event/multi/battle/end", "/api/pvt/area/multi/battle/end",
)
_MULTI_TYPES = {P.Command_Enums_BattleType.Value("Command_Enums_BattleType_EventMulti"),
                P.Command_Enums_BattleType.Value("Command_Enums_BattleType_AreaMulti")}
_PROGRESS_START = P.Command_Enums_BattleProgressType.Value("Command_Enums_BattleProgressType_Start")


def match_session_local(st, req, ctx):
    body = P.Command_ApiNetwork_Api_PostPvtMatchSessionResponse()
    body.match_token = _b64.urlsafe_b64encode(os.urandom(96)).decode().rstrip("=")
    with st.transaction("match/session") as tx: pass
    r = _resp(tx, "post_pvt_match_session", body); r.post_pvt_match_session.SetInParent()
    return r


def restart_info_local(st, req, ctx):
    with st.transaction("battle/restart/info") as tx:
        body = P.Command_ApiNetwork_Api_PostPvtBattleRestartInfoResponse()
        body.restart_type = P.Command_Enums_MultiBattleRestartType_None
        e = st.one("user_battle_entry_list")
        if e and e.battle_type in _MULTI_TYPES and e.battle_progress_type == _PROGRESS_START:
            body.restart_type = P.Command_Enums_MultiBattleRestartType_DeleteBattle
            _end_entry(st, tx, e.battle_id)
            s = st.one("user_status_list")
            if s: stamina_settle(s); tx.touch("user_status_list", s)
    r = _resp(tx, "post_pvt_battle_restart_info", body); r.post_pvt_battle_restart_info.SetInParent()
    return r


for _p, _f in (("/api/pvt/match/session", match_session_local),
               ("/api/match/room/matching/stats", _empty("post_match_room_matching_stats")),
               ("/api/pvt/room/matching/cancel", _empty("post_pvt_room_matching_cancel")),
               ("/api/pvt/room/private/invite/target/list", _empty("post_pvt_room_private_invite_target_list")),
               ("/api/pvt/room/private/invite/receive/list", _empty("post_pvt_room_private_invite_receive_list")),
               ("/api/pvt/battle/restart/info", restart_info_local)):
    HANDLERS[_p] = _body_wrap(_me_wrap(_f, _p))
for _p in COOP_REFUSED: HANDLERS.pop(_p, None)


# ---- daily counts (stage clears, premium quests, Skip Battle) -----------------------------------------------------
_RESET_MS = {3: 3600000, 4: DAY_MS, 5: 7 * DAY_MS}   # m_reset CycleType Hour / Day / Week


def reset_period(reset_id, t):
    """Which period of m_reset row `reset_id` the time t falls in; None = the count never resets (no row, or id 0:
    Square's '[1 time only]' stages). Day = the game day (m_reset 1, 4 and 20 all roll at the same hour)."""
    r = mdx("m_reset", 0).get((reset_id,)) if reset_id else None
    if r and r[2] == 6:    # Month: whole months since the row's start, counted on its day and time of day (UTC)
        a, b = _utc(r[1]), _utc(t)
        m = (b.year - a.year) * 12 + b.month - a.month
        if (b.day, b.time()) < (a.day, a.time()):
            m -= 1
        return m // max(1, r[3])
    if not r or r[2] not in _RESET_MS:
        return None
    return (t - r[1]) // (_RESET_MS[r[2]] * max(1, r[3]))


def _utc(ms):    # datetime arithmetic, since time.gmtime refuses times before 1970 on Windows
    import datetime
    return datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)


def _daily_first(stage, row):
    """True on the first win of a reset period of a stage whose only reward is its first-clear set: limited
    (ChallengeCountMax, col 11) on a repeating reset (ChallengeResetId, col 12), no rank drops (BattleRankGroupId, col 6,
    is 1) and a FirstRewardSetId (col 7). Five of Square's stages: the three "[1/Day] Golden Bomb" stages (3008012,
    3010012, 3019007) and the two Fury of Lightning & Wind dailies (12001301, 12001401). Call after _daily_count."""
    return bool(stage and stage[11] > 0 and stage[6] == 1 and stage[7]
                and reset_period(stage[12], row.last_challenge_reset_datetime) is not None
                and row.challenge_count == 1)


def _daily_count(row, count, stamp, now, reset_id=1):
    """Add one to a count the game resets on m_reset `reset_id` (default: each game day), starting again from 0 on the
    first one of a new period. The game reads the count against its stamp, so the stamp is set every time."""
    p = reset_period(reset_id, now)
    if p is not None and reset_period(reset_id, getattr(row, stamp)) < p:
        setattr(row, count, 0)
    setattr(row, count, getattr(row, count) + 1)
    setattr(row, stamp, now)


def _area_category_win(st, tx, area_id, now):
    """Premium battles share a daily count per m_solo_area_group_category row (id 99995500001 for category 6),
    kept in the account's one user_solo_area_group_category_list row."""
    T = "user_solo_area_group_category_list"
    cats = {g[1]: g[0] for g in md("m_solo_area_group_category")}
    rows = st.tables[T]
    for k, r in list(rows.items()):    # rows an earlier build keyed by the area category number instead
        if r.solo_area_group_category_id not in cats.values():
            rows.pop(k)
            getattr(tx.dl, T).append(r)
    cid = cats.get(_area_category(area_id))
    if not cid:
        return
    gc = next((r for r in rows.values() if r.solo_area_group_category_id == cid), None) or st.new_row(T, solo_area_group_category_id=cid)
    _daily_count(gc, "reset_win_count", "last_win_reset_datetime", now)
    tx.upsert(T, gc)


# ---- Escalation Challenge missions against the levels actually cleared ---------------------------------------------
def _panel_battle_missions_heal(st):
    """Mission panels' ProgressBattleWin missions ("Clear "Lightning 3" 1 time(s)", m_mission col 5 = the stage's
    battle) count wins made while a panel is open. Panels were closed until they reopened, so wins made before then
    never counted (#108): each is caught up to the wins of the event stages that fight that battle."""
    panel_groups = {r[2] for r in md('m_mission_panel_mission_group_rel')}
    wins = {}
    for row in st.tables.get('user_event_solo_battle_list', {}).values():
        stage = _stage(row.event_solo_battle_id)
        if stage and row.total_win_count:
            wins[stage[5]] = wins.get(stage[5], 0) + row.total_win_count
    t = now_ms(); fix = []
    for m in _mt_index().get(MTYPE['ProgressBattleWin'], []):
        if m[3] == 3 or not m[5] or _set_group(m[1]) not in panel_groups or not _group_open(_set_group(m[1]), t):
            continue
        row = st.get('user_mission_list', m[0])
        if wins.get(m[5], 0) > (row.progress_count if row else 0):
            fix.append((m, row))
    if not fix:
        return 0
    with st.transaction('missions: panel stage clears') as tx:
        for m, row in fix:
            row = row or st.new_row('user_mission_list', mission_id=m[0])
            old, row.progress_count = row.progress_count, wins[m[5]]
            _announce_step(st, tx, m, row, old, t)
            tx.upsert('user_mission_list', row)
    print(f'panel battle missions heal: {st.user_id} {len(fix)} mission(s) caught up from stage wins')
    return len(fix)


def _escalation_missions_heal(st):
    """Met ones marked done; ones an earlier build marked done for any clear taken back, unless already claimed."""
    best = {}
    for r in st.tables.get("user_boss_challenge_list", {}).values():
        best[r.boss_challenge_id] = max(best.get(r.boss_challenge_id, 0), r.clear_level)
    top = max(best.values(), default=0)
    t = now_ms(); fix = []
    for m in _mt_index().get(MTYPE["ProgressBossChallengeClear"], []):
        if m[3] == 3 or not _group_open(_set_group(m[1]), t):
            continue
        met = (best.get(m[5], 0) if m[5] else top) >= max(m[8], 1)
        row = st.get("user_mission_list", m[0]); have = row.progress_count if row else 0
        if met and have < 1:
            fix.append((m[0], row, 1))
        elif not met and have and row.received_progress_count == 0:
            fix.append((m[0], row, 0))
    if not fix:
        return 0
    with st.transaction("missions: escalation clears by level") as tx:
        for mid, row, n in fix:
            row = row or st.new_row("user_mission_list", mission_id=mid)
            row.progress_count = n; row.notified_progress_count = min(row.notified_progress_count, n)
            tx.upsert("user_mission_list", row)
    print(f"escalation missions heal: {st.user_id} {len(fix)} mission(s) set from cleared levels")
    return len(fix)


# ---- Score Battle / High-Risk Battle missions ("Clear Score Battle 1 with Rank D or higher", "... a score of 3,000") --
# Nothing counted them. Both name the stage (m_mission col 5 = m_event_solo_battle id) and a bar (col 8: a
# BattleRankType, or a score). The rank is the one the server reports (always S here); the score is the fight's total
# damage, the number the server records as the stage's high score.
def _score_battle_missions(st, tx, stage_id, rank, score):
    mission_event(st, tx, "ProgressEventScoreBattleRankClear", 1, stage_id, reached=rank)
    mission_event(st, tx, "ProgressEventScoreBattleScoreClear", 1, stage_id, reached=score)


_TOWER_OF_STAGE = {}

def tower_floor_of_stage():
    """{event stage id: (tower id, floor)} for the event towers whose towers are in the served tables
    (engine/cards.py tower_plan: floor n = the event's n-th stage)."""
    if 'ok' not in _TOWER_OF_STAGE:
        from engine import cards
        try:
            served = {row[0] for row in md('m_tower')}
            for tid, _, stages in cards.tower_plan(_offline_recipe(), md):
                if tid in served:
                    for seq, stage in enumerate(stages, 1):
                        _TOWER_OF_STAGE[stage[0]] = (tid, seq)
        except (ValueError, KeyError, TypeError) as e:
            print(f'event towers: no stage map ({e!r})')
        _TOWER_OF_STAGE['ok'] = True
    return _TOWER_OF_STAGE

def _event_tower_heal(st):
    """An event tower's floors are its event's stages: floors already won as event stages count as cleared, so a
    player never refights or is paid twice for them. Raises clear_floor_seq only, never lowers it."""
    floor_of = tower_floor_of_stage()
    reached = {}
    for row in st.tables['user_event_solo_battle_list'].values():
        hit = floor_of.get(row.event_solo_battle_id) if row.total_win_count > 0 else None
        if hit and hit[1] > reached.get(hit[0], 0):
            reached[hit[0]] = hit[1]
    behind = [(tid, seq) for tid, seq in sorted(reached.items())
              if (st.get('user_tower_list', tid).clear_floor_seq if st.get('user_tower_list', tid) else 0) < seq]
    if not behind:
        return 0
    with st.transaction('event towers: floors won as event stages') as tx:
        for tid, seq in behind:
            row = st.get('user_tower_list', tid) or st.new_row('user_tower_list', tower_id=tid)
            row.clear_floor_seq = seq
            tx.upsert('user_tower_list', row)
    return len(behind)

def _weapon_upgrade_type_heal(st):
    """Weapons obtained before the gacha fix have weapon_upgrade_type 0; Square's are Rank from obtain. The client
    reads "not Rank" as "past Overboost" (Rarity tab: "Unable to overboost+ any further"). Type 0 -> Rank, count kept."""
    bad = [r for r in st['user_weapon_list'].values() if r.weapon_upgrade_type == 0]
    if not bad:
        return 0
    rank = P.Command_Enums_WeaponUpgradeType.Value('Command_Enums_WeaponUpgradeType_Rank')
    with st.transaction('weapon upgrade type heal') as tx:
        for r in bad:
            r.weapon_upgrade_type = rank
            tx.upsert('user_weapon_list', r)
    return len(bad)

def _weapon_evolve_type_heal(st):
    """#70: weapons whose Customization was unlocked before the first unlock went into use have options unlocked
    (evolve_flags0) and none in use (weapon_evolve_type 0). The lowest unlocked option goes into use."""
    bad = [r for r in st['user_weapon_list'].values() if r.evolve_flags0 >> 1 and not r.weapon_evolve_type]
    if not bad:
        return 0
    with st.transaction('weapon customization heal') as tx:
        for r in bad:
            r.weapon_evolve_type = next(t for t in range(1, 64) if r.evolve_flags0 >> t & 1)
            tx.upsert('user_weapon_list', r)
    return len(bad)

def _score_battle_missions_heal(st):
    """Stages already won before the missions were counted: each mission whose stage was won (at the rank the server
    gave: S) or whose stage's high score reaches its bar is marked done."""
    t = now_ms(); fix = []
    for mt in ("ProgressEventScoreBattleRankClear", "ProgressEventScoreBattleScoreClear"):
        for m in _mt_index().get(MTYPE[mt], []):
            if not m[5] or m[3] == 3 or not _group_open(_set_group(m[1]), t):
                continue
            won = st.get("user_event_solo_battle_list", m[5])
            if not won or not won.total_win_count:
                continue
            got = (won.high_rank_type or RANK_S) if mt.endswith("RankClear") else won.high_score
            row = st.get("user_mission_list", m[0])
            if got >= m[8] and (row is None or row.progress_count < 1):
                fix.append((m[0], row))
    if not fix:
        return 0
    with st.transaction("missions: score battles already won") as tx:
        for mid, row in fix:
            row = row or st.new_row("user_mission_list", mission_id=mid)
            row.progress_count = 1
            tx.upsert("user_mission_list", row)
    print(f"score battle missions heal: {st.user_id} {len(fix)} mission(s) caught up")
    return len(fix)

def _event_battle_episodes_heal(st):
    """Battle episodes and present levels from wins before either was counted: a won stage that stands for an episode
    clears it (so the next stage opens), and each won stage whose first-clear rewards raise a present level counts."""
    wins = [k[0] for k, v in st.tables['user_event_solo_battle_list'].items() if v.total_win_count > 0]
    eps = [e for e in (_battle_episode(s) for s in wins) if e and not (st.get('user_event_scenario_episode_list', e) or st.new_row('user_event_scenario_episode_list')).clear_count]
    want = {}
    for s in wins:
        stage = _stage(s)
        for rel in _rs_index()['rel'].get(stage[7], []) if stage and stage[7] else ():
            rw = mdx('m_reward', 0).get((rel[2],))
            if rw and rw[1] == RT_EVENT_IDLING_LEVEL:
                want[rw[2]] = want.get(rw[2], 1) + rel[4]
    lv = {c: n for c, n in want.items() if (st.get('user_event_idling_collect_list', c) or st.new_row('user_event_idling_collect_list', level=1)).level < min(n, max((r[1] for r in _idling_levels(c)), default=n))}
    if not eps and not lv:
        return 0
    with st.transaction('events: battle episodes and present levels already won') as tx:
        for e in eps:
            _episode_clear(st, tx, e, None)
        for c, n in lv.items():
            row = _idling_row(st, c, now_ms())
            row.level = min(n, max((r[1] for r in _idling_levels(c)), default=n))
            tx.upsert('user_event_idling_collect_list', row)
    print(f'event battle episodes heal: {st.user_id} {len(eps)} episode(s) cleared, {len(lv)} present level(s) set')
    return len(eps) + len(lv)

def _event_story_missions_heal(st):
    """Event episodes watched before their missions were counted ("Watch the prologue of ..."): each mission whose
    episode was already cleared is marked done, as the episode's result now does."""
    seen = {k[0] for k, v in st.tables["user_event_scenario_episode_list"].items() if v.clear_count > 0}
    t = now_ms(); fix = []
    for m in _mt_index().get(MTYPE["SetEventStoryRead"], []):
        if m[3] != 3 or m[5] not in seen or not _group_open(_set_group(m[1]), t) or not _mission_window_ok(m, t, False):
            continue
        s = st.get("user_mission_set_list", m[1])
        if not (s and _set_flag(s, "achieved", m[2])):
            fix.append(m)
    if not fix:
        return 0
    with st.transaction("missions: event episodes already watched") as tx:
        for m in fix:
            _set_mission_achieve(st, tx, m, t)
    print(f"event story missions heal: {st.user_id} {len(fix)} mission(s) caught up")
    return len(fix)


# A guild carried over from an imported save keeps the role the player had on Square (#27): a SubMaster or member
# whose Master never came across is left in a guild nobody can run - edit role, bonus level up and kick all need a
# Master. When no account here is the Master, the longest-standing member here takes it (earliest join, then lowest
# user id), as Square hands the guild on when its Master leaves.
def _guild_master_heal(st):
    gid = _my_guild(st, None)
    own = _gm_own(st, gid) if gid else None
    if own is None or own.guild_role_type == G_MASTER:
        return False
    members = _guild_members_here(gid)
    members[str(st.user_id)] = own
    if any(m.guild_role_type == G_MASTER for m in members.values()):
        return False
    heir = min(members, key=lambda u: (members[u].join_datetime or 0, int(u) if u.isdigit() else 0))
    if heir != str(st.user_id):
        return False
    with st.transaction('guild: no master here') as tx:
        old = own.guild_role_type
        own.guild_role_type = G_MASTER
        tx.upsert('shared_guild_member_list', own)
    print(f'guild master heal: {st.user_id} role {old} -> Master in guild {gid} ({len(members)} member(s) here, none Master)')
    return True


def _with_master_heal(view):
    def h(st, req, ctx):
        try:
            _guild_master_heal(st)
        except Exception as e:
            print(f'guild master heal: {st.user_id}: {e!r}')
        return view(st, req, ctx)
    return h
for _p in ('/api/pvt/guild/top', '/api/pvt/guild/get/self', '/api/pvt/guild/member/list'):
    HANDLERS[_p] = _with_master_heal(HANDLERS[_p])


# ---- Crisis dungeon end: the run's score, rank and rank rewards (GitLab #61) ----
# dungeon/another/end never ranked a Crisis run: high_rank_type stayed 0, every run showed F with no rewards, and the
# "Clear X with Rank S+" missions (ProgressAnotherRankClear) and the rank-count ones never moved.
# Recorded on Square (7 Crisis runs: 5 cleared, 2 given up), the reply's final_score is:
#   given up:  the run's six per-fight score totals (user_another_dungeon_entry total_*_score), nothing else  (2 of 2 exact)
#   cleared:   (those totals + total_atb_cost_score + remained_instant_item_score + dungeon_clear_score)
#              x (1000 + boss_score_coefficient) / 1000, floored                                              (5 of 5 exact)
#   dungeon_clear_score 10000 in all five; boss_score_coefficient 800 with 4 bosses beaten, 700 with 3.
# Not worked out from the recordings, so estimated (recorded clears land within -6%..+2%, all at Square's rank):
#   total_atb_cost_score 10000 x m, m = the dungeon's top rank threshold / 110000; remained_instant_item_score 400 per
#   item x its m_instant_item col 6 x m; boss_score_coefficient 400 + 100 per boss beaten.
# A run is cleared when its last boss (m_another_battle col 5 = 3) was won this run. Every run is ranked on its score
# (given-up runs too, as recorded): high score / rank, first reward per newly reached rank, lap reward of the rank reached.
# Only Crisis dungeons (m_another_dungeon col 6 = 1; Criterion dungeons have no rank rows).
_CRK_T1 = 110000
_CRK_TOTALS = ('total_death_count_score', 'total_max_blow_damage_score', 'total_taken_damage_score',
               'total_battle_time_score', 'total_weak_element_hit_count_score', 'total_notes_break_count_score')
_CRK = {}

def _crk_rank_rows(did):
    if 'rr' not in _CRK:
        rr = {}
        for r in md('m_another_dungeon_rank'):
            rr.setdefault(r[0], []).append(r)
        _CRK['rr'] = {k: sorted(v, key=lambda r: r[2]) for k, v in rr.items()}
    return _CRK['rr'].get(did, [])

def _crk_last_boss(did):
    if 'lb' not in _CRK:
        _CRK['lb'] = {b[1]: b[7] for b in md('m_another_battle') if b[5] == 3}
    return _CRK['lb'].get(did)

# Dungeon Ranking (#71): some post-fight picks trade stat penalties for score. Their group holds a Score effect
# (m_dungeon_buff_debuff col 1 = 17) whose m_dungeon_buff_debuff_group_rel Value is a permil score bonus (4110301032:
# -50% physical attack, -25% magic attack, -30% both defences, +400). Only Dungeon Ranking dungeons have them. The game
# totals them itself (dungeon/another/end's total_score_buff_debuff_coefficient) and the reply has
# score_buff_debuff_coefficient for it, but nothing counted them: a cleared run lost the bonus it paid for. Taken here
# from the run's own picks (buff_debuff_info) and added to the boss coefficient; how Square combined the two was never
# recorded, so that part is a stand-in like the rest of this formula.
_CRK_SCORE_EFFECT = 17

def _crk_bd_coef(ae):
    """-> the Score permil of every buff/debuff group taken this run."""
    if 'bd' not in _CRK:
        kinds = {r[0]: r[1] for r in md('m_dungeon_buff_debuff')}
        bd = {}
        for g, e, v in md('m_dungeon_buff_debuff_group_rel'):
            if kinds.get(e) == _CRK_SCORE_EFFECT:
                bd[g] = bd.get(g, 0) + v
        _CRK['bd'] = bd
    return sum(_CRK['bd'].get(g, 0) for g in _bd_load(ae).get('buff_debuff_group_id_list', []))

def _crk_score(st, did, dt):
    """-> (final score, reply fields) for the run in the entry rows of dungeon type dt. Read before the inner handler."""
    ae = st.get('user_another_dungeon_entry_list', dt)
    if ae is None:
        return 0, {}
    fights = sum(getattr(ae, k) for k in _CRK_TOTALS)
    out = {'death_count_score': ae.total_death_count_score, 'max_blow_damage_score': ae.total_max_blow_damage_score,
           'total_taken_damage_score': ae.total_taken_damage_score, 'total_battle_time_score': ae.total_battle_time_score,
           'weak_element_hit_count_score': ae.total_weak_element_hit_count_score,
           'notes_break_count_score': ae.total_notes_break_count_score}
    lb = _crk_last_boss(did)
    if lb is None or not (ae.another_battle_idx_flag >> lb) & 1:
        return fights, out
    rows = _crk_rank_rows(did)
    m = max(1.0, (rows[-1][2] if rows else _CRK_T1) / _CRK_T1)
    de = st.get('user_dungeon_entry_list', dt)
    bag = {}
    try:
        d = json.loads(de.instant_item_info) if de is not None and de.instant_item_info else {}
        bag = d.get('instant_item_map', {}) if isinstance(d, dict) else {}
    except ValueError:
        print(f'crisis rank: unreadable instant_item_info {de.instant_item_info!r}, no item score')
    mi = mdx('m_instant_item', 0)
    grade = sum(int(n) * ((mi.get((int(k),)) or [0] * 7)[6] or 0) for k, n in bag.items() if str(k).isdigit() and int(n) > 0)
    atb, items, clear = round(10000 * m), round(400 * grade * m), 10000
    coef = 400 + 100 * ae.total_boss_count
    bd = _crk_bd_coef(ae)
    out.update(total_atb_cost_score=atb, remained_instant_item_score=items, dungeon_clear_score=clear, boss_score_coefficient=coef,
               score_buff_debuff_coefficient=bd)
    return (fights + atb + items + clear) * (1000 + coef + bd) // 1000, out

def _crk_merge(dst, src):
    for f, rows in src.common.user.update.ListFields():
        lst = getattr(dst.common.user.update, f.name)
        first = {}
        for i, x in enumerate(lst):
            first.setdefault(_ES.row_key(f.name, x), i)
        for row in rows:
            i = first.get(_ES.row_key(f.name, row))
            if i is not None:
                lst[i].CopyFrom(row)
            else:
                first[_ES.row_key(f.name, row)] = len(lst)
                lst.add().CopyFrom(row)

def _crk_end(fn):
    def h(st, req, ctx):
        q = req.post_pvt_dungeon_another_end
        ad = mdx('m_another_dungeon', 0).get((q.dungeon_id,))
        rows = _crk_rank_rows(q.dungeon_id)
        if not ad or ad[6] != 1 or not rows:
            return fn(st, req, ctx)
        score, parts = _crk_score(st, q.dungeon_id, q.dungeon_type)
        prev = st.get('user_another_dungeon_list', q.dungeon_id)
        prev_rank = int(prev.high_rank_type) if prev is not None else 0
        r = fn(st, req, ctx)
        body = r.post_pvt_dungeon_another_end
        rk = None
        for x in rows:
            if score >= x[2]:
                rk = x
        rank = rk[1] if rk else 0
        with st.transaction('dungeon/another/end: crisis rank') as tx:
            row = st.get('user_another_dungeon_list', q.dungeon_id) or st.new_row('user_another_dungeon_list', another_dungeon_id=q.dungeon_id)
            if score > row.high_score:
                row.high_score = score
                row.last_update_score_datetime = now_ms()
            if rank > row.high_rank_type:
                row.high_rank_type = rank
            tx.upsert('user_another_dungeon_list', row)
            for x in rows:
                if prev_rank < x[1] <= rank and x[3]:
                    fi = body.first_reward_infos.add()
                    fi.dungeon_rank_type = x[1]
                    grant_reward_set(st, tx, x[3], 1, fi.reward_info)
            if rk and rk[4]:
                grant_reward_set(st, tx, rk[4], 1, body.lap_reward_infos)
            for k, v in parts.items():
                setattr(body, k, v)
            body.final_score = score
            body.final_score_dungeon_rank_type = rank
            mission_event(st, tx, 'ProgressAnotherRankClear', 1, q.dungeon_id, reached=int(row.high_rank_type))
        _crk_merge(r, _resp(tx))
        return r
    return h
HANDLERS['/api/pvt/dungeon/another/end'] = _me_wrap(_crk_end(HANDLERS['/api/pvt/dungeon/another/end']), '/api/pvt/dungeon/another/end')


# ---- Criterion dungeons: dungeon missions and run score (GitLab #62) ----
# Each Criterion dungeon (m_another_dungeon col 6 = 0, ids 3xxxxxx) lists three dungeon missions in cols 12-14
# (m_dungeon_mission: id, kind, target battle, amount, element, reward set, text). Official saves keep one achieved flag
# per mission on user_another_dungeon (achieved_dungeon_mission_id0..2). Nothing here checked them, so missions never
# cleared and paid nothing. Each fight now also records what the missions need in the run's entry row, in the official
# layout: total deaths, total weakness damage, and dungeon_mission_info {element_type_total_damage_map,
# battle_time_sec_map, battle_taken_damage_map} keyed by element / fight index (m_another_battle col 7), plus
# rank_s_idx_flag for every fight won at S. A cleared run (last boss won) is then checked at dungeon/another/end;
# each newly met mission sets its flag and pays its reward once, listed in mission_clear_reward_infos.
# Mission kinds (col 1), from the mission texts; "Symbol enemies" are the dungeon's col-5 = 1 fights:
#   1 no character KO'd   4 one hit of col 3+   5 every Symbol beaten, their fights within col 3 seconds in total
#   6 every Symbol beaten at S   8 col 3+ damage of element col 4   9 col 3+ weakness damage   10 every Symbol beaten
#   11 every rare chest (m_dungeon_treasure col 4 = 1) opened   12 fight col 2 won
#   13 every Symbol beaten, under col 3 damage taken in their fights in total
# Score: Criterion dungeons have no rank table and official saves kept high score 0 for them, so there is nothing to
# fit. The reply's final score is an approximation: the run's six per-fight score totals, plus dungeon_clear_score
# 10000 for a clear (the Crisis formula without its boss coefficient, ATB or item parts). It is shown on the clear
# screen and kept as the dungeon's high score.
_CM = {}
_CM_SYMBOL = 1
_CM_CLEAR = 10000

def _cm_criterion(ad):
    return bool(ad) and ad[6] == 0 and ad[0] // 1000000 == 3

def _cm_missions():
    if 'm' not in _CM:
        _CM['m'] = mdx('m_dungeon_mission', 0)
    return _CM['m']

def _cm_battles(did):
    if 'b' not in _CM:
        b = {}
        for r in md('m_another_battle'):
            b.setdefault(r[1], []).append(r)
        _CM['b'] = b
    return _CM['b'].get(did, [])

def _cm_info(ae):
    try:
        d = json.loads(ae.dungeon_mission_info) if ae.dungeon_mission_info else {}
    except ValueError:
        print(f'dungeon_mission_info for {ae.dungeon_type} could not be read; starting it again')
        d = {}
    if not isinstance(d, dict):
        d = {}
    for k in ('element_type_total_damage_map', 'battle_time_sec_map', 'battle_taken_damage_map'):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d

def _cm_after_battle_end(fn):
    def h(st, req, ctx):
        r = fn(st, req, ctx)
        q = req.post_pvt_another_battle_end
        ab = mdx('m_another_battle', 0).get((q.another_battle_id,))
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        if not ab or ae is None or q.battle_result_type != P.Command_Enums_BattleResultType_Win:
            return r
        bi = q.battle_input
        idx = str(ab[7])
        d = _cm_info(ae)
        el = d['element_type_total_damage_map']
        for k in range(10):
            el.setdefault(str(k), 0)
        for k, v in q.element_type_total_damage_map.items():
            el[str(k)] = el.get(str(k), 0) + int(v)
        d['battle_time_sec_map'][idx] = bi.total_battle_msec // 1000
        d['battle_taken_damage_map'][idx] = bi.total_taken_damage
        with st.transaction('another/battle/end: dungeon mission data') as tx:
            ae.total_death_count += bi.death_count
            ae.total_weak_element_damage += q.total_weak_element_damage
            ae.dungeon_mission_info = json.dumps(d, separators=(',', ':')) + '\n'
            if r.post_pvt_another_battle_end.score_result.final_score_battle_rank_type == RANK_S:
                ae.rank_s_idx_flag |= 1 << ab[7]
            tx.upsert('user_another_dungeon_entry_list', ae)
        _bd_reply_row(r, ae)
        return r
    return h
HANDLERS['/api/pvt/another/battle/end'] = _cm_after_battle_end(HANDLERS['/api/pvt/another/battle/end'])

def _cm_rare_done(st, did, entry_count):
    for t in md('m_dungeon_treasure'):
        if t[1] != did or t[4] != 1:
            continue
        row = st.get('user_dungeon_treasure_list', t[0])
        if row is None or not (row.entry_count == entry_count or t[5]):
            return False
    return True

def _cm_met(st, m, ae, did, entry_count):
    """-> True when this cleared run meets dungeon mission row m."""
    kind, target, amount, element = m[1], m[2], m[3], m[4]
    won = lambda b: bool((ae.another_battle_idx_flag >> b[7]) & 1)
    syms = [b for b in _cm_battles(did) if b[5] == _CM_SYMBOL]
    all_syms = bool(syms) and all(won(b) for b in syms)
    d = _cm_info(ae)
    if kind == 1:
        return ae.total_death_count == 0
    if kind == 4:
        return ae.max_blow_damage >= amount
    if kind == 5:
        return all_syms and sum(int(d['battle_time_sec_map'].get(str(b[7]), 0)) for b in syms) <= amount
    if kind == 6:
        return all_syms and all((ae.rank_s_idx_flag >> b[7]) & 1 for b in syms)
    if kind == 8:
        return int(d['element_type_total_damage_map'].get(str(element), 0)) >= amount
    if kind == 9:
        return ae.total_weak_element_damage >= amount
    if kind == 10:
        return all_syms
    if kind == 11:
        return _cm_rare_done(st, did, entry_count)
    if kind == 12:
        b = mdx('m_another_battle', 0).get((target,))
        return bool(b) and b[1] == did and won(b)
    if kind == 13:
        return all_syms and sum(int(d['battle_taken_damage_map'].get(str(b[7]), 0)) for b in syms) < amount
    print(f'criterion dungeon {did}: mission {m[0]} has unknown kind {kind}; not checked')
    return False

def _cm_end(fn):
    def h(st, req, ctx):
        q = req.post_pvt_dungeon_another_end
        ad = mdx('m_another_dungeon', 0).get((q.dungeon_id,))
        if not _cm_criterion(ad):
            return fn(st, req, ctx)
        ae = st.get('user_another_dungeon_entry_list', q.dungeon_type)
        snap = type(ae)() if ae is not None else None
        if ae is not None:
            snap.CopyFrom(ae)
        cleared = _another_cleared(st, q.dungeon_id, q.dungeon_type)
        de = st.get('user_dungeon_entry_list', q.dungeon_type)
        entry_count = q.entry_count or (de.entry_count if de is not None else 0)
        r = fn(st, req, ctx)
        body = r.post_pvt_dungeon_another_end
        fights = sum(getattr(snap, k) for k in _CRK_TOTALS) if snap is not None else 0
        score = fights + (_CM_CLEAR if cleared else 0)
        missions = _cm_missions()
        with st.transaction('dungeon/another/end: criterion missions') as tx:
            row = st.get('user_another_dungeon_list', q.dungeon_id) or st.new_row('user_another_dungeon_list', another_dungeon_id=q.dungeon_id)
            met = 0
            if cleared and snap is not None:
                obt = P.Command_Enums_RewardObtainType.Value('Command_Enums_RewardObtainType_DungeonMission')
                for i, mid in enumerate(ad[12:15]):
                    m = missions.get((mid,)) if mid else None
                    flag = f'achieved_dungeon_mission_id{i}'
                    if not m or getattr(row, flag) or not _cm_met(st, m, snap, q.dungeon_id, entry_count):
                        continue
                    setattr(row, flag, True)
                    met += 1
                    if m[5]:
                        _first_rewards(st, tx, ctx, m[5], 1, body.mission_clear_reward_infos, obt, BATTLE_FIRST_MSG)
            if score > row.high_score:
                row.high_score = score
                row.last_update_score_datetime = now_ms()
            tx.upsert('user_another_dungeon_list', row)
            if met:
                mission_event(st, tx, 'ProgressAnotherDungeonMissionComplete', met, q.dungeon_id)
        if snap is not None:
            body.death_count_score = snap.total_death_count_score
            body.max_blow_damage_score = snap.total_max_blow_damage_score
            body.total_taken_damage_score = snap.total_taken_damage_score
            body.total_battle_time_score = snap.total_battle_time_score
            body.weak_element_hit_count_score = snap.total_weak_element_hit_count_score
            body.notes_break_count_score = snap.total_notes_break_count_score
        body.dungeon_clear_score = _CM_CLEAR if cleared else 0
        body.final_score = score
        _crk_merge(r, _resp(tx))
        return r
    return h
HANDLERS['/api/pvt/dungeon/another/end'] = _me_wrap(_cm_end(HANDLERS['/api/pvt/dungeon/another/end']), '/api/pvt/dungeon/another/end')


# ---- Daily quests (Mercenary Requests): the day's quests at each new game day ----
# A new day only set the rows already held back to 1 of 1, so the same quests came back every day. Official accounts
# hold the quests of one m_daily_quest_group per day, at the player's difficulty, one row each: (remain 1, total 1)
# until won, (0, 1) after. The day's group: the newest m_daily_quest_setting [0 Id, 1 StartDatetime] that has started;
# game days since its start, modulo its m_daily_quest_setting_offset rows [0 SettingId, 1 Offset, 2 GroupSetId]; the
# set's group (m_daily_quest_group_daily_group_set_rel [0 GroupSetId, 1 GroupId]) whose m_daily_quest_group
# [0 Id, 1 DifficultyLevel] is the player's; its quests from m_daily_quest_daily_quest_group_rel
# [0 GroupId, 1 DailyQuestId, 2 wins a day]. Matched official rows on game days 567, 568 and 571 of setting 8.
T_DQ = 'user_daily_quest_list'

def dq_today(level, now=None):
    """The day's daily quests at a difficulty level: [(quest id, wins a day)]; [] when the tables name none."""
    now = now_ms() if now is None else now
    started = [r for r in md('m_daily_quest_setting') if r[1] <= now]
    if not started:
        return []
    cur = max(started, key=lambda r: r[1])
    offs = {r[1]: r[2] for r in md('m_daily_quest_setting_offset') if r[0] == cur[0]}
    if not offs:
        return []
    gset = offs.get((game_day(now) - game_day(cur[1])) % len(offs))
    groups = {r[1] for r in md('m_daily_quest_group_daily_group_set_rel') if r[0] == gset}
    grp = next((r[0] for r in md('m_daily_quest_group') if r[0] in groups and r[1] == level), None)
    return [(r[1], r[2]) for r in md('m_daily_quest_daily_quest_group_rel') if r[0] == grp and _dq(r[1]) and r[2] > 0]

def dq_level(st):
    """The player's daily quest difficulty: the highest among their rows and the quest they last started; 0 = never
    ran one."""
    ids = [r.daily_quest_id for r in st[T_DQ].values()]
    en = st.one('user_daily_quest_entry_list')
    if en:
        ids.append(en.daily_quest_id)
    return max((_dq(i)[1] for i in ids if _dq(i)), default=0)

def dq_day_start(st, tx, now=None):
    """Start of a game day: the rows become the day's quests at the player's level, nothing won. A player who never ran
    a daily quest keeps no row (the first win makes one). -> the quest ids set, [] when nothing changed."""
    today = dq_today(dq_level(st), now)
    if not today:
        for r in list(st[T_DQ].values()):           # tables name no quests today: the held rows can be cleared again
            if r.remain_win_count != 1 or r.total_remain_win_count != 1:
                r.remain_win_count = r.total_remain_win_count = 1
                tx.upsert(T_DQ, r)
        return []
    want = dict(today)
    for r in list(st[T_DQ].values()):
        if r.daily_quest_id not in want:
            tx.delete(T_DQ, r)
    for qid, n in today:
        row = st.get(T_DQ, qid) or st.new_row(T_DQ, daily_quest_id=qid)
        row.remain_win_count = row.total_remain_win_count = n
        tx.upsert(T_DQ, row)
    return [qid for qid, _ in today]


# ---- Another dungeon fight end: buff items last one fight; a lost fight is marked for Continue ----
# Buff items: dungeon/another/item adds each use to instant_item_info's used_instant_item_list
# ({instant_item_id, character_id}), and only dungeon start / end / delete cleared it, so a buff (Physical Defense Up
# and the like) stayed on that character for every later fight of the run. The list is cleared after each fight;
# used_instant_item_map (how many of each the run used) stays.
# Lost fights: on the official service, after a lost another/battle/end the run's user_dungeon_entry was
# Unreceived + Lose (recorded on Another dungeons 3010101 / 3100101), and dungeon/another/continue, whose request
# names no dungeon type, finds the run by that row. Nothing set it, so Continue always took the Another (Criterion)
# row: continuing a lost Crisis or Dungeon Ranking fight reset the wrong run. Continue puts it back to Start.
def _fight_end_after(fn):
    def h(st, req, ctx):
        r = fn(st, req, ctx)
        q = req.post_pvt_another_battle_end
        de = st.get('user_dungeon_entry_list', q.dungeon_type)
        if de is None:
            return r
        lost = q.battle_result_type == P.Command_Enums_BattleResultType_Lose
        d = _ii_load(de)
        if not lost and not d.get('used_instant_item_list'):
            return r
        with st.transaction('another/battle/end: run row') as tx:
            if d.get('used_instant_item_list'):
                de.instant_item_info = _ii_dump(d, False)
            if lost:
                de.dungeon_progress_type = P.Command_Enums_DungeonProgressType_Unreceived
                de.dungeon_result_type = P.Command_Enums_DungeonResultType_Lose
            tx.upsert('user_dungeon_entry_list', de)
        _crk_merge(r, _resp(tx))
        return r
    return h
HANDLERS['/api/pvt/another/battle/end'] = _fight_end_after(HANDLERS['/api/pvt/another/battle/end'])


# ---- Keys in story dungeons ----
# A Key is InstantItem 91, held in the run's user_dungeon_entry_list.instant_item_info; grant_reward puts it there
# only when _II.dt names the run's dungeon type, and only dungeon/another/treasure set it, so a story chest's Key went
# to "outside a dungeon - dropped" and the story gates (dungeon/story/trigger/lock) never opened. Two story dungeons
# have Key chests and gates: 1110266 (episode 110266) and 1310261 (episode 310261). Story treasure now sets
# _II.dt = Story; story start and end clear the run's field items, as Another start and end do.
def _sk_treasure(fn):
    def h(st, req, ctx):
        _II.dt = DUNGEON_STORY
        try:
            return fn(st, req, ctx)
        finally:
            _II.dt = None
    return h

def _sk_clear(fn):
    def h(st, req, ctx):
        de = st.get('user_dungeon_entry_list', DUNGEON_STORY)
        if de is not None:
            de.instant_item_info = ''      # both handlers write this row back
        return fn(st, req, ctx)
    return h
HANDLERS['/api/pvt/dungeon/story/treasure'] = _sk_treasure(HANDLERS['/api/pvt/dungeon/story/treasure'])
HANDLERS['/api/pvt/dungeon/story/start'] = _sk_clear(HANDLERS['/api/pvt/dungeon/story/start'])
HANDLERS['/api/pvt/dungeon/story/end'] = _sk_clear(HANDLERS['/api/pvt/dungeon/story/end'])
# ---- "Missions Cleared at 100% in Criterion Dungeons" (ProgressAnotherAreaMissionCompleteRate, 136) ----
# One mission, 2400104001 (bar 100, param 0), the first step of Highwind key item 104. Nothing reported the rate, so
# the step never completed and the key item stayed locked behind it. The rate is read per Criterion area (the type is
# an AnotherArea rate): the share of the area's dungeon missions (m_another_dungeon cols 12-14, the Criterion screen's
# three per dungeon) that user_another_dungeon_list marks achieved, in percent, floored; the best area counts. Each
# area holds two dungeons with three missions each, so 100 means every mission of one area. Reported (keeping the
# highest) after every dungeon/another/end and at the title, so missions cleared before this count too.
_ABS_EXTRA.add(MTYPE['ProgressAnotherAreaMissionCompleteRate'])
_AMR = {}

def _amr_areas():
    if not _AMR:
        for a in md('m_another_dungeon'):
            idx = [i for i, m in enumerate(a[12:15]) if m]
            if a[6] == 0 and idx:
                _AMR.setdefault(a[1], []).append((a[0], idx))
    return _AMR

def _amr_best(st):
    """-> the best Criterion area's dungeon-mission completion, 0-100."""
    best = 0
    for ds in _amr_areas().values():
        total = sum(len(idx) for _, idx in ds)
        got = 0
        for did, idx in ds:
            row = st.get('user_another_dungeon_list', did)
            if row is not None:
                got += sum(1 for i in idx if getattr(row, f'achieved_dungeon_mission_id{i}'))
        best = max(best, got * 100 // total)
    return best

def _amr_after(fn):
    def h(st, req, ctx):
        r = fn(st, req, ctx)
        rate = _amr_best(st)
        if rate:
            with st.transaction('missions: Criterion area mission rate') as tx:
                mission_event(st, tx, 'ProgressAnotherAreaMissionCompleteRate', rate)
            _crk_merge(r, _resp(tx))
        return r
    return h
for _amr_p in ('/api/pvt/dungeon/another/end', '/api/pvt/user/title'):
    HANDLERS[_amr_p] = _amr_after(HANDLERS[_amr_p])


def _nc_ranking_heal(st):
    """#73: accounts imported from Never Crisis before the importer checked keep its impossible Battle Ranking totals
    (one showed 1.7 trillion). For those accounts, rows of events whose total or high score is past what Square's game
    produced (editor/nevercrisis.py POINT_CEILING) are removed with that event's stage rows. Real results are billions."""
    if not st.folder:
        return 0
    try:
        meta = json.load(open(os.path.join(st.folder, 'info.json')))
    except (OSError, ValueError):
        return 0
    if not any(str(x).startswith('nevercrisis:') for x in meta.get('sources') or []):
        return 0
    ceiling = 10 ** 11
    events = {r.event_crisis_battle_id for r in st['user_event_crisis_battle_list'].values()
              if max(r.total_point, r.max_high_score) > ceiling}
    if not events:
        return 0
    gone = []
    for table, key in (('user_event_crisis_battle_list', lambda r: r.event_crisis_battle_id),
                       ('user_event_crisis_battle_solo_battle_list', lambda r: r.event_solo_battle_id // 1000),
                       ('user_event_damage_ranking_battle_solo_battle_list', lambda r: r.event_solo_battle_id // 1000)):
        gone += [(table, r) for r in list(st[table].values()) if key(r) in events]
    with st.transaction('Never Crisis ranking heal') as tx:
        for table, r in gone:
            tx.delete(table, r)
    board = _crisis_load(st)
    mine = board.get(str(st.user_id), {})
    if any(str(e) in mine for e in events):
        for e in events:
            mine.pop(str(e), None)
        _crisis_save(st, board)
    _CR_ALL['t'] = 0.0
    return len(gone)
_nc_title_orig = HANDLERS['/api/pvt/user/title']

def _nc_title(st, req, ctx):
    try:
        n = _nc_ranking_heal(st)
        if n:
            print(f'Never Crisis ranking heal: {st.user_id} {n} row(s)')
    except Exception as e:
        print(f'Never Crisis ranking heal skipped: {e!r}')
    return _nc_title_orig(st, req, ctx)
for _a in ('unlocked',):
    if hasattr(_nc_title_orig, _a):
        setattr(_nc_title, _a, getattr(_nc_title_orig, _a))
HANDLERS['/api/pvt/user/title'] = _nc_title
