# Windows x64 onedir build. Run with the project's virtual environment.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ('pillow_heif',):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden

a = Analysis([str(root / 'packaging' / 'entry.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], runtime_hooks=[], excludes=['pytest', 'IPython'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SameImage',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SameImage')
