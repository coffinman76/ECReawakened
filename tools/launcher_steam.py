"""Steam installation and protected-copy operations for the desktop launcher."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import hashlib

BUILDS = {'24813881': '4.0.0', '25718420': '4.0.1.0'}

# Battle Ranking (#50), Clash and Crisis Battle stages ran on Square's battle server: before the party screen starts one,
# the game asks EnumExtensions.UseBattleServer(EventBattleType) - true for Clash (2), CrisisBattle (7) and
# DamageRankingBattleScoreBattle (9) - and then sends match/session + room/solo/create to get a battle-server room.
# There is no battle server any more, so the game shows Network Error / Under Maintenance. Making that function return
# false sends these stages down the ordinary local path (event/solo/battle/start + end, with battle_input.total_damage),
# which the server already scores. Callers: the solo and guest party screens' CreateEventSoloAreaBattle and
# RestoreBattleStore.UseBattleServer (resuming an interrupted battle) - all three should be local now.
# Game 4.0.0: RVA 0x2158100, file offset 0x2156700. The whole 17-byte function is matched, so another build with the
# same code is patched too and any other build is left alone.
BATTLE_SERVER_CHECK = bytes.fromhex('83f902740c83f907740783f9090f94c0c3')    # cmp ecx,2 / je / cmp ecx,7 / je / cmp ecx,9 / sete al / ret
BATTLE_SERVER_LOCAL = bytes.fromhex('31c0c3') + BATTLE_SERVER_CHECK[3:]       # xor eax,eax / ret (the rest is never reached)


def patch_battle_server(folder):
    """Make event battles that used Square's battle server run locally in this game copy's GameAssembly.dll.
    Returns 'patched', 'already' or 'unknown build' (the file is left as it is). The original is kept once as
    GameAssembly.dll.steam."""
    dll = folder / 'GameAssembly.dll'
    data = dll.read_bytes()
    if data.count(BATTLE_SERVER_LOCAL) == 1 and BATTLE_SERVER_CHECK not in data:
        return 'already'
    if data.count(BATTLE_SERVER_CHECK) != 1:
        return 'unknown build'
    backup = folder / 'GameAssembly.dll.steam'
    if not backup.exists():
        shutil.copy2(dll, backup)
    at = data.index(BATTLE_SERVER_CHECK)
    with open(dll, 'r+b') as handle:
        handle.seek(at)
        handle.write(BATTLE_SERVER_LOCAL[:3])
    if dll.read_bytes()[at:at + len(BATTLE_SERVER_LOCAL)] != BATTLE_SERVER_LOCAL:
        raise RuntimeError('GameAssembly.dll did not take the battle-server patch.')
    return 'patched'


def registry(key, value):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, value)[0]
    except OSError:
        return ''


class Steam:
    def __init__(self, root):
        self.root = root
        try:
            self.state = json.loads((root / 'game.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            self.state = {}

    def save(self):
        temporary = self.root / 'game.json.tmp'
        temporary.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        temporary.replace(self.root / 'game.json')

    def install(self):
        from engine.cli import steam_game_dir
        folder = steam_game_dir()
        return Path(folder) if folder else None

    def copy(self):
        folder = self.state.get('copy')
        return Path(folder) if folder and (Path(folder) / 'FF7EC.exe').is_file() else None

    def build(self, folder):
        if not folder:
            return ''
        try:
            if (folder / 'game-build.txt').is_file():
                return (folder / 'game-build.txt').read_text().strip()
            text = (folder.parent.parent / 'appmanifest_2484110.acf').read_text()
            match = re.search(r'"buildid"\s*"(\d+)"', text)
            return match[1] if match else ''
        except OSError:
            return ''

    def identity(self):
        active = registry(r'Software\Valve\Steam\ActiveProcess', 'ActiveUser')
        if active:
            sid = str(76561197960265728 + int(active))
        else:
            from engine.localsave import steam_id
            sid = steam_id(str(self.root))
        if not sid:
            raise ValueError('Open Steam and sign in once with the account you play FF7EC on.')
        self.state['steamid'] = sid
        self.save()
        return sid

    def setup_copy(self, folder):
        installed = self.install()
        if installed and (folder.resolve() == installed.resolve() or folder.resolve().is_relative_to(installed.resolve())):
            raise ValueError("Choose a copy outside Steam's installed game folder.")
        sid = self.identity()
        source = self.root / 'steamfree' / 'steam_api64.dll'
        appid = self.root / 'steamfree' / 'steam_appid.txt'
        if not source.is_file() or not appid.is_file():
            raise ValueError('The steamfree folder is missing. Unzip the complete Ever Crisis: Reawakened release.')
        plugin = folder / 'FF7EC_Data' / 'Plugins' / 'x86_64'
        if not (folder / 'FF7EC.exe').is_file() or not plugin.is_dir():
            raise ValueError('Select the game folder containing FF7EC.exe and FF7EC_Data.')
        target = plugin / 'steam_api64.dll'
        same = target.is_file() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
        backup = plugin / 'steam_api64.dll.steam'
        if target.is_file() and not same and not backup.exists():
            shutil.copy2(target, backup)
        if not same:
            shutil.copy2(source, target)
        name = 'Player'
        try:
            users = (Path(registry(r'Software\Valve\Steam', 'SteamPath')) / 'config' / 'loginusers.vdf').read_text(encoding='utf-8')
            match = re.search('"' + re.escape(sid) + r'"\s*\{[^}]*?"PersonaName"\s*"([^"]*)"', users)
            if match:
                name = re.sub(r'[\r\n=]', '', match[1]) or name
        except OSError:
            pass
        language = registry(r'Software\Valve\Steam', 'Language') or 'english'
        if not re.fullmatch('[a-z]+', language):
            language = 'english'
        settings = plugin / 'steam_settings'
        settings.mkdir(exist_ok=True)
        contents = {
            'steam_appid.txt': '2484110',
            'configs.user.ini': f'[user::general]\naccount_name={name}\naccount_steamid={sid}\nlanguage={language}\n[user::saves]\nlocal_save_path=./ec-saves\n',
            'configs.main.ini': '[main::connectivity]\ndisable_networking=1\n',
            'auto_send_invite.txt': '',
            'ec-local.txt': 'Set up by Ever Crisis: Reawakened. Original DLL: steam_api64.dll.steam.\n',
        }
        for filename, text in contents.items():
            (settings / filename).write_text(text, encoding='utf-8')
        for destination in (plugin / 'steam_appid.txt', folder / 'steam_appid.txt'):
            if not destination.exists():
                shutil.copy2(appid, destination)
        # Dungeon Ranking bosses: fight in the game, not on Square's battle server (GitLab #49).
        from tools import scoreboss
        print(scoreboss.patch(folder), flush=True)
        # Dungeon Ranking fights end the Crisis-dungeon way: score, HP, Limit and Ultimate Weapon carry over (#71, #72).
        print(scoreboss.patch_battle_end(folder), flush=True)
        # Max Damage Challenge: fight in the game, not on Square's battle server, and send the damage to the server.
        print(scoreboss.patch_damage_challenge(folder), flush=True)
        if (folder / 'GameAssembly.dll').is_file():
            self.state['battle_server_patch'] = patch_battle_server(folder)
            self.save()

    def use(self, folder, confirm):
        build = self.build(folder)
        if build and build not in BUILDS:
            raise ValueError(f'Unsupported game build {build}. Supported versions: {", ".join(BUILDS.values())}.')
        if not build and not confirm('The build of this copy is unknown. Use it anyway?'):
            return
        self.setup_copy(folder)
        self.state['copy'] = str(folder)
        self.save()

    def protect(self, selected, confirm, say):
        source = self.install()
        if not source:
            raise ValueError('Game not found in Steam folders.')
        build = self.build(source)
        if build not in BUILDS:
            raise ValueError(f'Cannot protect unsupported Steam build {build or "unknown"}.')
        self.identity()
        destination = selected / 'FF7EC' if any(selected.iterdir()) else selected
        src, dst = source.resolve(), destination.resolve()
        if dst == src or dst.is_relative_to(src) or src.is_relative_to(dst):
            raise ValueError('Choose a separate folder outside the Steam game installation.')
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Destination is not empty. Choose another folder.')
        say('Measuring the game…')
        size = sum(p.stat().st_size for p in source.rglob('*') if p.is_file() and p.suffix != '.log')
        if shutil.disk_usage(selected).free < size + 1024**3:
            raise ValueError('Not enough free space for the game plus 1 GB spare.')
        if not confirm(f'Copy {size / 1024**3:.1f} GB from {source} to {destination}? Steam’s installation stays unchanged.'):
            return
        say(f'Copying game to {destination}…')
        # Robocopy codes 0–7 indicate success, unlike ordinary process exit codes.
        result = subprocess.run(['robocopy.exe', str(source), str(destination), '/E', '/MT:16', '/R:2', '/W:2', '/NP', '/NFL', '/NDL', '/NJH', '/NJS', '/XF', '*.log'],
                                creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if result.returncode >= 8 or not (destination / 'FF7EC.exe').is_file():
            raise RuntimeError(f'Game copy failed (robocopy code {result.returncode}).')
        (destination / 'game-build.txt').write_text(build)
        self.setup_copy(destination)
        self.state['copy'] = str(destination)
        self.save()
        say(f'Protected copy ready: {destination}')

    def play(self):
        folder = self.copy()
        if not folder:
            os.startfile('steam://rungameid/2484110')
            return
        self.setup_copy(folder)
        args = [str(folder / 'FF7EC.exe')]
        try:
            match = re.search(r'<Index>(\d+)</Index>', (folder / 'launcher.xml').read_text())
            if match:
                args += ['-force-device-index', match[1]]
        except OSError:
            pass
        subprocess.Popen(args, cwd=folder)
