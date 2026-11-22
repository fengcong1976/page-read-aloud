# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('venv/Lib/site-packages/wordninja/wordninja_words.txt.gz', 'wordninja'),
    ('图标.ico', '.'),
    ('图标.png', '.'),
    ('ecdict.db', '.'),   # ECDICT（MIT）
    ('cedict.db', '.'),   # CC-CEDICT（CC BY-SA 4.0）
    ('idiom.db', '.'),    # chinese-xinhua 成语数据（MIT）
]
binaries = []
hiddenimports = ['PyQt6.QtMultimedia', 'PyQt6.sip']
for _pkg in ('rapidocr_onnxruntime', 'wordninja', 'onnxruntime', 'pymupdf', 'edge_tts'):
    _ret = collect_all(_pkg)
    datas += _ret[0]; binaries += _ret[1]; hiddenimports += _ret[2]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='书页朗读助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['图标.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='书页朗读助手',
)
