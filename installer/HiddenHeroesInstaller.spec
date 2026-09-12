# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['hidden_heroes_installer.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Hidden_Heroes_Installer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Packed executables are disproportionately likely to trigger heuristic AV
    # detections. Keep the public build transparent even if it is slightly larger.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    version='version_info.txt',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
