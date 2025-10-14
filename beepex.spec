# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['beepex.py'],
    datas=[
        ("css/*.css", "css")
    ],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='beepex',
    bootloader_ignore_signals=False,
    strip=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    name='beepex',
)
