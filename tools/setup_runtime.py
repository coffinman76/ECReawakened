"""Install launcher/server dependencies into the bundled runtime. No PowerShell."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
PROBE = ['cryptography', 'cffi', 'google.protobuf', 'Crypto', 'msgpack', 'lz4', 'webview', 'psutil']


def main():
    try:
        for name in PROBE:
            importlib.import_module(name)
        return
    except ImportError:
        pass
    print('Setting up Ever Crisis: Reawakened dependencies. First setup needs internet access.', flush=True)
    environment = dict(os.environ, PYTHONPATH=str(ROOT / 'python' / 'Lib' / 'site-packages'))
    try:
        import pip
    except ImportError:
        path = ROOT / 'python' / 'get-pip.py'
        urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', path)
        subprocess.run([sys.executable, str(path), '--disable-pip-version-check', '--no-warn-script-location'], check=True, env=environment)
        path.unlink()
    flags = ['--disable-pip-version-check', '--no-warn-script-location']
    subprocess.run([sys.executable, '-m', 'pip', 'install', *flags, 'setuptools', 'wheel'], check=True, env=environment)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-build-isolation', *flags, '-r', str(ROOT / 'requirements.txt')], check=True, env=environment)
    subprocess.run([sys.executable, '-c', '; '.join('import ' + name for name in PROBE)], check=True)


if __name__ == '__main__':
    main()
