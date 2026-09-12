from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import hidden_heroes_installer as installer  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.processes_patch = mock.patch.object(installer, "processes", return_value=[])
        self.processes_patch.start()
        self.work = Path(tempfile.mkdtemp(prefix="hidden_heroes_tests_", dir=Path(__file__).parent))
        installer.DATA_ROOT = self.work / "installer_data"
        installer.LOG_PATH = None
        installer.initialize_log()
        self.payload = self.work / "payload"
        self.archive = self.work / "DS_HIDDEN_HEROES_ALL_7_20990101.zip"
        self._make_payload()
        self._zip_payload(self.archive)
        self.metadata = self.work / "release.json"
        self._metadata(self.metadata)

    def tearDown(self) -> None:
        self.processes_patch.stop()
        shutil.rmtree(self.work, ignore_errors=True)

    def _write(self, relative: str, data: bytes) -> None:
        path = self.payload / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _make_payload(self) -> None:
        files = {
            "DS/Binaries/Win64/dwmapi.dll": b"proxy",
            "DS/Binaries/Win64/ue4ss/LICENSE": b"license",
            "DS/Binaries/Win64/ue4ss/UE4SS.dll": b"fixture ue4ss",
            "DS/Binaries/Win64/ue4ss/UE4SS-settings.ini": (
                b"ModsFolderPath = DS/Binaries/Win64/ue4ss/QualificationMods\r\n"
                b"ControllingModsTxt = QualificationMods/mods.txt\r\n"
            ),
            "DS/Binaries/Win64/ue4ss/QualificationMods/mods.txt": b"DSAbuseCompat : 1\r\nCharacterShowSetInfoGuard : 1\r\n",
            "DS/Binaries/Win64/ue4ss/QualificationMods/DSAbuseCompat/Native/DSAbuseCompat.dll": b"compat dll",
            "DS/Binaries/Win64/ue4ss/QualificationMods/DSAbuseCompat/README.md": b"compat readme",
            "DS/Binaries/Win64/ue4ss/QualificationMods/DSAbuseCompat/Scripts/main.lua": b"return {}",
            "DS/Binaries/Win64/ue4ss/QualificationMods/CharacterShowSetInfoGuard/Native/DSCharacterShowSetInfoGuard.dll": b"guard dll",
            "DS/Binaries/Win64/ue4ss/QualificationMods/CharacterShowSetInfoGuard/README.md": b"guard readme",
            "DS/Binaries/Win64/ue4ss/QualificationMods/CharacterShowSetInfoGuard/Scripts/main.lua": b"return {}",
            "DS/Content/Paks/mods/DS_HIDDEN_HEROES_P.pak": os.urandom(1280 * 1024),
        }
        manifest_files = []
        for relative, data in files.items():
            self._write(relative, data)
            manifest_files.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest().upper()})
        self._write("README.md", b"Hidden Heroes fixture")
        self._write("MANIFEST.json", json.dumps({"files": manifest_files}, indent=2).encode("utf-8"))

    def _zip_payload(self, target: Path) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
            for path in sorted(self.payload.rglob("*")):
                if path.is_file():
                    package.write(path, path.relative_to(self.payload).as_posix())
        self.assertGreater(target.stat().st_size, 1024 * 1024)

    def _metadata(self, target: Path, hash_override: str | None = None) -> None:
        value = {
            "tag_name": "fixture-1.0.0", "draft": False, "prerelease": False,
            "assets": [{
                "name": self.archive.name,
                "size": self.archive.stat().st_size,
                "digest": "sha256:" + (hash_override or digest(self.archive)),
                "browser_download_url": f"https://github.com/{installer.REPOSITORY}/releases/download/fixture-1.0.0/{self.archive.name}",
            }],
        }
        target.write_text(json.dumps(value), encoding="utf-8")

    def _game(self, name: str) -> Path:
        root = self.work / name
        (root / "DS" / "Binaries" / "Win64").mkdir(parents=True)
        (root / "DS" / "Content" / "Paks").mkdir(parents=True)
        (root / "DSClient.exe").write_bytes(b"launcher")
        (root / "DS" / "Binaries" / "Win64" / "DSClient-Win64-Shipping.exe").write_bytes(b"shipping")
        return root

    def _install(self, game: Path):
        return installer.install_main(
            game, installer.Reporter(), skip_system=True,
            metadata_path=self.metadata, asset_file=self.archive,
        )

    def test_release_and_happy_path(self) -> None:
        release = installer.resolve_release(self.metadata)
        self.assertEqual(release.main.sha256, digest(self.archive))
        self.assertIsNone(release.guard)
        game = self._game("happy")
        result, _ = self._install(game)
        self.assertEqual(result.files_verified, 12)
        pak = game / "DS" / "Content" / "Paks" / "mods" / "DS_HIDDEN_HEROES_P.pak"
        self.assertEqual(digest(pak), digest(self.payload / pak.relative_to(game)))
        receipt = json.loads(installer.receipt_path(game).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "installed")
        self.assertEqual(len(receipt["files"]), 12)
        settings = (game / "DS" / "Binaries" / "Win64" / "ue4ss" / "UE4SS-settings.ini").read_text(encoding="utf-8")
        mods = str(game / "DS" / "Binaries" / "Win64" / "ue4ss" / "QualificationMods").replace("\\", "/")
        self.assertIn(f"ModsFolderPath = {mods}", settings)
        self.assertIn(f"ControllingModsTxt = {mods}/mods.txt", settings)

    def test_ue4ss_configuration_rejects_missing_contract(self) -> None:
        game = self._game("bad_settings")
        settings = game / "DS" / "Binaries" / "Win64" / "ue4ss" / "UE4SS-settings.ini"
        settings.parent.mkdir(parents=True)
        settings.write_text("ModsFolderPath = old\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallerError, "Could not configure UE4SS paths"):
            installer.configure_ue4ss(game)

    def test_uninstall_moves_owned_runtime_and_preserves_saved(self) -> None:
        game = self._game("uninstall")
        saved = game / "DS" / "Saved" / "account.sav"
        saved.parent.mkdir(parents=True)
        saved.write_bytes(b"keep")
        self._install(game)
        runtime = game / "DS" / "Binaries" / "Win64" / "ue4ss" / "runtime-state.bin"
        runtime.write_bytes(b"generated")
        result = installer.uninstall_main(game)
        deleted = Path(result.deleted_root)
        self.assertEqual(saved.read_bytes(), b"keep")
        self.assertFalse((game / "DS" / "Binaries" / "Win64" / "dwmapi.dll").exists())
        self.assertTrue((deleted / runtime.relative_to(game)).is_file())
        self.assertTrue((deleted / "DS" / "Content" / "Paks" / "mods" / "DS_HIDDEN_HEROES_P.pak").is_file())
        self.assertIn("deleted_hidden_heroes", str(deleted))

    def test_uninstall_restores_preexisting_ue4ss_and_moves_module_runtime(self) -> None:
        game = self._game("restore")
        old = game / "DS" / "Binaries" / "Win64" / "ue4ss" / "UE4SS.dll"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        self._install(game)
        runtime = game / "DS" / "Binaries" / "Win64" / "ue4ss" / "QualificationMods" / "DSAbuseCompat" / "Runtime" / "state.bin"
        runtime.parent.mkdir(parents=True)
        runtime.write_bytes(b"generated")
        result = installer.uninstall_main(game)
        self.assertEqual(old.read_bytes(), b"old")
        self.assertTrue((Path(result.deleted_root) / runtime.relative_to(game)).is_file())
        self.assertEqual(result.files_restored, 1)

    def test_uninstall_finishes_root_writes_before_guard_relock(self) -> None:
        game = self._game("uninstall_guard_order")
        self._install(game)
        real_write_json = installer.write_json
        guard_locked = {"value": True}
        actions: list[str] = []

        def guard_active(_root: Path) -> bool:
            return guard_locked["value"]

        def run_guard(action: str, _root: Path) -> None:
            actions.append(action)
            guard_locked["value"] = action == "lock"

        def reject_root_write_after_lock(path: Path, value: object) -> None:
            if guard_locked["value"] and installer.inside(path, game):
                raise PermissionError(f"write attempted after Guard relock: {path}")
            real_write_json(path, value)

        with (
            mock.patch.object(installer, "guard_active_for", side_effect=guard_active),
            mock.patch.object(installer, "run_guard", side_effect=run_guard),
            mock.patch.object(installer, "write_json", side_effect=reject_root_write_after_lock),
        ):
            result = installer.uninstall_main(game, allow_guard_disable=True)

        report = json.loads((Path(result.deleted_root) / "uninstall-report.json").read_text(encoding="utf-8"))
        self.assertEqual(actions, ["unlock", "lock"])
        self.assertTrue(result.guard_was_restored)
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["guard_restore_requested"])

    def test_bad_digest_and_traversal_do_not_mutate_game(self) -> None:
        game = self._game("bad")
        bad = self.work / "bad.json"
        self._metadata(bad, "0" * 64)
        with self.assertRaisesRegex(installer.InstallerError, "SHA-256 mismatch"):
            installer.install_main(game, installer.Reporter(), skip_system=True, metadata_path=bad, asset_file=self.archive)
        self.assertFalse((game / "DS" / "Content" / "Paks" / "mods").exists())
        traversal = self.work / "traversal.zip"
        shutil.copy2(self.archive, traversal)
        with zipfile.ZipFile(traversal, "a") as package:
            package.writestr("../escaped.txt", "escape")
        with self.assertRaisesRegex(installer.InstallerError, "Unsafe ZIP path"):
            installer.extract_main_archive(traversal, self.work / "staging", installer.Reporter())
        self.assertFalse((self.work / "escaped.txt").exists())

    def test_manifest_tamper_is_rejected(self) -> None:
        tampered = self.work / "tampered.zip"
        shutil.copy2(self.archive, tampered)
        with zipfile.ZipFile(tampered, "a") as package:
            package.writestr("DS/Binaries/Win64/ue4ss/UE4SS.dll", b"tampered")
        with self.assertRaisesRegex(installer.InstallerError, "Duplicate ZIP path"):
            installer.extract_main_archive(tampered, self.work / "tampered_staging", installer.Reporter())

    def test_apply_failure_rolls_back(self) -> None:
        game = self._game("rollback")
        old = game / "DS" / "Binaries" / "Win64" / "ue4ss" / "UE4SS.dll"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        with mock.patch.object(installer, "verify_files", side_effect=installer.InstallerError("forced verification failure")):
            with self.assertRaisesRegex(installer.InstallerError, "forced verification failure"):
                self._install(game)
        self.assertEqual(old.read_bytes(), b"old")
        self.assertFalse((game / "DS" / "Content" / "Paks" / "mods" / "DS_HIDDEN_HEROES_P.pak").exists())

    def test_conflicting_ggo_pak_is_rejected_before_download(self) -> None:
        game = self._game("conflict")
        conflict = game / "DS" / "Content" / "Paks" / "mods" / "DS_GGO_P.pak"
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"conflict")
        with mock.patch.object(installer, "resolve_release") as resolve:
            with self.assertRaisesRegex(installer.InstallerError, "mutually exclusive"):
                self._install(game)
        resolve.assert_not_called()

    def test_frozen_location_and_uninstall_shortcut(self) -> None:
        game = self._game("root")
        executable = game / "Hidden_Heroes_Installer.exe"
        executable.write_bytes(b"fixture")
        with mock.patch.object(installer.sys, "frozen", True, create=True), mock.patch.object(installer.sys, "executable", str(executable)):
            self.assertTrue(installer.same_path(installer.executable_game_root(), game))
            self.assertFalse(installer.same_path(installer.executable_game_root(), game / "different"))
        result, release = self._install(game)
        receipt = json.loads(installer.receipt_path(game).read_text(encoding="utf-8"))
        backup = Path(result.backup_root)
        plans = [installer.FilePlan(item["relative"], game / item["relative"], (game / item["relative"]).stat().st_size) for item in receipt["files"]]
        records = [{"relative": item.relative, "existed": False} for item in plans]
        with mock.patch.object(installer.sys, "frozen", True, create=True), mock.patch.object(installer.sys, "executable", str(executable)), mock.patch.object(installer, "create_shortcut") as create:
            installer.write_install_receipt(installer.norm(game), release, plans, backup, records, [], [], False)
        self.assertEqual(create.call_args.args[0], installer.norm(game) / installer.UNINSTALL_SHORTCUT)
        self.assertIn("deleted_hidden_heroes", create.call_args.args[4])

    def test_guard_v2_is_deployed_to_game_root(self) -> None:
        game = self._game("guard")
        guard_zip = self.work / "DragonSword_Update_Guard.zip"
        guard_bytes = os.urandom(1024 * 1024)
        with zipfile.ZipFile(guard_zip, "w", zipfile.ZIP_STORED) as package:
            package.writestr("DragonSwordGuard.exe", guard_bytes)
        asset = installer.Asset(guard_zip.name, "https://example.invalid/guard.zip", guard_zip.stat().st_size, digest(guard_zip))
        release = installer.Release("fixture", installer.resolve_release(self.metadata).main, asset)
        completed = mock.Mock(returncode=0)
        with mock.patch.object(installer, "can_install_guard", return_value=True), mock.patch.object(installer, "guard_active_for", return_value=True), mock.patch.object(installer.subprocess, "run", return_value=completed) as run:
            installer.install_guard(release, game, installer.Reporter(), guard_zip)
        deployed = game / "DragonSwordGuard.exe"
        self.assertEqual(deployed.read_bytes(), guard_bytes)
        self.assertEqual(Path(run.call_args.args[0][0]), deployed)
        self.assertEqual(run.call_args.args[0][1], "install")
        with mock.patch.object(installer, "can_install_guard", return_value=True), mock.patch.object(installer, "guard_active_for", return_value=False), mock.patch.object(installer.subprocess, "run", return_value=completed) as setup_run:
            installer.install_guard(release, game, installer.Reporter(), guard_zip, enable_now=False)
        self.assertEqual(setup_run.call_args.args[0][1], "setup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
