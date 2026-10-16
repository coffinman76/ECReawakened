# Ever Crisis Reawakened (ECR)
ECR is developed for educational and preservation purposes. To run it, you must supply the required files from your own lawfully acquired game installation. ECR does not provide game installations, APKs, MasterData, LocalizeText, or other assets, and does not link to downloads of those files. It is an independent community project, not affiliated with or endorsed by Square Enix or Applibot.

## Requirements

- Windows 10 or 11, with permission to approve administrator access for local networking and certificate setup.
- A fully downloaded Steam game installation. ECR does not install the game or download missing game assets.
- Complete MasterData and English LocalizeText from an existing player cache or your own previously prepared files. The baseline tables bundled in a clean game installation are insufficient. You need the final-version game assets and complete MasterData and English LocalizeText tables from your own installation or backup.
- Microsoft's Edge WebView2 Runtime for the launcher. If it is missing, install the [Evergreen WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).
- Internet access for the initial Python and Python-package setup. These are tooling dependencies, not game files. This snapshot is an unpacked Python application, not an Android app or Flatpak.

## Getting started

1. Extract ECR to a writable folder that you control.
2. Open **Ever Crisis Reawakened.bat**. First startup automatically tries to download Python and the packages listed in `requirements.txt`.
3. Let ECR find MasterData and LocalizeText. It searches its local `gameassets` folder, the selected/detected game folders, and the player's Documents game-cache folders. If needed you can use **Choose folder** to point it to a location that has the necessary assets.
4. Select or import your account (if you have exported from a server in the past), or create a new account.
5. Click **Start** to activate the backend, approve Windows setup if requested, and launch the game. Keep the launcher running in the background while playing.

If ECR reports incomplete data, provide a complete local cache and search again. Renaming an incomplete folder or supplying bootstrap tables will not replace the missing data. A typical Steam player cache is under:

```text
Documents/My Games/FF7EC/Steam/<SteamID>/
  MasterData/
  LocalizeText/En/
```

## Local networking and certificates

ECR listens on HTTPS port 443 and manages the game's Windows hosts redirects while running. The launcher displays the PC's LAN IP; another device must use that address, not `0.0.0.0`. Windows firewall and certificate setup require administrator privileges.

Each installation generates its own local certificate authorities and server certificates in `certs/`. Keep their private keys on that PC. A mobile game client would need matching DNS configuration and certificate trust; previously patched clients trusting another installation's certificate will not automatically trust these generated certificates. APK patching and mobile installation tools are not included here.

ECR only acknowledges final Octo revision checks for already completed clients. It does not serve full Octo indexes or blobs, fetch from asset mirrors, or populate an incomplete game installation.

## Files and account storage

```text
engine/                  Python gameplay and protocol implementation
engine/account-templates/ Readable starter-account and tutorial defaults
engine/data/             Gameplay mappings and local table-modification recipes
tools/                   Launcher support and Python setup
ui/                      HTML, CSS, and JavaScript launcher
gameplay-settings.json   Shared gameplay-rule settings

gameassets/              User-supplied/imported game data (created locally)
accounts/                Personal account state (created locally)
certs/                   Per-install certificates and private keys (created locally)
logs/                    Runtime logs (created locally)
python/                  Downloaded Python runtime (created locally)
```

The generated and user-supplied folders are excluded by `.gitignore`. Share source files rather than a folder containing your accounts or imported game data. Review log archives before sharing them because they can include local paths and account identifiers.

New accounts are generated from `engine/account-templates/new-account.json`. Its starter inventory and progression defaults were audited from the former baseline; user IDs, item-instance IDs, and dates are filled at runtime. Existing account saves and JSON imports keep their existing format.

## Development and validation

The launcher uses Python with pywebview and the existing HTML/CSS/JavaScript UI. Its bootstrap is `launcher.py`; `tools/launcher_backend.py` controls the engine, and `engine/cli.py` exposes command-line operations. Protocol definitions are in `engine/wire.py`.

With the bundled Python installed, run the included regression checks from the project folder:

```bat
python\python.exe -m unittest discover -s tests -v
```

## Licence and credits

Project-owned ECR code is licensed under **GNU GPL version 3 or later** (`GPL-3.0-or-later`); see [LICENSE](LICENSE). This grant does not cover Square Enix/Applibot game files, trademarks, or third-party components. Existing third-party copyright and licence notices remain applicable.

ECR is built through community contributions. Protocol structures, constants, and numeric gameplay defaults support interoperability; they are not a grant of rights to the original game or its assets.
This work is solely to preserve functionality of copies that were installed by players.

The separately bundled `steamfree/steam_api64.dll` is the unmodified third-party gbe_fork implementation described in [steamfree/README.txt](steamfree/README.txt), with its GPL/LGPL licence texts retained in that folder. Its corresponding upstream source is at [release-2026_09_27](https://github.com/Detanup01/gbe_fork/tree/release-2026_09_27).
Other Python dependencies are listed in `requirements.txt` and retain their respective licences.
