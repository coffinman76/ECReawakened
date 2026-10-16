"""LAN discovery and a scoped Windows firewall rule for this server instance."""
import hashlib
import ipaddress
import os
import socket
import subprocess
import sys


def lan_addresses():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = ipaddress.ip_address(item[4][0])
            if not (address.is_loopback or address.is_link_local or address.is_unspecified):
                addresses.add(str(address))
    except OSError:
        pass
    return sorted(addresses)


def firewall(home, install, port=443):
    """Private networks/local subnet only; remove this project's rule on shutdown."""
    if os.name != 'nt':
        return
    name = 'EC-Local-LAN-' + hashlib.sha256(os.path.abspath(home).lower().encode()).hexdigest()[:12]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.run(['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={name}'],
                   capture_output=True, creationflags=flags)
    if install:
        result = subprocess.run(['netsh', 'advfirewall', 'firewall', 'add', 'rule', f'name={name}',
                                 'dir=in', 'action=allow', 'protocol=TCP', f'localport={port}',
                                 'profile=private', 'remoteip=LocalSubnet', f'program={sys.executable}'],
                                capture_output=True, text=True, creationflags=flags)
        if result.returncode:
            raise RuntimeError('Windows firewall rule failed: ' + (result.stdout + result.stderr).strip())
