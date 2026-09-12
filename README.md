# DragonSword Hidden Heroes

Standalone seven-character mod for **DragonSword: Awakening**: Awakened Lute,
Viola, Ysera, Ryza, Jerome, Logan and Veronica.

**Current release: 1.0.1**, updated for **Steam build 25076183**
(September 4, 2026 game update).

## Download and install

1. Download [Hidden_Heroes_Installer.exe](https://github.com/Sai-Hakuto/DS_HIDDEN_HEROES/releases/latest/download/Hidden_Heroes_Installer.exe).
2. Place it in the game root, next to `DSClient.exe`.
3. Close the game and run the installer. It downloads the latest mod, verifies
   the release and file hashes, and backs up replaced files.

The full archive and optional Update Guard are on the
[releases page](https://github.com/Sai-Hakuto/DS_HIDDEN_HEROES/releases/latest).
The archive includes UE4SS, DSAbuseCompat and CharacterShowSetInfoGuard v5.

**Hidden Heroes and GGO are mutually exclusive.** Do not install both.
Standard ascension materials and the existing Hidden Heroes character tuning
are retained; the GGO gacha, Rift and season-pass systems are not included.

Use the generated **Uninstall Hidden Heroes** shortcut to remove the mod.
Removed files are preserved under `deleted_hidden_heroes`, and pre-existing
files are restored. Saved games are outside uninstall scope.

## Version 1.0.1

- Updates 13 character/component exports for the September game layout.
- Refreshes the two native compatibility modules.
- Rebases owned tables onto current vendor changes while retaining restored
  character damage scaling and standard ascension.
- Refreshes the packaged installer and fixes game-root comparisons through
  Windows directory junctions.

Offline native/asset checks and an isolated packaged install/uninstall passed.
No installation into the developer's live game was performed for this release.

## Installer source

The installer source, PyInstaller specification and tests are in [`installer/`](installer/).
Build on Windows with Python and PyInstaller:

```powershell
cd installer
python -m unittest discover -s testing_tools -p test_installer.py
python -m PyInstaller --noconfirm --clean HiddenHeroesInstaller.spec
```

Release file hashes and validation details are under [`releases/1.0.1/`](releases/1.0.1/).
