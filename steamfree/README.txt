steam_api64.dll here is gbe_fork (https://github.com/Detanup01/gbe_fork), release-2026_09_27,
emu-win-release-vs22.7z, release/regular/x64/steam_api64.dll, unmodified.
sha256 70d9826a402d3a78b0c07e9f7ea7c6832468a1113013f31ea570fccaa5445ea9
License: LGPL-3.0 (LICENSE-gbe_fork.txt), which builds on the GPL-3.0 (LICENSE-GPL-3.0.txt). Source: the repository above at that release tag.
Protect puts it in its copy of the game in place of Steam's steam_api64.dll (kept as steam_api64.dll.steam),
so the copy starts without Steam. Steam's own install is never changed.
