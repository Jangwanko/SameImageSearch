"""Local visual candidate detection. Scores are heuristics, not probabilities."""
from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import cv2
import numpy as np
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from duplicate_finder import (Cancelled, DuplicateGroup, MediaFile, VIDEO_EXTENSIONS,
                              _is_link, digest_file, snapshot)

register_heif_opener()
SAMPLE_POINTS = (0.1, 0.3, 0.5, 0.7, 0.9)
THRESHOLDS = {'엄격': 5, '보통': 9, '넓게': 13}


@dataclass(frozen=True)
class Signature:
    phash: int
    colors: tuple[float, ...]


@dataclass(frozen=True)
class VisualInfo:
    width: int
    height: int
    duration: float
    kind: str
    signatures: tuple[Signature, ...]


@dataclass(frozen=True)
class Record:
    file: MediaFile
    digest: str
    info: VisualInfo | None


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled()


def rgb_image(image):
    image = ImageOps.exif_transpose(image)
    if image.mode == 'RGB' and 'transparency' not in image.info:
        return image
    rgba = image.convert('RGBA')
    background = Image.new('RGBA', rgba.size, 'white')
    return Image.alpha_composite(background, rgba).convert('RGB')


def signature(image: Image.Image) -> Signature:
    gray = np.asarray(image.convert('L').resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
    low = cv2.dct(gray)[:8, :8].flatten()[1:]
    # Quantization suppresses near-zero numerical noise in simple graphics.
    low = np.round(low, 3)
    bits = low > np.median(low)
    value = sum(int(bit) << i for i, bit in enumerate(bits))
    colors = np.asarray(image.resize((4, 4), Image.Resampling.BOX), dtype=np.float32).flatten()
    return Signature(value, tuple(float(v) for v in colors))


def video_frames(path, cancel, points=SAMPLE_POINTS):
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise OSError('영상 코덱을 읽을 수 없습니다.')
        fps = capture.get(cv2.CAP_PROP_FPS)
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if not math.isfinite(fps) or not math.isfinite(count) or fps <= 0 or count < 1:
            raise OSError('영상 길이를 확인할 수 없습니다.')
        duration = count / fps
        frames = []
        for fraction in points:
            check_cancel(cancel)
            if not capture.set(cv2.CAP_PROP_POS_MSEC, duration * fraction * 1000):
                raise OSError('영상 탐색을 지원하지 않습니다.')
            ok, frame = capture.read()
            if not ok:
                raise OSError('영상 샘플 프레임을 읽을 수 없습니다.')
            frames.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        return duration, frames
    finally:
        capture.release()


def inspect_media(path, cancel):
    check_cancel(cancel)
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        duration, frames = video_frames(path, cancel)
        width, height = frames[0].size
        return VisualInfo(width, height, duration, 'video', tuple(signature(frame) for frame in frames))
    with Image.open(path) as source:
        if getattr(source, 'n_frames', 1) > 1:
            raise OSError('애니메이션·다중 페이지 이미지는 완전 중복만 비교합니다.')
        image = rgb_image(source)
        return VisualInfo(*image.size, 0.0, 'image', (signature(image),))


def preview(path: Path, point=0.5):
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return video_frames(path, Event(), (point,))[1][0]
    with Image.open(path) as source:
        return rgb_image(source)


def similarity(a: Record, b: Record, threshold: int) -> float | None:
    if a.digest == b.digest:
        return 100.0
    x, y = a.info, b.info
    if x is None or y is None or x.kind != y.kind:
        return None
    if abs((x.width / x.height) / (y.width / y.height) - 1) > .025:
        return None
    if x.kind == 'video' and abs(x.duration - y.duration) > max(.25, min(1., min(x.duration, y.duration) * .01)):
        return None
    distances = []
    for left, right in zip(x.signatures, y.signatures):
        distance = (left.phash ^ right.phash).bit_count()
        if distance > threshold:
            return None
        color_error = float(np.mean(np.abs(np.array(left.colors) - np.array(right.colors))))
        if color_error > 10 + threshold * 1.5:
            return None
        distances.append(max(distance / 63, color_error / 255))
    return round(100 * (1 - max(distances)), 1)


class CandidateIndex:
    """Exact radius prefilter over cluster representatives' first pHash.

    Split 63 bits into radius+1 disjoint blocks. If at most radius bits differ,
    at least one block is identical. Union those buckets, then apply the original
    full comparison. Digest lookup also preserves byte-identical fallback files.
    """
    def __init__(self, threshold):
        if not 0 <= threshold < 63:
            raise ValueError('유사도 임계값은 0~62 범위여야 합니다.')
        count = threshold + 1
        self.blocks = [(i * 63 // count, (1 << ((i + 1) * 63 // count - i * 63 // count)) - 1)
                       for i in range(count)]
        self.buckets = defaultdict(set)
        self.digests = defaultdict(set)

    def keys(self, record):
        if record.info and record.info.signatures:
            value = record.info.signatures[0].phash
            for i, (shift, mask) in enumerate(self.blocks):
                yield record.info.kind, i, (value >> shift) & mask

    def add(self, record, cluster, representative=False):
        self.digests[record.digest].add(cluster)
        if representative:
            for key in self.keys(record):
                self.buckets[key].add(cluster)

    def candidates(self, record):
        result = set(self.digests.get(record.digest, ()))
        for key in self.keys(record):
            result.update(self.buckets.get(key, ()))
        return sorted(result)  # Preserve the previous greedy grouping order.


def group_records(records, threshold, cancel, report=lambda _: None, *, use_index=True):
    # Highest pixel count is the proposed keeper; file size only breaks ties.
    records = sorted(records, key=lambda r: (
        -(r.info.width * r.info.height if r.info else 0), -r.file.size,
        str(r.file.path).casefold()))
    clusters: list[list[Record]] = []
    scores: list[float] = []
    search_index = CandidateIndex(threshold)
    for index, record in enumerate(records):
        check_cancel(cancel)
        if index % 25 == 0:
            report(f'유사 후보 비교 중 · {index + 1:,}/{len(records):,}')
        candidates = search_index.candidates(record) if use_index else range(len(clusters))
        for i in candidates:
            cluster = clusters[i]
            matches = []
            for other in cluster:
                check_cancel(cancel)
                score = similarity(record, other, threshold)
                if score is None:
                    break
                matches.append(score)
            else:
                cluster.append(record)
                search_index.add(record, i)
                scores[i] = min(scores[i], *matches)
                break
        else:
            clusters.append([record])
            scores.append(100.0)
            search_index.add(record, len(clusters) - 1, representative=True)
    groups = []
    for cluster, score in zip(clusters, scores):
        if len(cluster) < 2:
            continue
        exact = len({r.digest for r in cluster}) == 1
        groups.append(DuplicateGroup(cluster[0].digest, tuple(r.file for r in cluster),
                                     'exact' if exact else 'similar',
                                     tuple(r.info for r in cluster),
                                     tuple(r.digest for r in cluster), score))
    groups.sort(key=lambda g: sum(f.size for f in g.files[1:]), reverse=True)
    return groups


def scan_similar(root, extensions, cancel, report=lambda _: None, threshold=9):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('검색할 폴더를 선택해 주세요.')
    errors, records, files = [], [], []
    identities = set()
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=lambda e: errors.append(str(e))):
        check_cancel(cancel)
        dirs[:] = [name for name in dirs if not _is_link(Path(directory) / name)]
        for name in names:
            check_cancel(cancel)
            path = Path(directory) / name
            if path.suffix.lower() not in extensions or _is_link(path):
                continue
            try:
                file = snapshot(path)
                identity = (file.device, file.inode)
                if file.inode and identity in identities:
                    continue
                identities.add(identity)
                files.append(file)
            except OSError as error:
                errors.append(f'{path}: {error}')
    analyzed = {}
    for index, file in enumerate(files, 1):
        check_cancel(cancel)
        report(f'시각적 특징 분석 · {index:,}/{len(files):,} · {file.path.name}')
        try:
            digest = digest_file(file, cancel)
            info = None
            try:
                cache_key = (digest, file.path.suffix.lower())
                if cache_key in analyzed:
                    info = analyzed[cache_key]
                else:
                    info = inspect_media(file.path, cancel)
                    analyzed[cache_key] = info
            except (OSError, ValueError, cv2.error, Image.DecompressionBombError) as error:
                errors.append(f'{file.path}: 유사 비교 제외 · {error}')
            if snapshot(file.path) != file:
                raise OSError('분석 중 파일이 변경되었습니다.')
            records.append(Record(file, digest, info))
        except OSError as error:
            errors.append(f'{file.path}: {error}')
    return group_records(records, threshold, cancel, report), errors
