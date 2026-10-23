"""Generated Events-list cards and event towers for the offline MasterData.

Everything here is built from the player's own tables when engine/masterdata_patches.py rebuilds live data, as
told by the recipe (engine/data/masterdata-patches.json):

- "list_cards": several events on one card that opens the event page's area list (entries on the left, the chosen
  entry's stages on the right). The events' areas move under the card; stage and area ids stay. With "stories", every
  event keeps its own card (its story-fight areas, or its first area); its other areas go on the list. For events in
  "copies" the own card keeps every area and the list gets copies of the others, or of every area without "stories"
  (own ids; a win on either a copy or its original clears both, so first-clear rewards are paid once: engine/rules.py
  _stage_copies). Critical Threats are copied so their pages, with their ticket exchanges, stay open.
- "guild_card": the guild battles, which were fought online, as solo stages on one card ("mock battles").
- "escalation_cards": every Escalation ladder on one card and every Escalation co-op area on a second.
- "coop_as_solo" (from gameplay-settings.json, on unless false): every co-op fight also as a solo stage.
- "event_towers": events that are long runs of single fights, as towers on the Solo > Towers screen.
- "victory_draws": every Critical Threat's Victory Draws in one list on one Critical Threat page (the hub); the other
  Critical Threat cards are hidden (their fights stay on the Critical Threats list card as copies).

Each builder writes its rows into the tables and its names into Build.names in one pass. The list layout comes from
the page a card borrows (m_event_base col 20 EventDetailId stays the page's; cols 12 and 25, the tile and the art
inside, may come from another event) and from its areas sharing one area category (m_event_solo_area col 7 ->
m_event_area_category). A card that is shown with no areas behind it hangs the game, so every event whose areas move
has its own card hidden; that also hides its story chapter, while the chapter's fights stay on the card as stages.
"""
import copy
import textwrap
from datetime import datetime, timezone

HIDDEN = 620009999001    # never-true condition set (masterdata_patches._add_offline_rows)
SHOWN = 620009999002     # always-true condition set
ART = (12, 25)           # m_event_base: 12 the Events-list tile, 25 the art inside; 20 (Event Info) stays the page's

# text id = base + number; entries and stages are numbered per card (card * 1000 + n) or by their own id
TEXT_BASE = {
    'name': 851000000090000, 'subtitle': 852000000090000, 'label': 868000000090000,
    'category': 869000000090000, 'group': 862000090000000, 'entry': 863000090000000,
    'stage': 864000090000000, 'tower': 462 * 10 ** 12, 'tower_group': 460 * 10 ** 12, 'floor': 463 * 10 ** 12,
}


def text_id(kind, number):
    return TEXT_BASE[kind] + number


def wanted(recipe):
    """True when the recipe asks for any generated card or tower."""
    return any(recipe.get(key) for key in ('list_cards', 'guild_card', 'escalation_cards', 'event_towers',
                                           'victory_draws'))


def generated_ids(recipe):
    """Event and area-group ids the generated cards own: an area under one of them belongs to another event."""
    ids = [card['card'] for card in recipe.get('list_cards', ())]
    ids += [card['coop']['card'] for card in recipe.get('list_cards', ()) if card.get('coop')]
    if recipe.get('guild_card'):
        ids.append(recipe['guild_card']['card'])
    escalation = recipe.get('escalation_cards') or {}
    ids += [escalation[part]['card'] for part in ('solo', 'coop') if part in escalation]
    if 'extreme' in escalation:
        ids.append(escalation['extreme']['group'])
    return frozenset(ids)


def incomplete(what):
    return ValueError(f'MasterData is incomplete or incompatible: {what}')


def conflict(what):
    return ValueError(f'MasterData version conflict: {what}')


def clean(name):
    return name.replace('\\n', ' ').replace('\n', ' ').strip()


def month(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%b '%y")


ENTRY_LINE = 30          # characters of a list card's entry line that fit before the game cuts it off


def entry_name(name, detail='', start=None):
    """A list card entry: the event's name, wrapped at a word onto a second line when too long, then its part (Solo 2,
    Co-op) and month on a smaller line. The game cuts an entry off after about 33 characters a line; it shows a line
    break and <size> (as the Escalation cards' entries)."""
    lines = textwrap.wrap(name, ENTRY_LINE) or ['']
    lines = lines[:1] + [' '.join(lines[1:])] if len(lines) > 1 else lines
    small = ' '.join(x for x in (detail, f'({month(start)})' if start else '') if x)
    return '\n'.join(lines) + (f'\n<size=75%>{small}</size>' if small else '')


class Build:
    """The tables being built, the stock tables they came from, and the names the new rows need."""

    def __init__(self, recipe, source, tables, stock_text=None):
        self.recipe, self.source, self.tables = recipe, source, tables
        self.stock_text = stock_text or (lambda table: {})
        self.names = {}
        self.coop_done = set()                         # events whose co-op fights have solo stages

    def need(self, what, *tables):
        missing = [table for table in tables if table not in self.tables]
        if missing:
            raise ValueError(f'Offline MasterData recipe lists no {missing} for {what}')

    def ids(self, table):
        return {row[0]: row for row in self.tables[table]}

    def name(self, table, kind, number, value):
        self.names.setdefault(table, {})[text_id(kind, number)] = value

    def starts(self):
        """event id -> earliest start of its display window (m_condition type 2) in the stock tables."""
        sets = {}
        for row in self.source('m_condition_set_rel'):
            sets.setdefault(row[0], []).append(row[1])
        conditions = {row[0]: row for row in self.source('m_condition')}
        found = {}
        for event in self.source('m_event_base'):
            times = [conditions[c][5] for c in sets.get(event[5], ())
                     if c in conditions and conditions[c][1] == 2 and conditions[c][5]]
            if times:
                found[event[0]] = min(times)
        return found

    def newest_first(self, event_ids):
        starts = self.starts()
        return sorted(event_ids, key=lambda e: (e not in starts, -starts.get(e, 0), -e))

    def card(self, spec, order, section=0):
        """An always-shown Events-list card: a copy of its page event with its own id, place, names and art."""
        cid = spec['card']
        if not 9900 <= cid < 10000:
            raise ValueError(f'card id {cid} is outside 9900-9999')
        events = self.ids('m_event_base')
        if cid in events:
            raise conflict(f'card id {cid} is already an event')
        if spec['page'] not in events:
            raise incomplete(f'card page event {spec["page"]} is missing')
        row = list(events[spec['page']])
        row[0], row[2], row[3], row[4], row[6] = cid, order, 0, section, 0
        row[5] = row[7] = SHOWN
        row[10], row[26], row[27] = text_id('name', cid), text_id('label', cid), text_id('subtitle', cid)
        if spec.get('art'):
            if spec['art'] not in events:
                raise incomplete(f'card art event {spec["art"]} is missing')
            for column in ART:                         # inside art is the tile's picture (some events leave col 25 at 0)
                row[column] = events[spec['art']][12]
        self.tables['m_event_base'].append(row)
        self.name('m_event_base', 'name', cid, spec['title'])
        self.name('m_event_base', 'label', cid, spec['title'])
        self.name('m_event_base', 'subtitle', cid, '')
        return row

    def hide(self, event_ids):
        events = self.ids('m_event_base')
        for event_id in event_ids:
            events[event_id][5] = HIDDEN

    def event_name(self, event):
        return clean(self.stock_text('m_event_base').get(event[10], ''))


# ---------------------------------------------------------------- list cards
def build_list_cards(b):
    if not b.recipe.get('list_cards'):
        return
    b.need('list cards', 'm_event_base', 'm_event_solo_area_group', 'm_event_solo_area', 'm_event_area_category',
           'm_event_mission_group_rel')
    stock_events = {row[0]: row for row in b.source('m_event_base')}
    groups_of, areas_of = {}, {}
    for row in b.source('m_event_solo_area_group'):
        groups_of.setdefault(row[1], []).append(row)
    for row in b.source('m_event_solo_area'):
        areas_of.setdefault(row[1], []).append(row)
    starts = b.starts()
    story_stages = {row[11] for row in b.source('m_event_scenario_episode')}   # a story chapter's fights are area stages
    stages_of = {}
    for row in b.source('m_event_solo_battle'):
        stages_of.setdefault(row[2], []).append(row[0])

    chapters = {}
    for row in b.source('m_event_scenario_episode'):
        chapters[row[1]] = chapters.get(row[1], 0) + 1

    def has_story(event):
        """The event has a story chapter (m_event_base col 4 -> m_event_scenario_section, with episodes)."""
        return bool(event[4]) and chapters.get(event[4], 0) > 0

    def part_name(area_id):
        """Square leaves some areas unnamed (the trial-type crossovers); others name the same split Story / Solo."""
        stages = stages_of.get(area_id, ())
        return 'Story' if stages and all(s in story_stages for s in stages) else 'Solo'

    def copy_areas(cid, event_id, areas):
        """Copies of areas for the list card, made from the patched rows: same battles, names, order, rewards and skip.
        A stage that needs an earlier one needs that one's copy. Copy areas take the card's ids (card * 1000 + 901...)
        and copy stages the event's (event * 1000 + 901...), so a stage id // 1000 still names its event for missions;
        copied_stages pairs them back."""
        area_ids = {a[0] for a in b.tables['m_event_solo_area']}
        stage_ids = {s[0] for s in b.tables['m_event_solo_battle']}
        copies, n = [], 0
        for area in areas:
            area_id = cid * 1000 + 901 + sum(1 for a in area_ids if cid * 1000 + 900 < a < cid * 1000 + 1000)
            stages = sorted((s for s in b.tables['m_event_solo_battle'] if s[2] == area[0]), key=lambda s: (s[4], s[0]))
            ids = {}
            for stage in stages:
                n += 1
                ids[stage[0]] = event_id * 1000 + 900 + n
            if n >= 100:
                raise conflict(f'list card copies of {event_id} need more than 99 stage ids')
            if area_id in area_ids or stage_ids & set(ids.values()):
                raise conflict(f'list card copy {area_id} of {event_id} is already used')
            row = list(area)
            row[0] = area_id
            b.tables['m_event_solo_area'].append(row)
            area_ids.add(area_id)
            for stage in stages:
                stage = list(stage)
                stage[0], stage[2], stage[3] = ids[stage[0]], area_id, ids.get(stage[3], 0)
                b.tables['m_event_solo_battle'].append(stage)
            copies.append(row)
        return copies

    for spec in b.recipe['list_cards']:
        cid = spec['card']
        if cid in stock_events:
            raise conflict(f'list card id {cid} is already an event')
        if spec['page'] not in stock_events:
            raise incomplete(f'list card page event {spec["page"]} is missing')
        held, stays = {}, set()
        for event_id in spec['events']:
            if event_id not in stock_events:
                raise incomplete(f'list card event {event_id} is missing')
            areas = [area for group in sorted(groups_of.get(event_id, ()), key=lambda g: (g[2], g[0]))
                     for area in sorted(areas_of.get(group[0], ()), key=lambda a: (a[2], a[0]))]
            if spec.get('stories'):
                # its own card stays: with the areas its story chapter fights, or its first area when it has no
                # chapter in these tables (a card with no area behind it hangs the game); the rest go on the list.
                # Players (10-15) missed the cards of Beach Festival, One-Winged Angel, YoRHa, Open Bridal Audition,
                # Portal Battle: Angeal Penance, whose stories these tables lack, so every member keeps its card.
                own = ([area for area in areas if part_name(area[0]) == 'Story'] if has_story(stock_events[event_id]) else []) or areas[:1]
                stays.add(event_id)
                areas = [area for area in areas if area not in own]
                if event_id in spec.get('copies', ()):  # a crossover (Trial) card hangs the game without its solo area
                    areas = copy_areas(cid, event_id, areas)
            elif event_id in spec.get('copies', ()):
                # its own card stays with every area (its page, and the ticket exchange on it); the list gets copies
                stays.add(event_id)
                areas = copy_areas(cid, event_id, areas)
            if areas:                                  # an event with nothing to play is left out
                held[event_id] = areas
        members = b.newest_first(held)
        events = b.ids('m_event_base')
        order = min(events[e][2] for e in members) - 1 if members else events[spec['page']][2]
        if spec.get('front'):                          # ahead of every event instead of beside its newest member
            order = min(row[2] for row in events.values()) - 1
        card_bonus(b, b.card(spec, order), members)
        page_group = {row[1]: row for row in b.tables['m_event_solo_area_group']}.get(spec['page'])
        group = list(page_group or [0, 0, 1, 0, 0, 0])
        group[0:4] = [cid, cid, 1, text_id('name', cid)]
        b.tables['m_event_solo_area_group'].append(group)
        b.name('m_event_solo_area_group', 'name', cid, spec['title'])
        b.tables['m_event_area_category'].append([cid, cid, text_id('category', cid)])
        b.name('m_event_area_category', 'category', cid, '')
        live = b.ids('m_event_solo_area')
        area_names = b.stock_text('m_event_solo_area')
        entries = [(event_id, area) for event_id in members for area in held[event_id]]
        seen = {}                                      # (event, area name) -> times used: Square repeats "Solo"
        for position, (event_id, area) in enumerate(entries, 1):
            row = live[area[0]]
            row[1], row[2], row[6], row[7] = cid, position, text_id('entry', cid * 1000 + position), cid
            detail = ''
            if len(held[event_id]) > 1:
                part = area_names.get(area[6]) or part_name(area[0])
                seen[event_id, part] = seen.get((event_id, part), 0) + 1
                detail = part + (f' {seen[event_id, part]}' if seen[event_id, part] > 1 else '')
            # Square reused names for re-runs: the month tells them apart
            label = entry_name(b.event_name(stock_events[event_id]), detail, starts.get(event_id))
            b.name('m_event_solo_area', 'entry', cid * 1000 + position, label)
        b.hide([e for e in members if e not in stays])
        linked = {(row[0], row[1]) for row in b.tables['m_event_mission_group_rel']}
        for event_id, group_id in [tuple(row) for row in b.source('m_event_mission_group_rel')]:
            if event_id in held and (cid, group_id) not in linked:
                b.tables['m_event_mission_group_rel'].append([cid, group_id])
                linked.add((cid, group_id))


def card_bonus(b, card, members):
    """#104: the party screen shows the event bonus of the card it was opened from (m_event_base col 21
    EventBonusEffectGroupId), so a list card gets a group of its own (its id) holding copies of its members' bonus rows
    (m_event_bonus_effect, ids card * 1000 + 1...); the screen keeps the rows whose item the stage drops."""
    b.need('list card event bonuses', 'm_event_bonus_effect')
    stock_events = {row[0]: row for row in b.source('m_event_base')}
    cid = card[0]
    taken = {row[0] for row in b.tables['m_event_bonus_effect']}
    groups = [stock_events[e][21] for e in members if len(stock_events[e]) > 21 and stock_events[e][21]]
    rows = [list(row) for group in groups for row in b.source('m_event_bonus_effect') if row[1] == group]
    if len(rows) > 999:
        raise conflict(f'list card {cid} has more than 999 event bonus rows')
    for n, row in enumerate(rows, 1):
        row[0], row[1] = cid * 1000 + n, cid
        if row[0] in taken:
            raise conflict(f'event bonus row {row[0]} for list card {cid} is already used')
        b.tables['m_event_bonus_effect'].append(row)
    card[21] = cid if rows else 0


def copied_stages(recipe, groups, areas, stages):
    """{copy stage id: original stage id} for the list cards' "copies", read from built m_event_solo_area_group,
    m_event_solo_area and m_event_solo_battle rows: a copy is event * 1000 + 901..., matched to the stage of that
    event's own areas that has the same columns apart from id, area and premise."""
    events = {e for spec in recipe.get('list_cards', ()) for e in spec.get('copies', ())}
    group_event = {g[0]: g[1] for g in groups}
    own = {a[0] for a in areas if group_event.get(a[1]) in events}
    key = lambda s: (s[1], *s[4:])
    originals, pairs = {}, {}
    for stage in sorted(stages, key=lambda s: s[0]):
        if stage[2] in own and stage[0] % 1000 <= 900:
            originals.setdefault((stage[0] // 1000, key(stage)), []).append(stage[0])
    for stage in sorted(stages, key=lambda s: s[0]):
        if stage[0] // 1000 in events and stage[0] % 1000 > 900:
            match = originals.get((stage[0] // 1000, key(stage)))
            if match:
                pairs[stage[0]] = match.pop(0)
    return pairs


# ---------------------------------------------------------------- mock guild battles
# m_event_guild_ranking_battle: 0 Id, 1 EventBaseId (the guild battle), 2 stage number, 3 BattleId. One entry per
# guild battle, highest number first; a guild battle whose fights repeat an earlier one (Square lists #24 twice) is left
# out. Stages copy a plain solo stage, cost nothing and pay nothing. Each fight gets a battle effect from the recipe
# ("buffs") so one party can beat a boss made for a whole guild: party attack up, plus attack up in each element the
# boss is weak to (m_enemy col 19 -> m_resist_element, a negative per-mille resistance).
def guild_entries(b):
    fights = {}
    for row in b.source('m_event_guild_ranking_battle'):
        fights.setdefault(row[1], []).append(row)
    entries, seen = [], set()
    for event_id in sorted(fights, key=lambda e: (-(e % 100), e)):
        rows = sorted(fights[event_id], key=lambda r: (r[2], r[0]))
        battles = tuple(r[3] for r in rows)
        if battles not in seen:
            seen.add(battles)
            entries.append((event_id, rows))
    if not entries:
        raise incomplete('no guild battle fights for the guild card')
    return entries


def boss_of(b):
    """BattleId -> the boss of its last wave (StatusDisplayType 2, else the wave's first enemy), or None."""
    waves, groups = {}, {}
    for row in b.source('m_battle_wave'):
        waves.setdefault(row[0], []).append(row)
    for row in b.source('m_battle_enemy'):
        groups.setdefault(row[0], []).append(row)
    enemies = {row[0]: row for row in b.source('m_enemy')}
    battles = {row[0]: row for row in b.source('m_battle')}
    def boss(battle_id):
        battle = battles.get(battle_id)
        if not battle or battle[2] not in waves:
            return None
        last = max(waves[battle[2]], key=lambda w: w[1])
        members = groups.get(last[2], [])
        large = [e for e in members if e[5] == 2] or members
        return enemies.get(large[0][2]) if large else None
    return boss


def build_guild_card(b):
    spec = b.recipe.get('guild_card')
    if not spec:
        return
    entries = guild_entries(b)
    b.need('the guild card', 'm_event_base', 'm_event_solo_area_group', 'm_event_solo_area', 'm_event_solo_battle',
           'm_event_area_category', 'm_battle', 'm_battle_field_effect', 'm_battle_field_effect_group',
           'm_battle_field_effect_group_rel')
    cid = spec['card']
    events = b.ids('m_event_base')
    after = events.get(spec.get('after'))
    b.card(spec, after[2] if after else events[spec['page']][2])
    b.tables['m_event_solo_area_group'].append([cid, cid, 1, text_id('name', cid), 0, 0])
    b.name('m_event_solo_area_group', 'name', cid, spec['title'])
    b.tables['m_event_area_category'].append([cid, cid, text_id('category', cid)])
    b.name('m_event_area_category', 'category', cid, '')
    page_groups = {g[0] for g in b.tables['m_event_solo_area_group'] if g[1] == spec['page']}
    page_area = next((a for a in b.tables['m_event_solo_area'] if a[1] in page_groups), None)
    template = next((s for s in b.source('m_event_solo_battle') if s[0] == spec['stage_template']), None)
    if page_area is None or template is None:
        raise incomplete('guild card templates are missing')
    taken = {s[0] for s in b.tables['m_event_solo_battle']} | {a[0] for a in b.tables['m_event_solo_area']}
    battles = b.ids('m_battle')
    weak = {}
    for row in b.source('m_resist_element'):
        weak.setdefault(row[0], {})[row[1]] = row[2]
    boss = boss_of(b)
    enemy_names = b.stock_text('m_enemy')
    buffs = spec.get('buffs') or {}
    effect_groups = {row[0] for row in b.tables['m_battle_field_effect_group']}
    n = 0
    for position, (event_id, fights) in enumerate(entries, 1):
        area_id = cid * 1000 + position
        if area_id in taken:
            raise conflict(f'guild card area {area_id} is already used')
        area = list(page_area)
        area[0:9] = [area_id, cid, position, area[3], 0, 0, text_id('entry', cid * 1000 + position), cid, False]
        b.tables['m_event_solo_area'].append(area)
        bosses = []
        for number, fight in enumerate(fights, 1):
            n += 1
            stage_id = cid * 1000 + 100 + n
            if stage_id in taken:
                raise conflict(f'guild card stage {stage_id} is already used')
            stage = list(template)
            stage[0:14] = [stage_id, 1, area_id, 0, number, fight[3], 1, 0, 0, 0, 0, 0, 0, text_id('stage', stage_id)]
            stage[24], stage[25], stage[27], stage[28] = 0, False, 0, False
            b.tables['m_event_solo_battle'].append(stage)
            enemy = boss(fight[3])
            label = enemy_names.get(enemy[5], '') if enemy else ''
            bosses.append(label)
            b.name('m_event_solo_battle', 'stage', stage_id, f'{label or "Boss"} (Stage {number})')
            battle = battles.get(fight[3])
            if buffs and battle:
                buff_battle(b, battle, spec['effect_groups'] + n, buffs, event_id, enemy, weak, effect_groups)
        title = f'#{event_id % 100}'
        b.name('m_event_solo_area', 'entry', cid * 1000 + position, f'{title}: {bosses[0]}' if bosses and bosses[0] else title)


def buff_battle(b, battle, group, buffs, event_id, enemy, weak, effect_groups):
    if group in effect_groups:
        raise conflict(f'battle effect group {group} is already used')
    attack = buffs['last_attack'] if event_id == buffs.get('last_event') and 'last_attack' in buffs else buffs['attack']
    effects = [(buffs['attack_effect'], attack)]
    by_element = {int(element): effect for element, effect in buffs['element_effects'].items()}
    for element, permil in sorted(weak.get(enemy[19], {}).items() if enemy else ()):
        if permil < 0 and element in by_element:
            effects.append((by_element[element], buffs['element']))
    b.tables['m_battle_field_effect_group'].append([group])
    for k, (kind, value) in enumerate(effects):
        b.tables['m_battle_field_effect'].append([group * 10 + k, kind, 2, True, value, 0, False, 0, 0])
        b.tables['m_battle_field_effect_group_rel'].append([group, group * 10 + k, k + 1])
    battle[21] = group                                  # BattleFieldEffectGroupId


# ---------------------------------------------------------------- escalation cards
# An Escalation event is one with a ladder: a stage with a BossChallengeId (m_event_solo_battle col 22) in an area that
# shows the level panel (m_event_solo_area col 8). Its ladder areas move to the solo card; its co-op areas
# (m_event_multi_area via m_event_multi_battle col 23) are copied to the co-op card as solo areas. Extreme's ladders
# get their own group and area category on the solo card and come first. As on Square's escalation pages, the card's
# own section holds one battle episode per trial stage (the ladder area's stage without a BossChallengeId).
def escalation_layout(b, spec):
    stock_events = {row[0]: row for row in b.source('m_event_base')}
    owner = {row[0]: row[1] for row in b.source('m_event_solo_area_group')}
    areas = {row[0]: row for row in b.source('m_event_solo_area')}
    ladders = {}
    for stage in b.source('m_event_solo_battle'):
        if len(stage) > 22 and stage[22]:
            area = areas.get(stage[2])
            if area is None or not area[8]:
                raise incomplete(f'ladder stage {stage[0]} has no ladder area')
            ladders.setdefault(owner[area[1]], {})[area[0]] = area
    events = b.newest_first(ladders)
    if not events:
        raise incomplete('no Escalation events')
    wrong = [e for e in events if stock_events[e][1] != 3]
    if wrong:
        raise incomplete(f'Escalation event {wrong[0]} is type {stock_events[wrong[0]][1]}')
    extreme = spec['extreme']['event']
    if extreme not in ladders:
        raise incomplete(f'Extreme event {extreme} has no ladders')
    solo = [(e, a) for e in [extreme] + [e for e in events if e != extreme]
            for a in sorted(ladders[e].values(), key=lambda a: (a[2], a[0]))]
    multi = {row[0]: row for row in b.source('m_event_multi_area')}
    coop = []
    for e in events:
        held = {battle[23] for battle in b.source('m_event_multi_battle') if battle[2] == e}
        coop += [(e, multi[i]) for i in sorted(held, key=lambda i: (multi[i][1], i))]
    return events, solo, coop


def boss_label(b, event, area, ladders, short):
    label = b.event_name(event)
    if short and label.startswith('Escalation Challenge: '):
        label = label[len('Escalation Challenge: '):]
    if ladders > 1:                                     # Extreme: each ladder named for its own boss
        own = b.stock_text('m_event_solo_area').get(area[6], '').strip()
        if own and own not in ('Solo', 'Co-op'):
            label = own
    return label


def build_escalation_cards(b):
    spec = b.recipe.get('escalation_cards')
    if not spec:
        return
    events, solo, coop = escalation_layout(b, spec)
    b.need('the Escalation cards', 'm_event_base', 'm_event_solo_area_group', 'm_event_solo_area',
           'm_event_area_category', 'm_event_scenario_section', 'm_event_scenario_episode',
           'm_event_mission_group_rel', 'm_event_multi_area', 'm_event_multi_battle', 'm_event_solo_battle')
    current = b.ids('m_event_base')
    after = current.get(spec.get('after'))
    first = after[2] + 1 if after is not None else min(current[e][2] for e in events) - 2
    member_rels = [tuple(row) for row in b.source('m_event_mission_group_rel') if row[0] in set(events)]
    sections = {row[0] for row in b.tables['m_event_scenario_section']}
    episodes = {row[0] for row in b.tables['m_event_scenario_episode']}
    groups = b.ids('m_event_solo_area_group')
    starts = b.starts()
    stock_events = {row[0]: row for row in b.source('m_event_base')}

    def dated(label, event_id):
        return label + (f"\n<size=75%>({month(starts[event_id])})</size>" if starts.get(event_id) else '')

    def card(part, order):
        cid = part['card']
        if cid in sections or cid in groups:
            raise conflict(f'card id {cid} is already used')
        card_bonus(b, b.card(part, order, section=cid), events)
        b.tables['m_event_scenario_section'].append([cid, 1, 0, 0, 0, 0])
        b.tables['m_event_area_category'].append([cid, cid, text_id('category', cid)])
        b.name('m_event_area_category', 'category', cid, part['title'])
        b.tables['m_event_mission_group_rel'] += [[cid, group] for _, group in member_rels]
        return cid

    cid = card(spec['solo'], first)
    xid = spec['extreme']['group']
    if xid in groups:
        raise conflict(f'group id {xid} is already used')
    newest = next(g for g in b.tables['m_event_solo_area_group'] if g[1] == events[0])
    for gid, position, text in ((cid, 2, text_id('name', cid)), (xid, 1, text_id('group', xid))):
        group = list(newest)
        group[0], group[1], group[2], group[3], group[5] = gid, cid, position, text, 0
        b.tables['m_event_solo_area_group'].append(group)
    b.name('m_event_solo_area_group', 'name', cid, spec['solo']['title'])
    b.name('m_event_solo_area_group', 'group', xid, spec['extreme']['title'])
    b.tables['m_event_area_category'].append([xid, cid, text_id('category', xid)])
    b.name('m_event_area_category', 'category', xid, spec['extreme']['title'])
    live = b.ids('m_event_solo_area')
    stages = {}
    for stage in b.tables['m_event_solo_battle']:
        stages.setdefault(stage[2], []).append(stage)
    ladders = {}
    for event_id, _ in solo:
        ladders[event_id] = ladders.get(event_id, 0) + 1
    n = 0
    for position, (event_id, area) in enumerate(solo, 1):
        home = xid if event_id == spec['extreme']['event'] else cid
        row = live[area[0]]
        row[1], row[2], row[4], row[6], row[7] = home, position, 0, text_id('entry', cid * 1000 + position), home
        b.name('m_event_solo_area', 'entry', cid * 1000 + position,
               dated(boss_label(b, stock_events[event_id], area, ladders[event_id], True), event_id))
        for stage in sorted((s for s in stages.get(area[0], ()) if not s[22]), key=lambda s: s[4]):
            n += 1
            if cid * 1000 + n in episodes:
                raise conflict(f'episode id {cid * 1000 + n} is already used')
            b.tables['m_event_scenario_episode'].append([cid * 1000 + n, cid, 0, 0, n, 0, 1, 1, False, 0, 3, stage[0], 0, 0])
    coop_id = card(spec['coop'], first + 1)
    if b.recipe.get('coop_as_solo', True):          # the co-op fights as solo stages, one area each (coop_solo)
        group = list(newest)
        group[0], group[1], group[2], group[3], group[5] = coop_id, coop_id, 1, text_id('name', coop_id), 0
        b.tables['m_event_solo_area_group'].append(group)
        b.name('m_event_solo_area_group', 'name', coop_id, spec['coop']['title'])
        ladder_of = {event_id: area for event_id, area in reversed(solo)}
        for position, (event_id, area) in enumerate(coop, 1):
            coop_solo(b, event_id, area, coop_id * 1000 + position, live[ladder_of[event_id][0]], coop_id, position,
                      coop_id, dated(boss_label(b, stock_events[event_id], area, 1, False), event_id))
    else:
        multi = b.ids('m_event_multi_area')
        for position, (event_id, area) in enumerate(coop, 1):
            row = multi[area[0]]
            row[1], row[2], row[3], row[4] = position, text_id('entry', coop_id * 1000 + position), coop_id, 0
            b.name('m_event_multi_area', 'entry', coop_id * 1000 + position,
                   dated(boss_label(b, stock_events[event_id], area, 1, False), event_id))
        for battle in b.tables['m_event_multi_battle']:
            if battle[2] in set(events):
                battle[2] = coop_id
    b.hide(events)


# ---------------------------------------------------------------- co-op as solo
# Co-op fights (m_event_multi_battle) ran on Square's battle server, so with "coop_as_solo" (gameplay-settings.json,
# on unless false) each event's co-op area becomes a solo area: an entry on its list card's co-op card (the list
# card's "coop"), else after the event's last solo area: after its copy on the list card for an event copied there
# without "stories" (Critical Threats), else on its own page when that is shown, else on the list card its areas moved
# to (Escalation's on the Escalation Co-op card). It has
# the co-op area's display and lock, and one stage per co-op battle in difficulty order, paying the battle's
# first-clear reward. Stage ids stay in the event's range (event * 1000 + 801...) for its missions. The co-op
# originals, which the server refuses, are hidden (display condition HIDDEN on every co-op area and battle).
# m_event_multi_battle: 0 Id, 1 DifficultyLevel, 2 EventBaseId, 4 BattleId, 5 BattleRankGroupId, 6 FirstRewardSetId,
# 10 StaminaCost, 12 DisplayConditionSetId, 13 LockConditionSetId, 14 ChallengeCountMax, 15 ChallengeResetId,
# 17-19 resources and story exp, 23 EventMultiAreaId, 24-28 as m_event_solo_battle 23-27.
# m_event_multi_area: 0 Id, 1 OrderNo, 2 NameLanguageId, 3 EventAreaCategoryId, 4 DisplayConditionSetId, 5 Lock.
COOP_STAGES = 800


def coop_solo(b, event_id, multi_area, area_id, template, group, order, category, label):
    """One solo area copying an event's co-op area, next to template (a solo area row of the event). A fight shown or
    unlocked by clearing another co-op fight (condition type 3, col 11 = 37) needs that fight's copy instead; one
    unlocked by a story episode (23) keeps its lock; time windows (type 2), long over, are dropped."""
    if area_id in b.ids('m_event_solo_area'):
        raise conflict(f'co-op area {area_id} is already used')
    stages = sorted((s for s in b.tables['m_event_solo_battle']
                     if s[0] // 1000 == event_id and s[0] % 1000 < COOP_STAGES), key=lambda s: s[0])
    if not stages:
        raise incomplete(f'event {event_id} has no solo stage to copy for its co-op fights')
    fights = sorted((f for f in b.source('m_event_multi_battle') if f[2] == event_id and f[23] == multi_area[0]),
                    key=lambda f: (f[1], f[0]))
    if len(fights) > 99:
        raise conflict(f'event {event_id} has more than 99 co-op fights')
    ids = {f[0]: event_id * 1000 + COOP_STAGES + n for n, f in enumerate(fights, 1)}
    taken = {s[0] for s in b.tables['m_event_solo_battle']}
    if taken & set(ids.values()):
        raise conflict(f'co-op stages of {event_id} are already used')
    rel = {}
    for row in b.source('m_condition_set_rel'):
        rel.setdefault(row[0], []).append(row[1])
    conditions = {row[0]: row for row in b.source('m_condition')}

    def after(set_id):                                 # (premise, lock to keep) for a condition set
        found = [conditions[c] for c in rel.get(set_id, ()) if c in conditions]
        needs = [c[12] for c in found if c[1] == 3 and c[11] == 37]
        if needs:
            return ids.get(needs[0], 0), 0
        return 0, set_id if found and all(c[1] == 3 and c[11] == 23 for c in found) else 0

    row = list(template)
    row[0:9] = [area_id, group, order, template[3], multi_area[4], multi_area[5], text_id('entry', area_id), category,
                False]
    b.tables['m_event_solo_area'].append(row)
    b.name('m_event_solo_area', 'entry', area_id, label)
    boss, enemy_names = boss_of(b), b.stock_text('m_enemy')
    for n, fight in enumerate(fights, 1):
        shown_after, _ = after(fight[12])
        premise, lock = after(fight[13])
        stage = list(stages[0])
        stage[0:14] = [ids[fight[0]], 1, area_id, premise or shown_after, n, fight[4], fight[5], fight[6], fight[10], 0,
                       lock, fight[14], fight[15], text_id('stage', ids[fight[0]])]
        stage[14:17] = fight[17:20]
        stage[22] = 0
        stage[23:28] = fight[24:29]
        stage[28] = False
        b.tables['m_event_solo_battle'].append(stage)
        enemy = boss(fight[4])
        name = clean(enemy_names.get(enemy[5], '')) if enemy else ''
        b.name('m_event_solo_battle', 'stage', ids[fight[0]], f'{name or "Co-op"} (Level {fight[1]})')
    b.coop_done.add(event_id)


def coop_cards(b, with_coop, stock_events):
    """The list cards' "coop" cards (a list card's co-op fights on a card of their own, beside it): {event: (card,
    position)} for its members with co-op fights, newest first, as on the list card."""
    placed = {}
    for spec in b.recipe.get('list_cards', ()):
        part = spec.get('coop')
        members = b.newest_first([e for e in spec['events'] if e in with_coop and e in stock_events])
        if not part or not members:
            continue
        cid = part['card']
        events = b.ids('m_event_base')
        b.card(dict(part, page=spec['page'], art=part.get('art', spec.get('art'))), events[spec['card']][2] + 1)
        page_group = {row[1]: row for row in b.tables['m_event_solo_area_group']}.get(spec['page'])
        group = list(page_group or [0, 0, 1, 0, 0, 0])
        group[0:4] = [cid, cid, 1, text_id('name', cid)]
        b.tables['m_event_solo_area_group'].append(group)
        b.name('m_event_solo_area_group', 'name', cid, part['title'])
        b.tables['m_event_area_category'].append([cid, cid, text_id('category', cid)])
        b.name('m_event_area_category', 'category', cid, '')
        linked = {(row[0], row[1]) for row in b.tables['m_event_mission_group_rel']}
        for event_id, group_id in [tuple(row) for row in b.source('m_event_mission_group_rel')]:
            if event_id in members and (cid, group_id) not in linked:
                b.tables['m_event_mission_group_rel'].append([cid, group_id])
                linked.add((cid, group_id))
        placed.update({e: (cid, position) for position, e in enumerate(members, 1)})
    return placed


def build_coop_solo(b):
    if not b.recipe.get('coop_as_solo', True) or not wanted(b.recipe):
        return
    b.need('co-op as solo', 'm_event_solo_area', 'm_event_solo_battle', 'm_event_multi_area', 'm_event_multi_battle')
    stock_events = {row[0]: row for row in b.source('m_event_base')}
    owner = {row[0]: row[1] for row in b.source('m_event_solo_area_group')}
    events = b.ids('m_event_base')
    groups = b.ids('m_event_solo_area_group')
    live = b.ids('m_event_solo_area')
    multi = {row[0]: row for row in b.source('m_event_multi_area')}
    starts = b.starts()
    areas_of = {}
    for area in b.source('m_event_solo_area'):
        areas_of.setdefault(owner.get(area[1]), []).append(live[area[0]])
    with_coop = {f[2] for f in b.source('m_event_multi_battle')}
    on_coop_card = coop_cards(b, with_coop, stock_events)
    on_card_events = {}                              # co-op card -> its events, for their event bonuses
    copied = {e for spec in b.recipe.get('list_cards', ()) if not spec.get('stories') for e in spec.get('copies', ())}
    for event_id, area_id in sorted({(f[2], f[23]) for f in b.source('m_event_multi_battle')}):
        if event_id in b.coop_done or event_id not in stock_events or not areas_of.get(event_id):
            continue
        area = multi.get(area_id, [area_id, 0, 0, 0, 0, 0])   # older events list co-op fights with no co-op area (0)
        if event_id in on_coop_card:                 # its list card's co-op card, an entry per event
            cid, position = on_coop_card[event_id]
            label = entry_name(b.event_name(stock_events[event_id]), '', starts.get(event_id))
            last = max(areas_of[event_id], key=lambda a: (a[2], a[0]))
            coop_solo(b, event_id, area, 98000000 + event_id * 10, last, cid, position, cid, label)
            on_card_events.setdefault(cid, []).append(event_id)
            continue
        home = [a for a in areas_of[event_id] if groups[a[1]][1] == event_id]
        if event_id in copied:                         # on the list after its copy, as before its own card came back
            on_card = [live[a] for a in sorted({st[2] for st in b.tables['m_event_solo_battle']
                                                if st[0] // 1000 == event_id and st[0] % 1000 > 900})]
        elif home and events[event_id][5] != HIDDEN:   # its own page is shown: there, where its co-op tab was
            on_card = []
        else:
            on_card = [a for a in areas_of[event_id] if 9900 <= groups[a[1]][1] < 10000]
        last = max(on_card or home or areas_of[event_id], key=lambda a: (a[2], a[0]))
        for row in b.tables['m_event_solo_area']:      # make room right after it
            if row[1] == last[1] and row[2] > last[2]:
                row[2] += 1
        label = 'Co-op'
        if on_card:                                    # a list entry names its event, as the list card's own do
            label = entry_name(b.event_name(stock_events[event_id]), 'Co-op', starts.get(event_id))
        coop_solo(b, event_id, area, 98000000 + event_id * 10, last, last[1], last[2] + 1, last[7], label)
    cards = {row[0]: row for row in b.tables['m_event_base']}
    for cid, members in on_card_events.items():        # its party screen shows its events' bonuses, as a list card's
        card_bonus(b, cards[cid], list(dict.fromkeys(members)))
    for fight in b.tables['m_event_multi_battle']:     # the co-op originals, which only refuse, are no longer shown
        fight[12] = HIDDEN
    for area in b.tables['m_event_multi_area']:
        area[4] = HIDDEN


# ---------------------------------------------------------------- event towers
# Each listed event becomes a tower in one more tower group placed after the groups already there, in the listed
# order. Floor n is the event's n-th stage by stage id (a Vortex Clash's normal stages, then its Brutal ones),
# fighting the stage's own battle and paying its first-clear reward. Nothing else is added. engine/rules.py counts
# floors already won as event stages at login.
def tower_plan(recipe, rows):
    """[(tower id, event id, [stage rows in floor order])] for recipe "event_towers"; rows(table) gives the rows."""
    spec = recipe.get('event_towers') or {}
    if not spec:
        return []
    owner = {group[0]: group[1] for group in rows('m_event_solo_area_group')}
    areas_of = {}
    for area in rows('m_event_solo_area'):
        areas_of.setdefault(owner.get(area[1]), set()).add(area[0])
    by_area = {}
    for stage in rows('m_event_solo_battle'):
        by_area.setdefault(stage[2], []).append(stage)
    plan = []
    for offset, event_id in enumerate(spec['events']):
        stages = sorted((s for a in areas_of.get(event_id, ()) for s in by_area.get(a, ())), key=lambda s: s[0])
        if not stages:
            raise incomplete(f'tower event {event_id} has no stages')
        plan.append((spec['first_tower'] + offset, event_id, stages))
    return plan


def build_event_towers(b):
    plan = tower_plan(b.recipe, b.source)
    if not plan:
        return
    b.need('the event towers', 'm_tower', 'm_tower_group', 'm_tower_floor', 'm_tower_floor_group', 'm_event_base',
           'm_battle')
    spec = b.recipe['event_towers']
    towers, floors = b.ids('m_tower'), b.ids('m_tower_floor')
    battles = b.ids('m_battle')
    stock_events = {row[0]: row for row in b.source('m_event_base')}
    groups = b.tables['m_tower_group']
    gid = max(g[0] for g in groups) + 1
    group = list(max(groups, key=lambda g: g[5]))
    group[0], group[1], group[5] = gid, text_id('tower_group', gid), max(g[5] for g in groups) + 1
    groups.append(group)
    b.name('m_tower_group', 'tower_group', gid, spec['title'])
    model = towers[spec['tower_template']]
    for k, (tid, event_id, stages) in enumerate(plan):
        if tid in towers:
            raise conflict(f'tower {tid} already exists')
        tower = list(model)
        tower[0], tower[1], tower[2], tower[3], tower[6], tower[8] = tid, gid, text_id('tower', tid), 0, len(stages), 0
        tower[7] = (model[7] - 2 + k) % 3 + 2              # the three tower models in turn
        b.tables['m_tower'].append(tower)
        b.names.setdefault('m_tower', {})[text_id('tower', tid)] = (
            b.stock_text('m_event_base').get(stock_events[event_id][10], '').replace('\\n', ' ').strip())
        b.tables['m_tower_floor_group'].append([tid * 100 + 1, tid, 0, 0])
        for seq, stage in enumerate(stages, 1):
            fid = tid * 1000 + seq
            if fid in floors:
                raise conflict(f'tower floor {fid} already exists')
            if stage[5] not in battles:
                raise incomplete(f'battle {stage[5]} is missing')
            floor = list(floors[spec['floor_template'] + min(seq, 100) - 1])
            kind = 3 if seq == len(stages) else 2 if stage[1] == 8 or seq % 10 == 0 else 1
            floor[0:10] = [fid, tid, seq, tid * 100 + 1, kind, stage[5], stage[7], 0, 0, text_id('floor', fid)]
            b.tables['m_tower_floor'].append(floor)
            b.name('m_tower_floor', 'floor', fid, f'B{seq}')
    b.hide(event_id for _, event_id, _ in plan)


# ---------------------------------------------------------------- victory draws
# Only a Critical Threat page (event type 2) has a Victory Draw button. It opens the event's draw group
# (m_event_base col 16 BoxGachaGroupId) and lists every draw of that group (m_box_gacha col 1), each with its own art,
# ticket and prizes, by OrderNo (col 6); the server keys draws by their own id. So every Critical Threat's draws join
# the hub's group, newest event first, and the hub (renamed from the recipe) is the one Critical Threat card shown: the
# others, whose fights are copied onto a list card, are hidden.
# The game's list picks the wrong draw once it scrolls; tools/scoreboss.patch_box_gacha_select fixes that.
def build_victory_draws(b):
    spec = b.recipe.get('victory_draws')
    if not spec:
        return
    b.need('the Victory Draws', 'm_event_base', 'm_box_gacha')
    events = b.ids('m_event_base')
    members = b.newest_first([row[0] for row in b.source('m_event_base') if row[1] == 2 and row[16]])
    hub = spec['hub']
    if hub not in members:
        raise incomplete(f'Victory Draw hub {hub} is not a Critical Threat with draws')
    group = events[hub][16]
    draws = {}
    for row in b.tables['m_box_gacha']:
        draws.setdefault(row[1], []).append(row)
    position = 0
    for event_id in members:
        for row in sorted(draws.get(events[event_id][16], ()), key=lambda r: (r[6], r[0])):
            row[1], row[6] = group, position
            position += 1
        events[event_id][16] = group
    events[hub][5] = events[hub][7] = SHOWN
    events[hub][10], events[hub][26], events[hub][27] = text_id('name', hub), text_id('label', hub), text_id('subtitle', hub)
    b.name('m_event_base', 'name', hub, spec['title'])
    b.name('m_event_base', 'label', hub, spec['title'])
    b.name('m_event_base', 'subtitle', hub, spec['title'])   # the card's big title (stock: the event's own)
    copied = {e for card in b.recipe.get('list_cards', ()) for e in card.get('copies', ())}
    b.hide(row[0] for row in b.source('m_event_base') if row[1] == 2 and row[0] in copied and row[0] != hub)


# ---------------------------------------------------------------- missions tab
# A card's Missions button hangs the game when the card has no mission group (m_event_mission_group_rel): every stock
# card has one. A card whose members have no missions (Frenzy Battles, Mock Guild Battles) gets an empty Event group,
# copied from its page's: m_mission_group 0 Id, 1 MissionGroupType (9 Event), 3 DisplayConditionSetId,
# 4 OpenConditionSetId, 5 LockConditionSetId, 7 NameLanguageId ("Events"); no m_mission_set row, so no missions.
def build_mission_tabs(b):
    specs = list(b.recipe.get('list_cards', ())) + ([b.recipe['guild_card']] if b.recipe.get('guild_card') else [])
    specs += [dict(c['coop'], page=c['page']) for c in b.recipe.get('list_cards', ()) if c.get('coop')]
    if not specs:
        return
    b.need('the cards\' missions', 'm_event_mission_group_rel', 'm_mission_group')
    groups = {row[0]: row for row in b.tables['m_mission_group']}
    linked = {row[0] for row in b.tables['m_event_mission_group_rel']}
    shown = b.ids('m_event_base')
    for spec in specs:
        cid = spec['card']
        if cid in linked or cid not in shown:
            continue
        page = [groups[r[1]] for r in b.tables['m_event_mission_group_rel'] if r[0] == spec['page'] and r[1] in groups]
        template = next((g for g in page if g[1] == 9), None) or next(g for g in groups.values() if g[1] == 9)
        group_id = 980000000 + cid
        if group_id in groups:
            raise conflict(f'card mission group {group_id} is already used')
        row = list(template)
        row[0], row[3], row[4], row[5] = group_id, 0, 0, 0
        b.tables['m_mission_group'].append(row)
        b.tables['m_event_mission_group_rel'].append([cid, group_id])


# ---------------------------------------------------------------- card order
# The Events list shows events by OrderNo (m_event_base col 2), lowest first; the stock events already run newest first.
# Recipe "card_order" lists the generated cards in the order they come first, ahead of every other shown event.
def build_card_order(b):
    order = b.recipe.get('card_order')
    if not order:
        return
    events = b.ids('m_event_base')
    order = [cid for cid in order if cid in events]     # a card not built (co-op cards with coop_as_solo off) is skipped
    first = min((row[2] for cid, row in events.items() if cid not in order and row[5] != HIDDEN), default=1000)
    for position, cid in enumerate(order):
        events[cid][2] = first - len(order) + position


BUILDERS = (build_list_cards, build_guild_card, build_escalation_cards, build_coop_solo, build_event_towers,
            build_victory_draws, build_mission_tabs, build_card_order)


def build(recipe, source, tables):
    """Add the recipe's cards and towers to the expected tables; returns the names the new rows need."""
    b = Build(recipe, source, tables)
    for builder in BUILDERS:
        builder(b)
    return b.names


def texts(recipe, source, stock_text):
    """{text table: {id: value}} for the recipe's cards and towers, built on a scratch copy of the stock tables."""
    if not wanted(recipe):
        return {}
    scratch = {table: copy.deepcopy(source(table)) for table in recipe['tables']}
    b = Build(recipe, source, scratch, stock_text)
    for builder in BUILDERS:
        builder(b)
    return b.names
