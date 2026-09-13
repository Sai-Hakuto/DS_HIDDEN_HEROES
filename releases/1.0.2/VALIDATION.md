# Hidden Heroes 1.0.2 acceptance

Target: DragonSword: Awakening 1.0.12, Steam build 25202218.
EXE SHA-256: `55DC7B73F2B8E29552D36FE87C75DD4D9F704B058974BBD1CF1C9A7A2852ECD4`.

Fifteen owned vendor tables were rebased, with 47 semantic field changes checked
against the old/new vendor pair. A single UI settings export byte changed.
Unrelated authored content and standalone balance were preserved. XML row order
is guarded, including a deliberate reorder negative control.

The PAK retains 3665 entries, SHA-256
`1D6571842BB62ED3827EDD5F30D0481ADF246D84CF510BD496463910B7D7C2D6`.
The two existing native compatibility DLLs are unchanged and armed on the new
executable. An isolated HH profile loaded the open world with restored Veronica
and Ysera. Each of the seven characters was not individually tested in combat.

A raw-profile test without installer configuration loaded no Lua modules.
Applying the existing installer's absolute UE4SS path configuration resolved it;
this was a fixture omission, not a new HH runtime defect.

The archive contains 14 entries, of which 12 are installed game files. It includes
UE4SS, DSAbuseCompat and CharacterShowSetInfoGuard, and no GGO runtime or player
save. Deterministic packing and independent file hashes passed.

Final packaged installer acceptance passed: all 12 installed files independently
verified, correct absolute UE4SS paths, uninstall and save-sentinel preservation.
The installer executable is unchanged from the verified 1.0.1 release.
