"""Desktop controller. Only the narrow action/status API is exposed to JavaScript."""
import ctypes
from collections import deque
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import zipfile

import psutil
from tools.launcher_steam import Steam, BUILDS



def acquire_window():
    if os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, 'Global\\EC-Local-window')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return False
    return handle




class Launcher:
    def __init__(self, root):
        self._root = Path(root)
        self._window = None
        self._lock = threading.RLock()
        self._messages = deque(maxlen=1500)
        self._sequence = 0
        self._server = None
        self._closed = False
        self._initialized = False
        self._initializing = False
        self._ready = False
        self._busy = False
        self._data_ready = False
        self._steam = Steam(self._root)
        self._accounts = []
        self._active = None
        self._network = {'addresses': [], 'hosts': []}
        self._seasons = []
        self._logs = self._root / 'logs'
        self._logs.mkdir(exist_ok=True)
        self._logpath = self._logs / 'server.log'
        self._launcher_log = self._logs / 'launcher.log'
        try:    # keep one previous launcher log
            if self._launcher_log.stat().st_size > 2 << 20:
                os.replace(self._launcher_log, self._logs / 'launcher.log.1')
        except OSError:
            pass
        self._logpos = self._logpath.stat().st_size if self._logpath.exists() else 0
        self._env = dict(os.environ, EC_HOME=str(self._root), PYTHONUNBUFFERED='1')
        self._python = str(self._root / 'python' / 'python.exe') if os.name == 'nt' else sys.executable
        self._redirect = None
        self._children = set()
        self._client_data = {'running': False, 'ready': False, 'label': 'Not checked', 'source': ''}

    def _client_data_folders(self):
        folders = []
        selected = self._steam.state.get('client_data_source')
        for folder in (selected, self._steam.copy(), self._steam.install()):
            if folder and str(folder) not in [str(p) for p in folders]:
                folders.append(Path(folder))
        return folders

    @staticmethod
    def _validate_client_data_folder(folder):
        if not folder.is_dir():
            raise ValueError('Select an existing game, player-save, or gameassets folder.')

    def _prepare_client_data(self):
        self._client_data.update(running=True, ready=False, label='Looking for complete MasterData and LocalizeText...')
        try:
            args = ['prepare-client-data']
            for folder in self._client_data_folders():
                args.extend(['--game', str(folder)])
            result = self._command(*args)
            label = f'{result["tables"]} MasterData and {result["language_files"]} LocalizeText files ready'
            self._client_data.update(ready=True, label=label, source=str(result.get('source') or self._root / 'gameassets'))
            for skipped in result.get('rejected', []):
                self._say(f'Skipped MasterData that the offline edits cannot apply to: {skipped}')
            self._say(label)
            if result.get('using'):
                self._say(result['using'])
            for name, path in result.get('borrowed', {}).items():
                self._say(f'{name} was missing or stale there; used the matching copy in {path}')
            for name, path in result.get('loose', {}).items():
                self._say(f'{name}: no folder has the version its catalog lists; used {path} as it is')
            return result
        except Exception as error:
            if getattr(error, 'using', None):
                self._say(error.using)
            self._client_data.update(ready=False, source='', label=str(error).splitlines()[0])
            raise
        finally:
            self._client_data['running'] = False

    def _find_client_data(self):
        if self._running() or self._port_busy() or self._others():
            raise ValueError('Stop the server and asset operations before finding MasterData and LocalizeText.')
        result = self._prepare_client_data()
        self._seasons = self._command('seasons')['seasons']
        return result

    def _say(self, message, record=True):
        """Show a line in the window; also keep it in logs/launcher.log unless it came from server.log."""
        with self._lock:
            self._sequence += 1
            self._messages.append({'id': self._sequence, 'text': str(message)})
            if record:
                try:
                    with self._launcher_log.open('a', encoding='utf-8') as stream:
                        stream.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}\n')
                except OSError:
                    pass

    def _spawn(self, args, **options):
        process = subprocess.Popen([self._python, '-u', '-m', 'engine.cli', *args], cwd=self._root,
                                env=self._env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                **options)
        with self._lock:
            self._children.add(process)
        return process

    def _command(self, *args):
        process = self._spawn(list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        output = process.communicate()[0]
        with self._lock:
            self._children.discard(process)
        for line in reversed(output.splitlines()):
            if line.startswith('{'):
                value = json.loads(line)
                if not value.get('ok') and not value.get('exists'):
                    error = RuntimeError(value.get('error', output))
                    error.using = value.get('using')
                    raise error
                return value
        raise RuntimeError(output.strip() or f'Engine exited with code {process.returncode}')

    def _running(self):
        return self._server is not None and self._server.poll() is None

    def _game_running(self):
        return any((p.info['name'] or '').lower() == 'ff7ec.exe' for p in psutil.process_iter(['name']))

    def _others(self):
        found = []
        for process in psutil.process_iter(['pid', 'cmdline']):
            args = process.info['cmdline'] or []
            if process.pid != (self._server.pid if self._server else 0) and 'engine.cli' in args and 'serve' in args:
                found.append(process)
        return found

    def _port_busy(self):
        with socket.socket() as listener:
            if os.name == 'nt':
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(('0.0.0.0', 443))
                return False
            except OSError:
                return True

    def _confirm(self, text):
        return self._window.create_confirmation_dialog('Ever Crisis: Reawakened', text)

    def _pick(self, folder=False, file_types=('All files (*.*)',)):
        import webview
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER if folder else webview.FileDialog.OPEN,
                                                  allow_multiple=False, file_types=file_types)
        return Path(result[0]) if result else None

    def _clear_hosts(self):
        if not self._running() and not self._others():
            self._command('unhost')

    def initialize(self):
        with self._lock:
            if self._initialized:
                return {'ok': True}
            self._initialized = True
            self._initializing = True
        try:
            self._say('Ever Crisis: Reawakened — leave this window open while playing. Closing it stops the server.')
            if os.name == 'nt':
                options = dict(creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, text=True)
                rules = subprocess.run(['netsh.exe', 'interface', 'portproxy', 'show', 'v4tov4'], **options)
                if re.search(r'^\s*127\.77\.7\.7\s+443\s+', rules.stdout, re.M):
                    result = subprocess.run(['netsh.exe', 'interface', 'portproxy', 'delete', 'v4tov4', 'listenaddress=127.77.7.7', 'listenport=443'], **options)
                    if result.returncode:
                        self._say('Could not remove an old port-forwarding rule: ' + result.stdout)
                    else:
                        self._say('Removed an old port-forwarding rule (127.77.7.7:443).')
            if not self._port_busy():
                self._clear_hosts()
                result = self._command('migrate-data')
                if result['moved']:
                    self._say(f'Migrated {result["moved"]} legacy data items.')
                result = self._command('ingest-folder')
                for catalog in result['imported']:
                    self._say(f'Imported {catalog["platform"]} r{catalog["revision"]}.')
            else:
                self._say('Another server is using port 443. Stop it before starting this server.')
            self._data_ready = True
            try:
                self._prepare_client_data()
            except Exception as error:
                self._say(f'MasterData and LocalizeText are incomplete: {error}. Choose a folder or find files again after restoring them.')
            self._refresh_accounts()
            self._network = self._command('network')
            self._seasons = self._command('seasons')['seasons'] if self._client_data['ready'] else []
            return {'ok': True}
        except Exception as error:
            self._say(f'Data setup failed: {error}. Resolve this and reopen the launcher.')
            return {'ok': False, 'error': str(error)}
        finally:
            self._initializing = False

    def _refresh_accounts(self):
        value = self._command('list')
        self._accounts, self._active = value['accounts'], value['active']




    def _stop_server(self):
        if not self._running():
            return
        process = self._server
        (self._logs / 'stop.flag').touch()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._children.discard(process)
        self._server = None
        self._ready = False
        self._clear_hosts()
        # Restore the player's original GameSave even after forced termination.
        from engine import localsave
        localsave.detach(str(self._root), str(self._root / 'accounts'))
        self._say('Server stopped. Game addresses restored.')

    def _start_server(self):
        others = self._others()
        if others:
            if not self._confirm('Another Ever Crisis: Reawakened server is running. Stop it and start this server?'):
                return
            for process in others:
                process.terminate()
                process.wait(timeout=5)
            self._clear_hosts()
        if self._port_busy():
            raise ValueError('Another program is using HTTPS port 443. Close it before starting the server.')
        self._prepare_client_data()
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            if self._redirect:
                kernel.CloseHandle(ctypes.c_void_p(self._redirect))
            self._redirect = kernel.CreateMutexW(None, False, 'Global\\ffviiec-redirect')
            if ctypes.get_last_error() == 183 and not self._confirm('Another DNS redirect program is open. Start anyway?'):
                kernel.CloseHandle(ctypes.c_void_p(self._redirect))
                self._redirect = None
                return
        (self._logs / 'stop.flag').unlink(missing_ok=True)
        self._logpos = self._logpath.stat().st_size if self._logpath.exists() else 0
        self._ready = False
        self._server = self._spawn(['serve', '--parent', str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._say('Starting Reawakened…')

    def _save_logs(self):
        import webview
        filename = f'reawakened-logs-{time.strftime("%Y%m%d-%H%M%S")}.zip'
        selected = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename, file_types=('ZIP archive (*.zip)',))
        if not selected:
            return
        destination = Path(selected if isinstance(selected, str) else selected[0])
        files = [p for p in self._logs.rglob('*') if p.is_file() and p.name != 'stop.flag']
        game = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))).parent / 'LocalLow' / 'SquareEnix' / 'FF7EC'
        for name in ('Player.log', 'Player-prev.log'):
            if (game / name).is_file():
                files.append(game / name)
        for folder in (game / 'QuaLoggerLogs', game / 'Bugsnag' / 'Events'):
            if folder.exists():
                files.extend(p for p in folder.glob('*') if p.is_file() and p.stat().st_mtime > time.time() - 172800)
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                if file.resolve() == destination.resolve():
                    continue
                name = 'server/' + str(file.relative_to(self._logs)) if file.is_relative_to(self._logs) else 'game/' + str(file.relative_to(game))
                archive.write(file, name)
        self._say(f'Saved logs to {destination}. Nothing was sent anywhere.')

    def action(self, name, value=None):
        with self._lock:
            if self._busy:
                return {'ok': False, 'error': 'Wait for the current operation.'}
            self._busy = True
        try:
            if name == 'refresh_network':
                self._network = self._command('network')
            elif not self._data_ready:
                raise ValueError('Wait for launcher setup to finish.')
            elif name == 'find_client_data':
                self._find_client_data()
            elif name == 'choose_client_data':
                if self._running() or self._port_busy() or self._others():
                    raise ValueError('Stop the server and asset operations before changing the source folder.')
                folder = self._pick(folder=True)
                if folder is not None:
                    self._validate_client_data_folder(folder)
                    self._steam.state['client_data_source'] = str(folder)
                    self._steam.save()
                    self._say(f'MasterData and LocalizeText source: {folder}')
                    self._prepare_client_data()
                    self._seasons = self._command('seasons')['seasons']
            elif name == 'start':
                if not self._running():
                    self._start_server()
            elif name == 'open_data':
                if os.name == 'nt':
                    os.startfile(str(self._root))
                else:
                    subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(self._root)])
            elif name == 'stop':
                self._stop_server()
            elif name in ('new', 'use', 'import_account', 'remove'):
                if name == 'new':
                    if self._confirm('Start a new account? Your other accounts remain available.'):
                        self._command('new')
                elif name == 'use':
                    if self._game_running():
                        raise ValueError('Close the game before switching accounts.')
                    self._command('use', str(value))
                elif name == 'remove':
                    if self._running():
                        raise ValueError('Stop the server before removing an account.')
                    if self._game_running():
                        raise ValueError('Close the game before removing an account.')
                    account = next((a for a in self._accounts if a['id'] == str(value)), None)
                    if not account:
                        raise ValueError('Pick the account to remove.')
                    label = f"{account['name'] or '(no name yet)'} ({account['id']})"
                    playing = ' This is the account you play.' if account['id'] == self._active else ''
                    if self._confirm(f'Remove {label} from the list?{playing}\n\nNothing is deleted: the save moves to the '
                                     '"removed" folder (accounts/removed) and can be recovered from there.'):
                        result = self._command('remove', account['id'])
                        self._say(f'Removed {label}. Its save is in {result["moved"]}.')
                else:
                    file = self._pick(file_types=('Account JSON (*.json)', 'All files (*.*)'))
                    if file:
                        result = self._command('import', str(file))
                        if result.get('exists'):
                            if self._running():
                                raise ValueError('Stop the server before replacing an account.')
                            if self._confirm('Replace this account? Existing progress is backed up under accounts/old.'):
                                self._command('import', str(file), '--replace')
                self._refresh_accounts()
            elif name == 'season':
                if self._running():
                    raise ValueError('Stop the server before changing the season pass.')
                if str(value) not in ['0'] + [str(s['id']) for s in self._seasons]:
                    raise ValueError('Unknown season')
                self._steam.state['season_pass'] = str(value) if str(value) != '0' else ''
                self._steam.save()
            elif name == 'play':
                if not self._ready or not self._running():
                    raise ValueError('Start the server and wait until it is running.')
                if self._game_running():
                    raise ValueError('The game is already open. Restart it if it was opened before the server.')
                self._steam.play()
            elif name in ('protect', 'use_copy'):
                if self._running() or self._game_running():
                    raise ValueError('Stop the server and close the game first.')
                folder = self._pick(folder=True)
                if folder:
                    if name == 'protect':
                        self._steam.protect(folder, self._confirm, self._say)
                    else:
                        self._steam.use(folder, self._confirm)
            elif name == 'save_logs':
                self._save_logs()
            else:
                raise ValueError('Unknown launcher action')
            return {'ok': True}
        except Exception as error:
            self._say(error)
            return {'ok': False, 'error': str(error)}
        finally:
            self._busy = False

    def status(self, after=0):
        try:
            if self._logpath.exists():
                with self._logpath.open('rb') as stream:
                    if self._logpath.stat().st_size < self._logpos:
                        self._logpos = 0
                    stream.seek(self._logpos)
                    data = stream.read()
                    # Keep incomplete log lines for the next poll.
                    end = data.rfind(b'\n') + 1
                    self._logpos += end
                for line in data[:end].decode('utf-8', errors='replace').splitlines():
                    self._say(line, record=False)
                    if 'ready: start the game' in line:
                        self._ready = True
            if not self._busy and self._server and self._server.poll() is not None:
                self._say(f'The server stopped by itself (code {self._server.returncode}). See Save logs for details.')
                self._children.discard(self._server)
                self._server = None
                self._ready = False
                self._clear_hosts()
                from engine import localsave
                localsave.detach(str(self._root), str(self._root / 'accounts'))
            folder = self._steam.copy() or self._steam.install()
            build = self._steam.build(folder)
            with self._lock:
                messages = [m.copy() for m in self._messages if m['id'] > int(after)]
            return {'accounts': self._accounts, 'active': self._active,
                    'server': 'Running' if self._running() and self._ready else 'Starting…' if self._running() else 'Stopped',
                    'running': self._running(), 'ready': self._ready and self._running(),
                    'busy': self._busy, 'data_ready': self._data_ready,
                    'client_data': self._client_data.copy(),
                    'network': self._network, 'seasons': self._seasons,
                    'season': str(self._steam.state.get('season_pass') or '0'),
                    'game': ('Protected copy' if self._steam.copy() else 'Steam installation') + f': {folder or "not found"} ({BUILDS.get(build, build or "build unknown")})',
                    'copy': bool(self._steam.copy()), 'installed': bool(self._steam.install()),
                    'game_running': self._game_running(), 'messages': messages, 'data_folder': str(self._root)}
        except Exception as error:
            self._say(error)
            return {'error': str(error)}

    def _close(self):
        if self._busy or self._initializing:
            self._say('Wait for the current operation before closing the launcher.')
            return False
        if self._closed:
            return
        self._closed = True
        try:
            self._stop_server()
            self._clear_hosts()
            for process in list(self._children):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
        except Exception as error:
            self._say(error)
        if self._redirect:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._redirect))
            self._redirect = None
