import tempfile
import unittest
import random
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw

from duplicate_finder import (IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, Cancelled,
                              snapshot, validate_selection)
from similarity import (Record, Signature, VisualInfo, CandidateIndex, group_records,
                        inspect_media, scan_similar, rgb_image)
from duplicate_finder import MediaFile


def scene(seed=17, size=(640, 480)):
    rng = np.random.default_rng(seed)
    image = Image.new('RGB', size, '#c5ddf0')
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y = rng.integers(0, 400, 2)
        w, h = rng.integers(20, 180, 2)
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        draw.ellipse((int(x), int(y), int(x + w), int(y + h)), fill=color)
    return image


class SimilarityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def scan(self):
        return scan_similar(self.root, IMAGE_EXTENSIONS | VIDEO_EXTENSIONS, Event())

    def test_index_never_loses_in_radius_candidate(self):
        rng = random.Random(23)
        for threshold in (5, 9, 13):
            for trial in range(100):
                value = rng.getrandbits(63)
                record = Record(MediaFile(Path('a'), 1, 0, 1, 1), 'a',
                                VisualInfo(100, 100, 0, 'image', (Signature(value, (128.,) * 48),)))
                index = CandidateIndex(threshold)
                index.add(record, 0, representative=True)
                changed = value
                for bit in rng.sample(range(63), threshold):
                    changed ^= 1 << bit
                other = replace(record, digest='b', info=replace(record.info,
                                  signatures=(Signature(changed, (128.,) * 48),)))
                self.assertIn(0, index.candidates(other))

    def test_index_matches_exhaustive_groups_and_scores(self):
        rng = random.Random(199)
        records = []
        for i in range(180):
            value = rng.getrandbits(63) if i % 6 == 0 else records[-1].info.signatures[0].phash
            for bit in rng.sample(range(63), i % 10):
                value ^= 1 << bit
            file = MediaFile(Path(f'{i:04}.png'), 100 + i, 0, 1, i + 1)
            records.append(Record(file, str(i), VisualInfo(640, 480, 0, 'image', (Signature(value, (128.,) * 48),))))
        records.extend([replace(records[0], file=replace(records[0].file, path=Path('x')), info=None),
                        replace(records[0], file=replace(records[0].file, path=Path('y')), info=None)])
        for threshold in (5, 9, 13):
            with self.subTest(threshold=threshold):
                self.assertEqual(group_records(records, threshold, Event()),
                                 group_records(records, threshold, Event(), use_index=False))

    def test_identical_bytes_are_decoded_once_per_scan(self):
        scene().save(self.root / 'a.png')
        (self.root / 'b.png').write_bytes((self.root / 'a.png').read_bytes())
        with patch('similarity.inspect_media', wraps=inspect_media) as inspect:
            groups, errors = self.scan()
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(errors, [])
        self.assertEqual(groups[0].kind, 'exact')

    def test_rgb_fast_path_and_transparency_keep_same_pixels(self):
        rgb = scene()
        self.assertEqual(rgb_image(rgb).tobytes(), rgb.tobytes())
        transparent = Image.new('RGBA', (10, 10), (255, 0, 0, 0))
        self.assertEqual(rgb_image(transparent).getpixel((0, 0)), (255, 255, 255))

    def test_resize_format_compression_and_keeper(self):
        source = scene()
        source.save(self.root / 'original.png')
        source.resize((320, 240)).save(self.root / 'small.jpg', quality=75)
        source.resize((160, 120)).save(self.root / 'tiny.webp', quality=70)
        scene(83).save(self.root / 'different.png')
        groups, errors = self.scan()
        self.assertEqual(errors, [])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(len(group.files), 3)
        self.assertEqual(group.kind, 'similar')
        self.assertEqual(group.files[0].path.name, 'original.png')
        # Different byte hashes are valid only if each file remains unchanged.
        selected = {f.path for f in group.files[1:]}
        self.assertEqual(len(validate_selection(group, selected, Event())), 2)
        (self.root / 'original.png').write_bytes(b'changed')
        with self.assertRaises(OSError):
            validate_selection(group, selected, Event())

    def test_distinct_solid_colors_do_not_match(self):
        Image.new('RGB', (100, 100), 'red').save(self.root / 'red.png')
        Image.new('RGB', (200, 200), 'blue').save(self.root / 'blue.png')
        self.assertEqual(self.scan()[0], [])

    def test_exif_orientation(self):
        source = scene()
        source.save(self.root / 'normal.png')
        exif = Image.Exif()
        exif[274] = 6
        source.transpose(Image.Transpose.ROTATE_90).save(self.root / 'rotated.jpg', exif=exif, quality=95)
        groups, errors = self.scan()
        self.assertEqual(errors, [])
        self.assertEqual(len(groups), 1)
        self.assertEqual({(d.width, d.height) for d in groups[0].details}, {(640, 480)})

    def test_unreadable_visual_still_finds_exact_duplicate(self):
        (self.root / 'a.jpg').write_bytes(b'not really an image')
        (self.root / 'b.png').write_bytes(b'not really an image')
        groups, errors = self.scan()
        self.assertEqual(len(errors), 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, 'exact')

    def test_no_transitive_similarity_chain(self):
        records = []
        for name in ('a', 'b', 'c'):
            path = self.root / f'{name}.png'
            path.write_bytes(name.encode())
            records.append(Record(snapshot(path), name, None))

        def score(a, b, _threshold):
            return None if {a.digest, b.digest} == {'a', 'c'} else 95

        with patch('similarity.similarity', side_effect=score):
            groups = group_records(records, 9, Event(), use_index=False)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].files), 2)

    def make_video(self, name, size, count=30, seed=17, codec='MJPG'):
        path = self.root / name
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), 10, size)
        self.assertTrue(writer.isOpened(), f'Test encoder unavailable: {codec}')
        try:
            base = scene(seed)
            for index in range(count):
                frame = base.copy()
                ImageDraw.Draw(frame).rectangle((index * 8, 20, index * 8 + 40, 80), fill='white')
                writer.write(cv2.cvtColor(np.asarray(frame.resize(size)), cv2.COLOR_RGB2BGR))
        finally:
            writer.release()
        return path

    def test_video_different_resolution_and_codec(self):
        self.make_video('original.avi', (640, 480))
        self.make_video('small.mp4', (320, 240), codec='mp4v')
        self.make_video('longer.avi', (320, 240), count=45)
        self.make_video('unrelated.avi', (320, 240), seed=91)
        groups, errors = self.scan()
        self.assertEqual(errors, [])
        self.assertEqual(len(groups), 1)
        self.assertEqual({f.path.name for f in groups[0].files}, {'original.avi', 'small.mp4'})
        self.assertEqual(groups[0].files[0].path.name, 'original.avi')
        self.assertEqual(len(groups[0].details[0].signatures), 5)

    def test_cancel(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            scan_similar(self.root, IMAGE_EXTENSIONS, cancel)


if __name__ == '__main__':
    unittest.main()
