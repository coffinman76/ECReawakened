import os
import time
import json
MARK = 'eventkit'
_MEM_BOARDS = {}
_MEM_RUNS = {}
STALE_RUN_MS = 60 * 60 * 1000

def install(g):
    H = g['HANDLERS']
    P = g['P']
    md = g['md']
    mdx = g['mdx']
    _resp0 = g['_resp']
    from engine import vault as _es
    _es.LEGACY.setdefault('scoredungeon', 'scoredungeon_legacy_none.json')
    grant_reward_set = g['grant_reward_set']
    flag_or = g['_flag_or']
    profile = g.get('_crisis_profile') or (lambda st: ('', [0, 0, 0, 0]))
    syn_duid = g.get('_syn_duid') or (lambda uid: str(uid))
    Q = P.Command_ApiNetwork_Api_ApiRequest

    def _resp(tx, field, body=None):
        r = _resp0(tx, field, body)
        getattr(r, field).SetInParent()
        return r
    now_ms = lambda: int(time.time() * 1000)

    def board_all(st, name):
        if not st.folder:
            return dict(_MEM_BOARDS.get(name, {}))
        from engine.vault import board_all as ba
        return ba(os.path.dirname(st.folder), name)

    def board_put(st, name, value):
        if not st.folder:
            _MEM_BOARDS.setdefault(name, {})[str(st.user_id)] = value
            return
        from engine.vault import board_put as bp
        bp(os.path.dirname(st.folder), name, st.user_id, value)

    def _runs_path(st):
        return os.path.join(st.folder, 'sd_runs.json') if st.folder else None

    def runs_load(st):
        p = _runs_path(st)
        if not p:
            return _MEM_RUNS.setdefault(st.user_id, {})
        try:
            return json.load(open(p))
        except Exception:
            return {}

    def runs_save(st, runs):
        p = _runs_path(st)
        if not p:
            _MEM_RUNS[st.user_id] = runs
            return
        tmp = p + '.tmp'
        json.dump(runs, open(tmp, 'w'))
        os.replace(tmp, p)

    def esd_row(esd_id):
        return mdx('m_event_score_dungeon', 0).get((esd_id,))

    def area_dungeons(esd_id):
        e = esd_row(esd_id)
        return [r[0] for r in md('m_another_dungeon') if e and r[1] == e[1]]

    def esd_of_dungeon(dungeon_id):
        ad = mdx('m_another_dungeon', 0).get((dungeon_id,))
        if not ad:
            return 0
        return next((e[0] for e in md('m_event_score_dungeon') if e[1] == ad[1]), 0)

    def rank_rows(dungeon_id):
        return sorted((r for r in md('m_another_dungeon_rank') if r[0] == dungeon_id), key=lambda r: r[2])

    def rank_for(dungeon_id, score):
        best = None
        for r in rank_rows(dungeon_id):
            if score >= r[2]:
                best = r
        return best

    def merge_up(resp, tx2):
        from engine.vault import row_key
        up = resp.common.user.update
        for f, lst in tx2.up.ListFields():
            dst = getattr(up, f.name)
            keys = {row_key(f.name, r) for r in lst}
            keep = [r for r in dst if row_key(f.name, r) not in keys]
            del dst[:]
            dst.extend(keep)
            dst.extend(lst)

    def call_inner(st, path, field, inner_req, ctx):
        fn = H.get(path)
        if fn is None:
            raise RuntimeError(f'no inner handler {path}')
        q = Q()
        getattr(q, field).CopyFrom(inner_req)
        return fn(st, q, dict(ctx or {}))

    def rewrap(resp, inner_field, outer_field, outer_sub):
        src = getattr(resp, inner_field)
        body = type(src)()
        body.CopyFrom(src)
        resp.ClearField(inner_field)
        out = getattr(resp, outer_field)
        getattr(out, outer_sub).CopyFrom(body)
        out.SetInParent()
        resp.ClearField(inner_field)
        return out

    def ack(st, field, name):
        with st.transaction(name) as tx:
            pass
        r = _resp(tx, field)
        getattr(r, field).SetInParent()
        return r

    def entry_row(st, tx, esd_id):
        row = st.get('user_event_score_dungeon_entry_list', esd_id)
        if row is None:
            row = st.new_row('user_event_score_dungeon_entry_list', event_score_dungeon_id=esd_id, group_idx=1)
            tx.upsert('user_event_score_dungeon_entry_list', row)
        return row

    def my_total(st, esd_id):
        tot = 0
        rows = []
        for d in area_dungeons(esd_id):
            r = st.get('user_another_dungeon_list', d)
            if r is not None:
                rows.append(r)
                tot += r.high_score
        return (tot, rows)

    def publish(st, esd_id):
        tot, rows = my_total(st, esd_id)
        b = board_all(st, 'scoredungeon').get(str(st.user_id), {})
        name, hon = profile(st)
        b[str(esd_id)] = {'pt': tot, 'name': name, 'hon': hon, 'duid': syn_duid(st.user_id), 'dungeons': {str(r.another_dungeon_id): [r.high_score, int(r.high_rank_type), r.win_count] for r in rows}}
        board_put(st, 'scoredungeon', b)

    def ranked(st, esd_id):
        k = str(esd_id)
        rows = [(u, e[k]) for u, e in board_all(st, 'scoredungeon').items() if k in e and e[k].get('pt', 0) > 0]
        rows.sort(key=lambda x: -x[1].get('pt', 0))
        return rows

    def my_rank(st, esd_id):
        return next((i + 1 for i, (u, e) in enumerate(ranked(st, esd_id)) if u == str(st.user_id)), 0)

    def rank_info(dst, e, rank):
        ri = dst.add()
        ri.display_user_id = e.get('duid', '')
        ri.player_name = e.get('name', '')
        hon = list(e.get('hon') or []) + [0, 0, 0, 0]
        ri.top_honor_id, ri.bottom_honor_id, ri.background_honor_id, ri.icon_honor_id = (hon[0], hon[1], hon[2], hon[3])
        ri.rank = rank
        ri.score = e.get('pt', 0)

    def sd_start(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_start
        runs = runs_load(st)
        runs[str(q.dungeon_another_start_request.dungeon_type or 3)] = {'esd': q.event_score_dungeon_id, 'score': 0, 't': now_ms()}
        runs_save(st, runs)
        r = call_inner(st, '/api/pvt/dungeon/another/start', 'post_pvt_dungeon_another_start', q.dungeon_another_start_request, ctx)
        rewrap(r, 'post_pvt_dungeon_another_start', 'post_pvt_event_score_dungeon_start', 'dungeon_another_start_response')
        with st.transaction('event/score/dungeon/start') as tx2:
            entry_row(st, tx2, q.event_score_dungeon_id)
        merge_up(r, tx2)
        return r

    def sd_battle_start(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_battle_start
        sreq = q.another_battle_start_request
        runs = runs_load(st)
        slot = runs.setdefault(str(sreq.dungeon_type or 3), {'esd': q.event_score_dungeon_id, 'score': 0, 't': now_ms()})
        slot['boss'] = {'id': sreq.another_battle_id, 'stage': sreq.boss_enhance_stage}
        runs_save(st, runs)
        r = call_inner(st, '/api/pvt/another/battle/start', 'post_pvt_another_battle_start', sreq, ctx)
        rewrap(r, 'post_pvt_another_battle_start', 'post_pvt_event_score_dungeon_battle_start', 'another_battle_start_response')
        return r

    def complete_end_request(st, ereq, slot, dtype):
        """Bosses patched to fight client-side (tools/scoreboss.py) finish with a bare end request: only the
        event id arrives. Rebuild what another/battle/end needs from the fight that battle/start saw."""
        if ereq.another_battle_id:
            return
        boss = slot.get('boss') or {}
        ereq.another_battle_id = boss.get('id', 0)
        ereq.boss_enhance_stage = boss.get('stage', 0)
        ereq.dungeon_type = ereq.dungeon_type or dtype
        entry = st.get('user_dungeon_entry_list', ereq.dungeon_type)
        if entry is not None and not ereq.entry_count:
            ereq.entry_count = entry.entry_count
        # No outcome is sent either; reaching this call means the fight finished, so count it as won.
        ereq.battle_result_type = ereq.battle_result_type or P.Command_Enums_BattleResultType_Win
        print(f'score dungeon {st.user_id}: bare battle/end completed as boss {ereq.another_battle_id} (stage {ereq.boss_enhance_stage}, entry {ereq.entry_count})')

    def sd_battle_end(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_battle_end
        ereq = type(q.another_battle_end_request)()
        ereq.CopyFrom(q.another_battle_end_request)
        dtype = ereq.dungeon_type or 3
        runs = runs_load(st)
        slot = runs.setdefault(str(dtype), {'esd': q.event_score_dungeon_id, 'score': 0, 't': now_ms()})
        complete_end_request(st, ereq, slot, dtype)
        r = call_inner(st, '/api/pvt/another/battle/end', 'post_pvt_another_battle_end', ereq, ctx)
        body = getattr(r, 'post_pvt_another_battle_end')
        for part in (body.battle_result, body.score_result):
            part.SetInParent()
        slot['score'] = slot.get('score', 0) + max(0, body.win_score)
        body.total_win_score = slot['score']
        runs_save(st, runs)
        out = rewrap(r, 'post_pvt_another_battle_end', 'post_pvt_event_score_dungeon_battle_end', 'another_battle_end_response')
        out.battle_input.CopyFrom(ereq.battle_input)
        out.battle_input.SetInParent()
        return r

    def sd_end(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_end
        iq = q.dungeon_another_end_request
        prev = st.get('user_another_dungeon_list', iq.dungeon_id)
        prev_rank = int(prev.high_rank_type) if prev is not None else 0
        # #71: fights that end the Crisis way (tools/scoreboss.patch_battle_end) score through another/battle/end into
        # the run's entry totals; the run is scored like a Crisis run from them (fight totals, plus the ATB, item and
        # clear bonuses and the boss multiplier on a cleared run). Read before the inner end handler.
        crk = g.get('_crk_score')
        run_score, parts = crk(st, iq.dungeon_id, iq.dungeon_type or 3) if crk else (0, {})
        r = call_inner(st, '/api/pvt/dungeon/another/end', 'post_pvt_dungeon_another_end', iq, ctx)
        body = getattr(r, 'post_pvt_dungeon_another_end')
        runs = runs_load(st)
        k = str(iq.dungeon_type or 3)
        score = runs.pop(k, {}).get('score', 0)
        runs_save(st, runs)
        if run_score > score:
            score = run_score
            for name, value in parts.items():
                setattr(body, name, value)
        esd_id = q.event_score_dungeon_id or esd_of_dungeon(iq.dungeon_id)
        with st.transaction('event/score/dungeon/end') as tx2:
            row = st.get('user_another_dungeon_list', iq.dungeon_id) or st.new_row('user_another_dungeon_list', another_dungeon_id=iq.dungeon_id)
            rk = rank_for(iq.dungeon_id, score)
            rank_type = rk[1] if rk else 0
            if score > row.high_score:
                row.high_score = score
            if rank_type > int(row.high_rank_type):
                row.high_rank_type = rank_type
            tx2.upsert('user_another_dungeon_list', row)
            for rr in rank_rows(iq.dungeon_id):
                if prev_rank < rr[1] <= rank_type and rr[3]:
                    fi = body.first_reward_infos.add()
                    fi.dungeon_rank_type = rr[1]
                    grant_reward_set(st, tx2, rr[3], 1, fi.reward_info)
            if rk and rk[4]:
                grant_reward_set(st, tx2, rk[4], 1, body.lap_reward_infos)
            if esd_id:
                entry_row(st, tx2, esd_id)
            body.total_battle_win_score = score
            body.final_score = score
            body.final_score_dungeon_rank_type = rank_type
        merge_up(r, tx2)
        rewrap(r, 'post_pvt_dungeon_another_end', 'post_pvt_event_score_dungeon_end', 'dungeon_another_end_response')
        if esd_id:
            publish(st, esd_id)
        return r

    def sd_inner(path, inner_field, outer_field, inner_sub, outer_sub):

        def fn(st, req, ctx):
            q = getattr(req, outer_field)
            if H.get(path) is not None:
                r = call_inner(st, path, inner_field, getattr(q, inner_sub), ctx)
                rewrap(r, inner_field, outer_field, outer_sub)
                return r
            r = ack(st, outer_field, path)
            getattr(getattr(r, outer_field), outer_sub).SetInParent()
            return r
        return fn

    def sd_current_rank(st, req, ctx):
        esd_id = req.post_pvt_event_score_dungeon_current_rank.event_score_dungeon_id
        r = ack(st, 'post_pvt_event_score_dungeon_current_rank', 'event/score/dungeon/current/rank')
        r.post_pvt_event_score_dungeon_current_rank.rank = my_rank(st, esd_id)
        return r

    def sd_score_get(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_score_get
        with st.transaction('event/score/dungeon/score/get') as tx:
            ent = entry_row(st, tx, q.event_score_dungeon_id)
        r = _resp(tx, 'post_pvt_event_score_dungeon_score_get')
        body = r.post_pvt_event_score_dungeon_score_get
        body.SetInParent()
        body.group_idx = ent.group_idx
        mine = not q.display_user_id or q.display_user_id == syn_duid(st.user_id)
        if mine:
            tot, rows = my_total(st, q.event_score_dungeon_id)
            body.total_score = tot
            body.user_another_dungeons.extend(rows)
        else:
            k = str(q.event_score_dungeon_id)
            for u, e in board_all(st, 'scoredungeon').items():
                if e.get(k, {}).get('duid') == q.display_user_id:
                    body.total_score = e[k].get('pt', 0)
                    for d, (hs, rt, wc) in e[k].get('dungeons', {}).items():
                        body.user_another_dungeons.add(user_id=int(u), another_dungeon_id=int(d), high_score=hs, high_rank_type=rt, win_count=wc)
                    break
        return r

    def sd_group_ranking(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_group_ranking
        rows = ranked(st, q.event_score_dungeon_id)
        r = ack(st, 'post_pvt_event_score_dungeon_group_ranking', 'event/score/dungeon/group/ranking')
        body = r.post_pvt_event_score_dungeon_group_ranking
        start = q.start_rank or 1
        for i, (u, e) in enumerate(rows):
            if i + 1 < start:
                continue
            rank_info(body.ranking_user_infos, e, i + 1)
            if len(body.ranking_user_infos) >= 50:
                break
        body.max_rank = len(rows)
        return r

    def sd_friend_ranking(st, req, ctx):
        esd_id = req.post_pvt_event_score_dungeon_friend_ranking.event_score_dungeon_id
        r = ack(st, 'post_pvt_event_score_dungeon_friend_ranking', 'event/score/dungeon/friend/ranking')
        for i, (u, e) in enumerate(ranked(st, esd_id)[:50]):
            rank_info(r.post_pvt_event_score_dungeon_friend_ranking.ranking_user_infos, e, i + 1)
        return r

    def sd_score_reward_receive(st, req, ctx):
        q = req.post_pvt_event_score_dungeon_score_reward_receive
        e = esd_row(q.event_score_dungeon_id)
        tot, _ = my_total(st, q.event_score_dungeon_id)
        with st.transaction('event/score/dungeon/score/reward/receive') as tx:
            body = P.Command_ApiNetwork_Api_PostPvtEventScoreDungeonScoreRewardReceiveResponse()
            ent = entry_row(st, tx, q.event_score_dungeon_id)
            tiers = {row[1]: row for row in md('m_event_score_dungeon_score') if e and row[0] == e[4]}
            want = list(q.idx_list) or sorted(tiers)
            flag = ent.received_score_reward_idx_flag
            for idx in want:
                t = tiers.get(idx)
                if not t or tot < t[2] or flag & 1 << idx:
                    continue
                grant_reward_set(st, tx, t[3], 1, body.reward_info_list)
                flag = flag_or(flag, idx)
            ent.received_score_reward_idx_flag = flag
            tx.upsert('user_event_score_dungeon_entry_list', ent)
        return _resp(tx, 'post_pvt_event_score_dungeon_score_reward_receive', body)

    def sd_ranking_reward_receive(st, req, ctx):
        esd_id = req.post_pvt_event_score_dungeon_ranking_reward_receive.event_score_dungeon_id
        e = esd_row(esd_id)
        rank = my_rank(st, esd_id)
        with st.transaction('event/score/dungeon/ranking/reward/receive') as tx:
            body = P.Command_ApiNetwork_Api_PostPvtEventScoreDungeonRankingRewardReceiveResponse()
            body.rank = rank
            ent = entry_row(st, tx, esd_id)
            if rank > 0 and e and (not ent.is_received_ranking_reward):
                tiers = sorted((row for row in md('m_ranking') if row[0] == e[3]), key=lambda x: -x[1])
                rs = next((row[2] for row in tiers if rank >= row[1]), None)
                if rs:
                    grant_reward_set(st, tx, rs, 1, body.reward_info_list)
                ent.is_received_ranking_reward = True
                ent.final_rank = rank
                tx.upsert('user_event_score_dungeon_entry_list', ent)
        r = _resp(tx, 'post_pvt_event_score_dungeon_ranking_reward_receive', body)
        return r
    def stale_run_heal(st, now=None):
        """A Dungeon Ranking run whose game stopped mid-run (network error, crash, restart) stays in progress (#54):
        the game then refuses party changes until the run's suspended-data prompt is found and discarded. At the
        title screen a run older than STALE_RUN_MS is discarded the way that prompt's Discard does (dungeon/delete):
        entry row -> End, its instant items cleared, its sd_runs.json record dropped; no score, no rewards. A younger
        run is left alone so a quick restart can still resume. Age = the run's start time 't' in sd_runs.json; a
        run with none recorded is stale. -> the dungeon types discarded"""
        now = g['now_ms']() if now is None else now
        de = st.get('user_dungeon_entry_list', P.Command_Enums_DungeonType_ScoreDungeon)
        if de is None or de.dungeon_progress_type != P.Command_Enums_DungeonProgressType_Start:
            return []
        runs = runs_load(st)
        k = str(P.Command_Enums_DungeonType_ScoreDungeon)
        t = (runs.get(k) or {}).get('t') or 0
        if now - t < STALE_RUN_MS:
            return []
        with st.transaction('score dungeon: stale run discarded') as tx:
            de.dungeon_progress_type = P.Command_Enums_DungeonProgressType_End
            de.instant_item_info = ''
            tx.upsert('user_dungeon_entry_list', de)
        if runs.pop(k, None) is not None:
            runs_save(st, runs)
        print(f'score dungeon {st.user_id}: run in dungeon {de.dungeon_id} from {t} discarded ({(now - t) // 60000} min old)')
        return [P.Command_Enums_DungeonType_ScoreDungeon]
    g['_sd_stale_run_heal'] = stale_run_heal

    def entry_heal(st):
        """The game greys out a Dungeon Ranking event, with no lock icon and no response to a tap, when the save has
        no user_event_score_dungeon_entry_list row for it. The live server added that row (the player's ranking group)
        when the event began, so a save that never saw a given event has none. At the title screen each missing row
        is added in group 1 with nothing claimed. -> the event_score_dungeon ids added"""
        missing = [e[0] for e in md('m_event_score_dungeon') if st.get('user_event_score_dungeon_entry_list', e[0]) is None]
        if not missing:
            return []
        with st.transaction('score dungeon: entry rows added') as tx:
            for esd_id in missing:
                entry_row(st, tx, esd_id)
        print(f'score dungeon {st.user_id}: entry rows added for {missing}')
        return missing
    g['_sd_entry_heal'] = entry_heal

    SD = {'/api/pvt/event/score/dungeon/start': sd_start, '/api/pvt/event/score/dungeon/battle/start': sd_battle_start, '/api/pvt/event/score/dungeon/battle/end': sd_battle_end, '/api/pvt/event/score/dungeon/end': sd_end, '/api/pvt/event/score/dungeon/current/rank': sd_current_rank, '/api/pvt/event/score/dungeon/score/get': sd_score_get, '/api/pvt/event/score/dungeon/group/ranking': sd_group_ranking, '/api/pvt/event/score/dungeon/friend/ranking': sd_friend_ranking, '/api/pvt/event/score/dungeon/score/reward/receive': sd_score_reward_receive, '/api/pvt/event/score/dungeon/ranking/reward/receive': sd_ranking_reward_receive}
    for sub in ('treasure', 'item', 'trigger/lock', 'party/separate', 'buff/debuff/select'):
        u = sub.replace('/', '_')
        SD[f'/api/pvt/event/score/dungeon/another/{sub}'] = sd_inner(f'/api/pvt/dungeon/another/{sub}', f'post_pvt_dungeon_another_{u}', f'post_pvt_event_score_dungeon_another_{u}', f'another_{u}_request', f'another_{u}_response')
    for p, fn in SD.items():
        H[p] = fn

    def generic(field):

        def fn(st, req, ctx):
            return ack(st, field, field)
        fn.generic_ack = True
        return fn
    SKIP = ('multi_battle', 'guild_ranking_battle_main', 'guild_ranking_battle_practice')
    added = []
    for f in Q.DESCRIPTOR.fields:
        n = f.name
        if not n.startswith('post_pvt_') or 'event' not in n or any((s in n for s in SKIP)):
            continue
        path = '/api/pvt/' + n[len('post_pvt_'):].replace('_', '/')
        if path in H:
            continue
        if not hasattr(P.Command_ApiNetwork_Api_ApiResponse(), n):
            continue
        H[path] = generic(n)
        added.append(path)
    install.added = added
    return (sorted(SD), added)
