# Hidden Heroes 1.0.1 validation

Target: Steam build25076183, PE timestamp044DB866, image size09EBE000.
Base: published seven-character1.0.0 archive75FFE220.

- PAK entry count remains3665. Exactly13 UEXP files and19 owned data tables
  change. Each UEXP change is one ordinal byte, and the resulting assets match
  the previously qualified September GGO counterparts byte-for-byte.
- Seven-character identity, Exchange Shop and standard TranscendData preserved.
- Restored-character AttackWeight curves are explicitly retained; current
  vendor placeholder1.0 curves are not applied to those characters. Independent
  compatible vendor changes and current translations are merged. No unresolved
  three-way conflicts remain.
- All1797 asset packages open. The older USMAP leaves known raw exports;
  native ordinal compatibility is supported by the vendor comparison and exact
  repaired-asset equality, not by a claim of zero raw exports.
- CharacterShow guard:61 standalone contract checks,32 core checks,47 native
  detour tests, static analysis and off-target refusal. The standalone verifier
  requires no GGO sender DLL.
- Abuse compatibility: current-target derivation,12 mutation/round-trip checks,
  source contract and off-target refusal.
- Installer:11 unit tests, including rollback, conflict rejection, path identity,
  receipt validation, save preservation and Guard-aware uninstall ordering.
- Final packaged EXE installs12 verified payload files in an isolated fake game
  root. It binds the two UE4SS paths to that root. Uninstall removes the owned
  payload and preserves the save and vanilla-file sentinels.
- PAK and ZIP repeat builds are byte-identical. Embedded manifest verification
  passes and rejects a deliberately corrupted file hash.

The live game and its saves were not modified. Live gameplay acceptance of
this exact standalone package has not been performed; this is offline and
isolated installer validation.
