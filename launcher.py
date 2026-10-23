"""Ever Crisis: Reawakened desktop entry point. The game server remains a separate process."""
import ctypes
import logging
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    os.environ['EC_HOME'] = str(ROOT)
    if os.name == 'nt':
        if not ctypes.windll.shell32.IsUserAnAdmin():
            result = ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', sys.executable, subprocess.list2cmdline([str(ROOT / 'launcher.py')]), str(ROOT), 1)   # SW_SHOWNORMAL: 0 (SW_HIDE) carries over to the window, which then never shows
            if result <= 32:
                ctypes.windll.user32.MessageBoxW(None, 'Administrator access is needed for hosts, certificates and the firewall.', 'Ever Crisis: Reawakened', 0)
            return
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ECLocal.Window')
    from tools.launcher_backend import Launcher, acquire_window
    lock = acquire_window()
    if lock is False:
        ctypes.windll.user32.MessageBoxW(None, 'Ever Crisis: Reawakened is already open. Use its existing window.', 'Ever Crisis: Reawakened', 0)
        return
    import webview
    api = Launcher(ROOT)
    window = webview.create_window('Ever Crisis: Reawakened', str(ROOT / 'ui' / 'index.html'), js_api=api,
                                   width=480, height=800, min_size=(420, 640), text_select=True)
    api._window = window
    window.events.closing += api._close
    try:
        webview.start(gui='edgechromium' if os.name == 'nt' else None)
    finally:
        api._close()
        if lock:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(lock))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        (ROOT / 'logs').mkdir(exist_ok=True)
        logging.basicConfig(filename=ROOT / 'logs' / 'launcher.log', level=logging.ERROR)
        logging.exception('Launcher failed')
        message = f'{error}\n\nSee logs/launcher.log.' + (' Install Microsoft Edge WebView2 Runtime if it is missing.' if os.name == 'nt' else '')
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, message, 'Ever Crisis: Reawakened could not start', 0)
        else:
            print(message, file=sys.stderr)
