"""Event and crossover stories as Story titles on the Story screen.

Built from the player's own tables when engine/masterdata_patches.py rebuilds live data, as told by the recipe's
"story_titles" (engine/data/masterdata-patches.json). Laid out the way the game's own story ports are (DIRGE of
CERBERUS, title 6): one title per group, one chapter per event (oldest event = chapter 1), one section per chapter at
map position 9999 (no map), the event's scenario episodes in order:

- a scene plays a Story copy of the event's scene dungeon. The game's ports use copies too: the same dungeon with
  dungeonType 1 instead of the event's -2. A Story episode on the event dungeon itself plays, then ends on a black
  screen. The copies are id - 200000 (m_dungeon rows here; their bundles come from engine/storyart.py).
- a fight is a story battle on the stage's own battle.
- a title's first chapter must open with a scene. Home's Story "tap to start" (HomeContentStory.TapToStartAsync)
  enters the title's first episode without a premise straight as a dungeon while it is not cleared, and a fight has
  none: the black loading screen forever. A title's "first" (recipe) lists events shown before the rest; the crossovers
- a fight's story party is the player's own pick: group STORY_PARTY_GROUP lists every playable character with none
  fixed. DIRGE's fight row (the template) points at its own group 601, which fixes Vincent, so every fight copied
  from it fielded Vincent. The stage's guest group and its battle's recommended combat power come from the stage.
  show A Resonant Waltz first (the REBIRTH crossover, listed first, opens with 13 fights). Ids stay in "events" order
  (oldest first) and only the chapters' OrderNo moves, so clears already made keep their episodes. A build whose
  first chapter opens with a fight is refused. (Fights later in a title are fine: the Story screen sends a fight to the party screen.)
  A title's "move" ({event: other title}) shows one of its events in the other title under the id it already has, so
  its clears stay: A Resonant Waltz, the first FFIX crossover, was built under Event Stories and is shown under
  Crossover Stories (#106). The other title's "first" can place it; otherwise it comes after that title's own.
- no first-clear rewards (m_story_episode FirstRewardSetId 6, StoryPartyFirstRewardSetId 12): the events keep their
  own cards, which pay them; the rows copied from DIRGE and FINAL FANTASY VII would otherwise pay those stories'.

Ids: title as given; chapter and section = base + n; episodes and story battles 88000000 + chapter * 1000 + n
. The game's titles move down so these sit on top, in the recipe's order.
"""

DUNGEON_SHIFT = -200000          # Story copy of an event scene dungeon: 99xxxxx -> 97xxxxx
EPISODE_BASE = 88000000
TEMPLATE_TITLE, TEMPLATE_CHAPTER, TEMPLATE_SECTION = 6, 601, 601       # DIRGE of CERBERUS
TEMPLATE_SCENE, TEMPLATE_FIGHT = 303901, 303910
TEMPLATE_PARTY = 601                                                 # DIRGE's story party (Vincent fixed)
TABLES = ('m_story_title', 'm_story_chapter', 'm_story_section', 'm_story_episode', 'm_story_battle', 'm_dungeon',
          'm_story_character_group_rel')
STORY_PARTY_GROUP, STORY_PARTY_SIZE = 880, 3     # every playable character, none fixed (DIRGE's 601 without its fix)


def wanted(recipe):
    return bool(recipe.get('story_titles'))


def incomplete(what):
    return ValueError(f'MasterData is incomplete or incompatible: story titles: {what}')


def conflict(what):
    return ValueError(f'MasterData version conflict: story titles: {what}')


def _clean(text):
    return (text or '').replace('\\n', ' ').replace('\n', ' ').strip()


def plan(recipe, source, stock_text=None):
    """(new rows {table: [rows]}, title order {title id: order}, texts {table: {id: value}}, dungeon copies {event
    dungeon: story copy})."""
    spec = recipe['story_titles']
    text = stock_text or (lambda name: {})
    titles = {row[0]: row for row in source('m_story_title')}
    chapters = {row[0]: row for row in source('m_story_chapter')}
    sections = {row[0]: row for row in source('m_story_section')}
    episodes = {row[0]: row for row in source('m_story_episode')}
    battles = {row[0] for row in source('m_story_battle')}
    dungeons = {row[0]: row for row in source('m_dungeon')}
    events = {row[0]: row for row in source('m_event_base')}
    stages = {row[0]: row for row in source('m_event_solo_battle')}
    battle_rows = {row[0]: row for row in source('m_battle')}
    party = [row for row in source('m_story_character_group_rel') if row[0] == TEMPLATE_PARTY]
    if not party:
        raise incomplete(f'm_story_character_group_rel has no group {TEMPLATE_PARTY} to copy')
    if any(row[0] == STORY_PARTY_GROUP for row in source('m_story_character_group_rel')):
        raise conflict(f'story character group {STORY_PARTY_GROUP} already exists')
    by_section = {}
    for row in source('m_event_scenario_episode'):
        by_section.setdefault(row[1], []).append(row)
    for table, key, have in (('m_story_title', TEMPLATE_TITLE, titles), ('m_story_chapter', TEMPLATE_CHAPTER, chapters),
                             ('m_story_section', TEMPLATE_SECTION, sections),
                             ('m_story_episode', TEMPLATE_SCENE, episodes), ('m_story_episode', TEMPLATE_FIGHT, episodes)):
        if key not in have:
            raise incomplete(f'{table} has no row {key} to copy')
    event_names, scenario_names, stage_names = text('m_event_base'), text('m_event_scenario_episode'), text('m_event_solo_battle')
    new = {table: [] for table in TABLES}
    for row in party:
        row = list(row); row[0], row[3] = STORY_PARTY_GROUP, False
        new['m_story_character_group_rel'].append(row)
    names = {table: {} for table in ('m_story_title', 'm_story_chapter', 'm_story_section', 'm_story_episode')}
    order, copies = {}, {}
    skip = set(spec.get('skip', []))
    moved = {int(e): t for title in spec['titles'] for e, t in title.get('move', {}).items()}
    shown, opening = {}, {}
    for position, title in enumerate(spec['titles'], 1):
        tid, base = title['id'], title['chapter_base']
        if tid in titles:
            raise conflict(f'title {tid} already exists')
        row = list(titles[TEMPLATE_TITLE])
        row[0], row[2], row[3], row[4], row[5] = tid, title['code'], 0, 0, position
        row[7], row[8], row[9] = 231000000000000 + tid, 232000000000000 + tid, title['art']
        new['m_story_title'].append(row); order[tid] = position
        names['m_story_title'].update({row[7]: title['name'], row[8]: title['description']})
        number = 0
        for event_id in title['events']:                     # oldest first: ids
            event = events.get(event_id)
            if event_id in skip or event is None:
                continue
            scenario = sorted(by_section.get(event[4], []), key=lambda e: e[4])
            if not scenario:
                continue
            number += 1; cid = base + number
            if cid in chapters or cid in sections:
                raise conflict(f'chapter or section {cid} already exists')
            chapter = list(chapters[TEMPLATE_CHAPTER])
            chapter[0], chapter[1], chapter[2], chapter[3], chapter[5], chapter[6], chapter[7] = (
                cid, moved.get(event_id, tid), number, 210000000000000 + cid, 0, 0, False)
            new['m_story_chapter'].append(chapter)
            label = _clean(event_names.get(event[10], ''))
            names['m_story_chapter'][chapter[3]] = label
            section = list(sections[TEMPLATE_SECTION])
            section[0], section[1], section[2], section[3], section[4], section[5], section[6] = (
                cid, cid, 1, 9999, 200000000000000 + cid, 1, 1)
            new['m_story_section'].append(section)
            names['m_story_section'][section[4]] = label
            ids = {e[0]: EPISODE_BASE + cid * 1000 + n for n, e in enumerate(scenario, 1)}
            fight = lambda e: not e[3] and e[11]
            shown[event_id] = chapter; opening[event_id] = fight(scenario[0])
            for n, e in enumerate(scenario, 1):
                eid = ids[e[0]]
                if eid in episodes:
                    raise conflict(f'episode {eid} already exists')
                name = scenario_names.get(e[7], '') if e[7] > 1 else ''
                if fight(e):
                    stage = stages.get(e[11])
                    if stage is None:
                        raise incomplete(f'episode {e[0]} fights stage {e[11]}, which m_event_solo_battle lacks')
                    if eid in battles:
                        raise conflict(f'story battle {eid} already exists')
                    row = list(episodes[TEMPLATE_FIGHT])
                    row[4], row[15] = 0, eid
                    row[13], row[14], row[25] = STORY_PARTY_GROUP, STORY_PARTY_SIZE, stage[19]
                    battle = battle_rows.get(stage[5])
                    power = battle[12] if battle and not battle[11] else 0
                    row[9] = row[10] = power
                    new['m_story_battle'].append([eid, stage[5], stage[5], 0, False])
                    name = stage_names.get(stage[13], '') or name
                else:
                    if not e[3]:
                        raise incomplete(f'episode {e[0]} has neither a dungeon nor a stage')
                    if e[3] not in dungeons:
                        raise incomplete(f'episode {e[0]} plays dungeon {e[3]}, which m_dungeon lacks')
                    copy_id = e[3] + DUNGEON_SHIFT
                    if copy_id in dungeons:
                        raise conflict(f'dungeon {copy_id} (Story copy of {e[3]}) already exists')
                    copies[e[3]] = copy_id
                    row = list(episodes[TEMPLATE_SCENE]); row[4] = copy_id
                # an episode the event opened by date alone (no premise: REBIRTH's last 6, 3012's) needs the one before
                premise = ids.get(e[2], 0) or (ids[scenario[n - 2][0]] if n > 1 else 0)
                row[0], row[1], row[3], row[5], row[6], row[7], row[12] = eid, cid, premise, n, 0, 1, 0
                row[17], row[18] = 220000000000000 + eid, 221000000000000 + eid
                new['m_story_episode'].append(row)
                names['m_story_episode'].update({row[17]: _clean(name) or f'Episode {n}', row[18]: ''})
    for title in spec['titles']:
        tid = title['id']
        mine = lambda e: e in shown and shown[e][1] == tid
        first = [e for e in title.get('first', []) if mine(e)]
        display = first + [e for e in title['events'] if mine(e) and e not in first]
        display += [e for e, t in moved.items() if t == tid and mine(e) and e not in display]
        for rank, event_id in enumerate(display, 1):
            shown[event_id][2] = rank                         # OrderNo: what the Story screen and Home's start use
        if display and opening[display[0]]:
            raise incomplete(f'title {tid} would open with a fight (event {display[0]}): put an event that opens with a '
                             'scene in its "first"')
    for original, copy_id in sorted(copies.items()):
        row = list(dungeons[original]); row[0] = row[1] = copy_id
        new['m_dungeon'].append(row)
    return new, order, names, copies


def build(recipe, source, tables):
    """Add the titles' rows to the expected tables and move the game's titles below them."""
    if not wanted(recipe):
        return
    for table in TABLES:
        if table not in tables:
            raise incomplete(f'the recipe does not list {table}')
    new, order, _, _ = plan(recipe, source)
    for row in tables['m_story_title']:
        row[5] += len(order)
    for table, rows in new.items():
        tables[table].extend(rows)


def texts(recipe, source, stock_text):
    """{text table: {id: value}}: the titles', chapters', sections' and episodes' names."""
    if not wanted(recipe):
        return {}
    return plan(recipe, source, stock_text)[2]


def dungeon_copies(recipe, source):
    """{event scene dungeon: its Story copy} for engine/storyart.py."""
    return plan(recipe, source)[3] if wanted(recipe) else {}
