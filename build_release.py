"""Build and validate a ZIP release using the project's Python environment."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
VERSION = '0.1.0'


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build this Windows release on Windows.')
    os.chdir(ROOT)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', 'SameImage.spec'], check=True)
    bundle = ROOT / 'dist' / 'SameImage'
    shutil.copy2(ROOT / 'README.md', bundle / 'README.md')
    shutil.copy2(ROOT / 'packaging' / 'START-HERE.txt', bundle / 'START-HERE.txt')
    notices = bundle / 'THIRD-PARTY'
    notices.mkdir(exist_ok=True)
    versions = {}
    for name in ('Pillow', 'numpy', 'opencv-python-headless', 'pillow-heif'):
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        for file in distribution.files or []:
            if any(word in file.name.lower() for word in ('license', 'copying', 'notice')):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = notices / name / str(file).replace('../', '').replace('..\\', '')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', notices / 'Python-LICENSE.txt')
    for file in (Path(sys.base_prefix) / 'tcl').rglob('license*'):
        if file.is_file():
            destination = notices / 'Tcl-Tk' / file.relative_to(Path(sys.base_prefix) / 'tcl')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, destination)
    (bundle / 'VERSION.json').write_text(json.dumps({'version': VERSION, 'python': sys.version, 'dependencies': versions}, indent=2), encoding='utf-8')
    release = ROOT / 'release'
    release.mkdir(exist_ok=True)
    report = release / 'package-self-test.json'
    environment = dict(os.environ)
    environment.pop('PYTHONHOME', None)
    environment.pop('PYTHONPATH', None)
    environment['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    subprocess.run([str(bundle / 'SameImage.exe'), '--self-test', str(report)],
                   cwd=release, env=environment, check=True, timeout=90)
    archive = Path(shutil.make_archive(str(release / f'SameImage-{VERSION}-windows-x64'), 'zip', ROOT / 'dist', 'SameImage'))
    with archive.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    (release / 'SHA256SUMS.txt').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    print(f'Release: {archive}\nSize: {archive.stat().st_size / 1024 / 1024:.1f} MB\nSHA256: {checksum}')


if __name__ == '__main__':
    main()
