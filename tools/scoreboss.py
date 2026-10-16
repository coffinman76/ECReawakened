"""Dungeon Ranking bosses fight in the game instead of on a battle server (GitLab #49).

DungeonSceneBehaviour.SetupBattleAsync checks `cmp [display+0x20 dungeonType],3 ; jne` and, only for
DungeonType.ScoreDungeon (3), sets BattlePlayer.SetupParameter.usesBattleServer (+0x42) and asks the server for a
battle-server room (POST /api/pvt/room/solo/create). Nothing the server sends feeds that check, and there is no
battle server any more, so the boss shows Network Error / Under Maintenance. Turning that one `jne` (75) into `jmp`
(EB) makes the boss a normal local fight.

Game 4.0.0 (GameAssembly.dll md5 8e0cd9cc53e5debbd0ab88ee581a32d6): one match, file offset 0x10527C3
(RVA 0x10541C3). Other builds are found by the same byte pattern; anything other than exactly one match is left alone.
Only the protected copy is changed (never Steam's installation); the original is kept as GameAssembly.dll.steam.
"""
import bisect
import re
import shutil
from pathlib import Path

# cmp dword [rax+20h],3 ; jne/jmp +11h ; mov rcx,[rax+30h] ; test rcx,rcx ; je ... ; mov byte [rcx+42h],1
PATTERN = re.compile(rb'\x83\x78\x20\x03[\x75\xeb]\x11\x48\x8b\x48\x30\x48\x85\xc9\x0f\x84....\xc6\x41\x42\x01', re.S)
JNE, JMP = 0x75, 0xEB


def patch(game_folder):
    """Patch <game_folder>/GameAssembly.dll in place. Returns a short status string; never raises on file errors."""
    dll = Path(game_folder) / 'GameAssembly.dll'
    try:
        data = dll.read_bytes()
    except OSError as error:
        return f'scoreboss: cannot read {dll}: {error}'
    hits = [m.start() for m in PATTERN.finditer(data)]
    if len(hits) != 1:
        return f'scoreboss: {len(hits)} matches in {dll.name} (unknown build), left alone'
    at = hits[0] + 4
    if data[at] == JMP:
        return 'scoreboss: already patched'
    backup = dll.with_name('GameAssembly.dll.steam')
    try:
        if not backup.exists():
            shutil.copy2(dll, backup)
        with dll.open('r+b') as stream:
            stream.seek(at)
            stream.write(bytes([JMP]))
    except OSError as error:
        return f'scoreboss: cannot patch {dll} (is the game running?): {error}'
    return f'scoreboss: patched {dll.name} at 0x{at:X}'


# ---- Dungeon Ranking fight ends (GitLab #71, #72) ----
# With the boss fought in the game, DungeonTypeScoreDungeonBehaviour.BattleEndAsync still ends every fight the battle
# server way: event/score/dungeon/battle/end carries only the event id, the reply's battle_input is copied over the
# fight's own numbers (all 0 on the result screen, nothing for the server to score), and the party's HP, Limit, ATB,
# Ultimate Weapon gauge and uses are read back from user_another_dungeon_entry.battle_info, which only the battle server
# filled, so they reset after every fight.
# Crisis dungeons end their fights in the game: DungeonTypeCrisisBehaviour.BattleEndAsync keeps the party's numbers
# itself (SavePartyParameters) and sends another/battle/end with the fight's battle_input. Both behaviours share one
# base class and BattleEndAsync signature, so:
#   1. ScoreDungeon's BattleEndAsync starts with a jmp to Crisis's (5 bytes over `mov [rsp+8],rbx`).
#   2. Crisis's end passed a fixed DungeonType.Crisis (2) as RequestAnotherBattleEndAsync's dungeonType, used only to
#      read the run's score total before and after the fight. It now passes the request's own dungeon type
#      (parameter.dungeonType, from the behaviour's DungeonType): still 2 in a Crisis dungeon, 3 in Dungeon Ranking.
#      `mov dword [rsp+20h],2` -> `mov ecx,[rsi+3Ch] ; mov [rsp+20h],ecx ; nop` (ecx is free there: it was already
#      stored to [rsp+28h], and r9 = rax, the enemy results, is untouched).
# Game 4.0.0 layout: ScoreDungeon end wrapper file offset 0xF2FDD0, Crisis end wrapper 0xF2CF30, site 2 at 0xF31E6C
# (wrappers in one section, so the jmp is the file distance).
# Found by byte pattern and the PE's function table; anything other than exactly one match is left alone.
CRISIS_DUNGEON_ARG = re.compile(rb'\x8b\x4f\x40\x48\xc7\x44\x24\x30\x00\x00\x00\x00\x89\x4c\x24\x28'
                                rb'(\xc7\x44\x24\x20\x02\x00\x00\x00|\x8b\x4e\x3c\x89\x4c\x24\x20\x90)'
                                rb'\x4c\x8b\xc8\x4c\x8b\xc6\x48\x8b\xd3\x48\x8d\x4c\x24\x70\xe8', re.S)
OWN_DUNGEON_ARG = b'\x8b\x4e\x3c\x89\x4c\x24\x20\x90'
# in ScoreDungeon's BattleEndAsync state machine: GetPartyCharacterSaveParameterList(); SavePartyParameters(list);
# FromBattleInput(...)
SCORE_DUNGEON_END = re.compile(rb'\xe8....\x4d\x85\xe4\x0f\x84....\x45\x33\xc0\x48\x8b\xd0\x49\x8b\xcc\xe8....'
                               rb'\x45\x33\xc0\x48\x8b\x57\x58\x48\x8b\x4f\x38\xe8', re.S)
# the async wrapper's call into its state machine: mov rdx,[rax+38h] ; mov rdx,[rdx+8] ; call MoveNext
WRAPPER_CALL = b'\x48\x8b\x50\x38\x48\x8b\x52\x08\xe8'
WRAPPER_HEAD = b'\x48\x89\x5c\x24\x08'          # mov [rsp+8],rbx: exactly 5 bytes, overwritten by the jmp


class _Image:
    """Just enough of the PE: file offset <-> RVA, and the start of the function holding a file offset (.pdata)."""

    def __init__(self, data):
        self.data = data
        pe = int.from_bytes(data[0x3C:0x40], 'little')
        if data[pe:pe + 4] != b'PE\0\0':
            raise ValueError('not a PE file')
        sections, opt_size = int.from_bytes(data[pe + 6:pe + 8], 'little'), int.from_bytes(data[pe + 20:pe + 22], 'little')
        opt = pe + 24
        exc_rva, exc_size = (int.from_bytes(data[opt + 112 + 3 * 8 + i:opt + 112 + 3 * 8 + i + 4], 'little') for i in (0, 4))
        self.alignment = int.from_bytes(data[opt + 32:opt + 36], 'little')
        self.sections, self.code = [], []
        for i in range(sections):
            s = opt + opt_size + 40 * i
            vsize, va, rsize, raw, flags = (int.from_bytes(data[s + k:s + k + 4], 'little') for k in (8, 12, 16, 20, 36))
            self.sections.append((va, max(vsize, rsize), raw))
            if flags & 0x20000000:                    # IMAGE_SCN_MEM_EXECUTE
                self.code.append((raw, vsize, rsize, va, s + 8))
        start = self.offset(exc_rva)
        if start is None or not exc_size:
            raise ValueError('no function table')
        self.functions = sorted((int.from_bytes(data[p:p + 4], 'little'), int.from_bytes(data[p + 4:p + 8], 'little'))
                                for p in range(start, start + exc_size - 11, 12))

    def offset(self, rva):
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        return None

    def rva(self, offset):
        for va, size, raw in self.sections:
            if raw <= offset < raw + size:
                return offset - raw + va
        return None

    def function_start(self, offset):
        """-> file offset of the start of the function that holds `offset`, or None."""
        rva = self.rva(offset)
        i = bisect.bisect_right(self.functions, (rva, 1 << 32)) - 1
        if i < 0 or not self.functions[i][0] <= rva < self.functions[i][1]:
            return None
        return self.offset(self.functions[i][0])

    def wrapper_of(self, machine):
        """-> file offset of the one function that calls state machine `machine` the async-wrapper way, or None."""
        found = set()
        at = self.data.find(WRAPPER_CALL)
        while at >= 0:
            call = at + len(WRAPPER_CALL) - 1
            if call + 5 + int.from_bytes(self.data[call + 1:call + 5], 'little', signed=True) == machine:
                found.add(self.function_start(call))
            at = self.data.find(WRAPPER_CALL, at + 1)
        found.discard(None)
        return found.pop() if len(found) == 1 else None


def find_battle_end_sites(data):
    """-> (score dungeon wrapper offset, crisis wrapper offset, crisis argument offset), or a reason string."""
    args = list(CRISIS_DUNGEON_ARG.finditer(data))
    ends = list(SCORE_DUNGEON_END.finditer(data))
    if len(args) != 1 or len(ends) != 1:
        return f'{len(args)} Crisis / {len(ends)} Dungeon Ranking matches'
    try:
        image = _Image(data)
    except ValueError as error:
        return str(error)
    crisis_machine, score_machine = image.function_start(args[0].start()), image.function_start(ends[0].start())
    if crisis_machine is None or score_machine is None:
        return 'not inside a known function'
    crisis, score = image.wrapper_of(crisis_machine), image.wrapper_of(score_machine)
    if crisis is None or score is None or data[crisis:crisis + 5] != WRAPPER_HEAD:
        return 'BattleEndAsync wrappers not found'
    return score, crisis, args[0].start(1)


def _jmp(at, target):
    return b'\xe9' + (target - at - 5).to_bytes(4, 'little', signed=True)


def patch_battle_end(game_folder):
    """Dungeon Ranking fights end the Crisis-dungeon way (#71, #72) in <game_folder>/GameAssembly.dll.
    Returns a short status string; never raises on file errors."""
    dll = Path(game_folder) / 'GameAssembly.dll'
    try:
        data = dll.read_bytes()
    except OSError as error:
        return f'scoreend: cannot read {dll}: {error}'
    sites = find_battle_end_sites(data)
    if isinstance(sites, str):
        return f'scoreend: {sites} in {dll.name} (unknown build), left alone'
    score, crisis, arg = sites
    jump = _jmp(score, crisis)
    head_done, arg_done = data[score:score + 5] == jump, data[arg:arg + 8] == OWN_DUNGEON_ARG
    if head_done and arg_done:
        return 'scoreend: already patched'
    if not head_done and data[score:score + 5] != WRAPPER_HEAD:
        return f'scoreend: unexpected bytes at 0x{score:X} in {dll.name}, left alone'
    backup = dll.with_name('GameAssembly.dll.steam')
    try:
        if not backup.exists():
            shutil.copy2(dll, backup)
        with dll.open('r+b') as stream:
            stream.seek(arg)
            stream.write(OWN_DUNGEON_ARG)
            stream.seek(score)
            stream.write(jump)
    except OSError as error:
        return f'scoreend: cannot patch {dll} (is the game running?): {error}'
    return f'scoreend: patched {dll.name} at 0x{score:X} (-> 0x{crisis:X}) and 0x{arg:X}'


# ---- Max Damage Challenge in the game (no battle server) ----
# SoloPartySelectScreenPresenter.StartDamageChallengeBattleAsync always connects to a battle-server room
# (room/solo/create, which shows Under Maintenance now) and sets BattlePlayer.SetupParameter.usesBattleServer (+0x42).
# The fight itself runs in the game either way: BattleSceneBehaviour.PlayAsync starts it with damage/challenge/battle/
# start and ends it with damage/challenge/battle/end, but that request carries only damage_challenge_battle_id, so the
# score never reached a server. So:
#   1. the connect call (and the read of its UniTask) becomes `xorps xmm6,xmm6` + nops: a null UniTask is already
#      complete, so the await goes straight on;
#   2. usesBattleServer is set to 0 instead of 1;
#   3. where PlayAsync's end lambda calls RequestDamageChallengeBattleEndAsync, the fight's BattleInput.TotalDamage
#      (+0x18) is packed above the id: id | damage << 20 (engine/rules.py DC_SCORE_SHIFT unpacks it);
#   4. RequestDamageChallengeBattleEndAsync looks the fight up by that id before sending it, so the lookup masks the id
#      back to its low 20 bits (the request still sends the packed value).
# 3 and 4 jump to two small routines written into the zero padding after the end of the il2cpp code section, whose
# VirtualSize grows to cover them (still inside the section's last page). Game 4.0.0 RVAs: 1 0x1D13175, 2 0x1D133D7,
# 3 0x14AE2E0, 4 0xE7B125, routines 0x644AF30. Every site is found by byte pattern; anything other than exactly one
# match per site is left alone.
DC_CONNECT = re.compile(rb'\x41\xb9\x0f\x00\x00\x00\x4c\x8b\x47\x18\x49\x8b\xd6\x48\x8d\x4c\x24\x50'
                        rb'(\xe8....\x0f\x10\x30|\x0f\x57\xf6\x90\x90\x90\x90\x90)\x48\x8b\x0d', re.S)
DC_NO_CONNECT = b'\x0f\x57\xf6\x90\x90\x90\x90\x90'      # xorps xmm6,xmm6 ; 5 x nop
DC_USES_SERVER = re.compile(rb'\x48\x85\xc0\x0f\x84....\xc6\x40\x42([\x01\x00])\x48\x8b\x05....\x48\x8b\x88\xb8\x00\x00'
                            rb'\x00\x48\x8b\x01\x48\x85\xc0\x0f\x84....\xc7\x40\x28\x04\x00\x00\x00', re.S)
DC_END_CALL = re.compile(rb'\x4c\x8b\x80\x10\x01\x00\x00\x4d\x85\xc0\x7f\x05\xe9....\x4c\x8b\x48\x20\x4d\x85\xc9\x0f\x84....'
                         rb'(\x48\x89\x74\x24\x20|\xe9....)\x45\x8b\x49\x50\x48\x8b\x57\x20\x48\x8d\x8c\x24\x90\x00\x00\x00\xe8', re.S)
DC_END_LOOKUP = re.compile(rb'\x33\xc9\xe8....\x48\x85\xc0\x0f\x84....(\x45\x33\xc0\x49\x8b\x56\x28|\xe9....\x90\x90)'
                           rb'\x48\x8b\xc8\xe8....\x48\x8b\xd8\x33\xc9\xe8', re.S)
DC_CAVE = 64
# mov [rsp+20h],rsi ; test r15,r15 ; je +11 ; mov rax,[r15+18h] ; shl rax,20 ; or r8,rax ; (jmp back)
DC_PACK = bytes.fromhex('4889742420' '4d85ff' '740b' '498b4718' '48c1e014' '4909c0')
# xor r8d,r8d ; mov rdx,[r14+28h] ; and edx,0FFFFFh ; (jmp back)
DC_MASK = bytes.fromhex('4533c0' '498b5628' '81e2ffff0f00')


def find_cave(image, at, size=DC_CAVE):
    """-> (file offset, header offset of VirtualSize, new VirtualSize) for `size` bytes of the unused zero padding at
    the end of the code section holding file offset `at`, or None. The section's VirtualSize grows to cover them, which
    stays inside its last memory page (so the layout and SizeOfImage do not change)."""
    for raw, vsize, rsize, va, header in image.code:
        if not raw <= at < raw + vsize:
            continue
        cave = raw + (vsize + 15 & ~15)
        grown = cave - raw + size
        page = image.alignment
        if grown > rsize or (grown + page - 1) // page != (vsize + page - 1) // page:
            return None
        if image.data[raw + vsize:cave + size].strip(b'\x00'):
            return None
        return cave, header, grown
    return None


def patch_damage_challenge(game_folder):
    """Max Damage Challenge fights run in the game and send their damage to the server. Returns a short status string;
    never raises on file errors."""
    dll = Path(game_folder) / 'GameAssembly.dll'
    try:
        data = bytearray(dll.read_bytes())
    except OSError as error:
        return f'damagechallenge: cannot read {dll}: {error}'
    sites = [list(pattern.finditer(data)) for pattern in (DC_CONNECT, DC_USES_SERVER, DC_END_CALL, DC_END_LOOKUP)]
    if any(len(found) != 1 for found in sites):
        return f'damagechallenge: {"/".join(str(len(found)) for found in sites)} matches in {dll.name} (unknown build), left alone'
    connect, uses, end, lookup = (found[0].start(1) for found in sites)
    try:
        image = _Image(bytes(data))
    except ValueError as error:
        return f'damagechallenge: {error} in {dll.name}, left alone'
    grow = None
    if data[end] == 0xE9:
        cave = end + 5 + int.from_bytes(data[end + 1:end + 5], 'little', signed=True)
    else:
        found = find_cave(image, end)
        if found is None:
            return f'damagechallenge: no free space in {dll.name}, left alone'
        cave, header, grown = found
        grow = (header, grown.to_bytes(4, 'little'))
    pack, mask = cave, cave + 32
    if len({image.rva(at) - at for at in (pack, end, lookup)}) != 1:
        return f'damagechallenge: free space in another section of {dll.name}, left alone'
    wanted = {
        connect: DC_NO_CONNECT,
        uses: b'\x00',
        pack: DC_PACK + _jmp(pack + len(DC_PACK), end + 5),
        mask: DC_MASK + _jmp(mask + len(DC_MASK), lookup + 7),
        end: _jmp(end, pack),
        lookup: _jmp(lookup, mask) + b'\x90\x90',
    }
    if all(data[at:at + len(code)] == code for at, code in wanted.items()):
        return 'damagechallenge: already patched'
    if data[end] == 0xE9 and data[pack:pack + len(wanted[pack])] != wanted[pack]:
        return f'damagechallenge: unexpected code at 0x{pack:X} in {dll.name}, left alone'
    backup = dll.with_name('GameAssembly.dll.steam')
    try:
        if not backup.exists():
            shutil.copy2(dll, backup)
        with dll.open('r+b') as stream:
            if grow:
                stream.seek(grow[0])
                stream.write(grow[1])
            for at in (pack, mask, connect, uses, lookup, end):    # the routines go in before anything jumps to them
                stream.seek(at)
                stream.write(wanted[at])
    except OSError as error:
        return f'damagechallenge: cannot patch {dll} (is the game running?): {error}'
    return f'damagechallenge: patched {dll.name} at 0x{connect:X}, 0x{uses:X}, 0x{end:X}, 0x{lookup:X} (code at 0x{cave:X})'
