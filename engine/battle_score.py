"""engine/battle_score.py - per-fight score for Another dungeons (Criterion, Crisis, Dungeon Ranking).

Square scored every boss fight server-side from tables that were never shipped (m_battle_score points at rank-coefficient
and additional-score ids the game data does not hold). What IS known, from the 46 scored another/battle/end fights in
the Sept 30 / Oct 4 captures (score settings 1001/31):
  - final_score = death + max blow + battle time + taken damage + weak-element hits + break count (every fight).
  - a fight is scored only when its m_battle row has a BattleScoreId (col 18): Crisis normal fights have none and came
    back without score_result; Criterion fights and every boss have one.
  - difficulty-1 boss, everything S: death 1500, max blow ~1850, time 4840 - ~0.56/s, taken 3375 - ~0.03/point;
    Crisis Very Hard (2010301): death 9000, blow 9350, time 24840, taken 18375 (5.3x in total).
  - weak/break hits add a small amount that does not scale with difficulty (1:30, 2:37, 3:45 ... 47:145, cap 150).
  - one death: 40% of the death score (2170201 recording).
GUESSED (no recording exists): Dungeon Ranking fights (score settings 3212/4112, never recorded), and every difficulty
other than 1 and Crisis Very Hard. The scale m of a dungeon comes from its top m_another_dungeon_rank threshold, which
follows the same ratio in the recorded pair (Crisis Normal 110,000 -> 1.0, Very Hard 720,000 -> 5.32): m is linear in
that threshold through those two points, and each category is linear in m through its two recorded values.
Temple of the Ancients EX (3,000,000) -> m 21.5. Rank per category: S >= 90% of its full value, A 75, B 60, C 45,
D 30, E 15, else F."""
B1 = {"death": 1500, "blow": 1850, "time": 4840, "taken": 3375}        # difficulty-1 boss, all S
B3 = {"death": 9000, "blow": 9350, "time": 24840, "taken": 18375}      # Crisis Very Hard boss, all S
T1, T3, M3 = 110000, 720000, 61562 / 11565                              # their top rank thresholds; measured total ratio
HITS = [(0, 0), (1, 30), (2, 37), (3, 45), (4, 52), (6, 66), (8, 78), (11, 91), (21, 106), (47, 145), (67, 150)]
R_S, R_A, R_B, R_C, R_D, R_E, R_F = 7, 6, 5, 4, 3, 2, 1

def scale(top_threshold):
    """m for a dungeon whose highest rank needs top_threshold points (1.0 for a difficulty-1 Crisis dungeon)."""
    if not top_threshold: return 1.0
    return max(1.0, 1 + (top_threshold - T1) * (M3 - 1) / (T3 - T1))

def full(cat, m):
    return B1[cat] + (B3[cat] - B1[cat]) * (m - 1) / (M3 - 1)

def hits(n):
    if n <= 0: return 0
    for (a, x), (b, y) in zip(HITS, HITS[1:]):
        if n <= b: return round(x + (y - x) * (n - a) / (b - a))
    return HITS[-1][1]

def rank(frac):
    for lim, r in ((0.9, R_S), (0.75, R_A), (0.6, R_B), (0.45, R_C), (0.3, R_D), (0.15, R_E)):
        if frac >= lim: return r
    return R_F

def score(bi, battle_score_id, top_threshold):
    """bi: battle_input (attributes or dict); battle_score_id: m_battle col 18. -> BattleScoreResult fields, or None
    for a fight Square did not score."""
    if not battle_score_id: return None
    g = (lambda k: int(bi.get(k, 0) or 0)) if isinstance(bi, dict) else (lambda k: int(getattr(bi, k, 0) or 0))
    m = scale(top_threshold); sec = g("total_battle_msec") / 1000
    deaths, blow, taken = g("death_count"), g("max_blow_damage"), g("total_taken_damage")
    f_death = 0.4 ** deaths
    f_blow = 1.0 if blow > 0 else 0.0
    f_time = 1 - 0.000116 * sec if sec <= 90 else max(0.1, 0.9896 - (sec - 90) * 0.00375)
    f_taken = max(0.1, 1 - taken / (112000 / m ** 0.7))
    out = {}; tot_full = 0
    for cat, f, name in (("death", f_death, "death_count"), ("blow", f_blow, "max_blow_damage"),
                         ("time", f_time, "battle_time"), ("taken", f_taken, "taken_damage")):
        v = full(cat, m); tot_full += v
        out[f"{name}_score"] = round(v * f)
        out[f"{name}_score_battle_rank_type"] = rank(f)
    out["weak_element_hit_count_score"] = hits(g("weak_element_hit_count"))
    out["notes_break_count_score"] = hits(g("notes_break_count"))
    out["final_score"] = sum(out[k] for k in ("death_count_score", "max_blow_damage_score", "battle_time_score",
                                               "taken_damage_score", "weak_element_hit_count_score", "notes_break_count_score"))
    out["final_score_battle_rank_type"] = rank(out["final_score"] / tot_full)
    return out
