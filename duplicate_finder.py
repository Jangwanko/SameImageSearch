"""Exact media duplicate detection and conservative Windows recycling."""
from __future__ import annotations

import ctypes
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

IMAGE_EXTENSIONS = frozenset('.jpg .jpeg .png .gif .bmp .webp .tif .tiff .heic .heif .avif .ico .raw .cr2 .nef .arw .dng'.split())
VIDEO_EXTENSIONS = frozenset('.mp4 .mkv .mov .avi .wmv .webm .m4v .mpg .mpeg .mts .m2ts .3gp .flv .ts'.split())


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class MediaFile:
    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class DuplicateGroup:
    digest: str
    files: tuple[MediaFile, ...]
    kind: str = 'exact'
    details: tuple = ()
    digests: tuple[str, ...] = ()
    score: float = 100.0


def snapshot(path: Path) -> MediaFile:
    stat = path.stat()
    return MediaFile(path, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)


def digest_file(file: MediaFile, cancel: Event) -> str:
    if file.path.is_symlink() or snapshot(file.path) != file:
        raise OSError('검색 중 파일이 변경되었습니다.')
    digest = hashlib.sha256()
    with file.path.open('rb') as stream:
        while True:
            if cancel.is_set():
                raise Cancelled()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if snapshot(file.path) != file:
        raise OSError('검색 중 파일이 변경되었습니다.')
    return digest.hexdigest()


def scan(root: Path, extensions: frozenset[str], cancel: Event,
         report: Callable[[str], None] = lambda _: None) -> tuple[list[DuplicateGroup], list[str]]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('검색할 폴더를 선택해 주세요.')
    by_size: dict[int, list[MediaFile]] = defaultdict(list)
    errors: list[str] = []
    identities: set[tuple[int, int]] = set()
    count = 0

    def walk_error(error: OSError) -> None:
        errors.append(str(error))

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        if cancel.is_set():
            raise Cancelled()
        # Junctions may lead outside the selected tree or back into it.
        dirs[:] = [name for name in dirs if not _is_link(Path(directory) / name)]
        for name in names:
            if cancel.is_set():
                raise Cancelled()
            path = Path(directory) / name
            if path.suffix.lower() not in extensions or _is_link(path):
                continue
            try:
                file = snapshot(path)
                identity = (file.device, file.inode)
                if file.inode and identity in identities:
                    continue  # Hard links already share disk storage.
                identities.add(identity)
                by_size[file.size].append(file)
                count += 1
                if count % 100 == 0:
                    report(f'미디어 파일 확인 중 · {count:,}개')
            except OSError as error:
                errors.append(f'{path}: {error}')
    candidates = [file for files in by_size.values() if len(files) > 1 for file in files]
    by_digest: dict[tuple[int, str], list[MediaFile]] = defaultdict(list)
    for index, file in enumerate(candidates, 1):
        if cancel.is_set():
            raise Cancelled()
        report(f'내용 비교 중 · {index:,}/{len(candidates):,} · {file.path.name}')
        try:
            by_digest[(file.size, digest_file(file, cancel))].append(file)
        except OSError as error:
            errors.append(f'{file.path}: {error}')
    groups = [DuplicateGroup(digest, tuple(sorted(files, key=lambda f: str(f.path).casefold())))
              for (_, digest), files in by_digest.items() if len(files) > 1]
    groups.sort(key=lambda group: group.files[0].size * (len(group.files) - 1), reverse=True)
    report(f'검색 완료 · 미디어 {count:,}개 · 중복 그룹 {len(groups):,}개')
    return groups, errors


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True


def validate_selection(group: DuplicateGroup, selected: set[Path], cancel: Event) -> list[MediaFile]:
    targets = [file for file in group.files if file.path in selected]
    keepers = [file for file in group.files if file.path not in selected]
    if not targets:
        return []
    if not keepers:
        raise ValueError('각 그룹에서 최소 한 파일을 남겨야 합니다.')
    # Recheck a retained copy and every selected file before any mutation.
    if digest_file(keepers[0], cancel) != expected_digest(group, keepers[0]):
        raise ValueError('보관할 파일의 내용이 변경되었습니다. 다시 검색해 주세요.')
    for file in targets:
        if digest_file(file, cancel) != expected_digest(group, file):
            raise ValueError('선택한 파일의 내용이 변경되었습니다. 다시 검색해 주세요.')
    return targets


def expected_digest(group: DuplicateGroup, file: MediaFile) -> str:
    if group.kind == 'similar':
        return group.digests[group.files.index(file)]
    return group.digest


def recycle_file(path: Path) -> None:
    if os.name != 'nt':
        raise OSError('휴지통 정리는 Windows에서만 지원합니다.')

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', ctypes.c_void_p), ('wFunc', ctypes.c_uint),
                    ('pFrom', ctypes.c_void_p), ('pTo', ctypes.c_void_p),
                    ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', ctypes.c_int),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', ctypes.c_wchar_p)]

    # FOF_ALLOWUNDO without FOF_NOCONFIRMATION: Windows can ask when recycling
    # is unavailable (network/removable volumes, oversized files, etc.).
    source = ctypes.create_unicode_buffer(str(path.resolve()) + '\0\0')
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = ctypes.cast(source, ctypes.c_void_p)
    operation.fFlags = 0x0040 | 0x0400  # ALLOWUNDO | NOERRORUI
    shell = ctypes.windll.shell32.SHFileOperationW
    shell.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell.restype = ctypes.c_int
    result = shell(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError(f'휴지통 이동이 취소되었거나 실패했습니다. (코드 {result})')
