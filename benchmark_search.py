"""Reproducible in-memory candidate comparison benchmark (no user files)."""
import random
import time
from pathlib import Path
from threading import Event

from duplicate_finder import MediaFile
from similarity import Record, Signature, VisualInfo, group_records


def sample_records(count=1200):
    rng = random.Random(420)
    records = []
    for i in range(count):
        info = VisualInfo(640, 480, 0, 'image',
                          (Signature(rng.getrandbits(63), (128.,) * 48),))
        records.append(Record(MediaFile(Path(f'{i:05}.jpg'), 1024, 0, 1, i + 1), str(i), info))
    return records


if __name__ == '__main__':
    records = sample_records()
    start = time.perf_counter()
    groups = group_records(records, 5, Event())
    print(f'1200 synthetic image signatures, strict: {time.perf_counter() - start:.3f}s, {len(groups)} groups')
