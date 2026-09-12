# Hidden Heroes Installer

`Hidden_Heroes_Installer.exe` is the single-file installer and uninstaller for
DragonSword Hidden Heroes. It must be launched from the game root next to
`DSClient.exe`; otherwise it exits without changing anything.

The installer resolves the latest GitHub release, verifies GitHub's SHA-256,
validates every payload file against the embedded manifest, backs up replaced
files, installs the mod, and offers the optional Update Guard v2. Hidden Heroes
is intentionally rejected when a GGO installation is present.

A successful installation creates `Uninstall Hidden Heroes.lnk` in the game
root. After confirmation it moves installed files and owned UE4SS runtime state
to `deleted_hidden_heroes\YYYYMMDD-HHMMSS\` with the original hierarchy and
restores files that existed before installation. Vanilla files and `DS\Saved`
are outside the removal scope.

Build:

```powershell
python -m PyInstaller --noconfirm --clean HiddenHeroesInstaller.spec
```

The build disables UPX and embeds product metadata. It is unsigned, so public
distribution should include the published SHA-256 and use Authenticode when a
trusted code-signing certificate becomes available.

Acceptance install and uninstall:

```powershell
Hidden_Heroes_Installer.exe --non-interactive --game-dir "F:\SteamLibrary\steamapps\common\DragonSword  Awakening" --skip-system-integration --result-json result.json
Hidden_Heroes_Installer.exe --non-interactive --uninstall --game-dir "F:\SteamLibrary\steamapps\common\DragonSword  Awakening" --result-json uninstall-result.json
```
