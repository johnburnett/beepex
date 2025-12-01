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
    a.binaries,
    a.datas,
    strip=True,
    name='beepex',
)
