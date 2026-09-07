import os
import tempfile
import unittest
from pathlib import Path
from threading import Event

from duplicate_finder import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, Cancelled, scan, validate_selection


class DuplicateFinderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, extensions=IMAGE_EXTENSIONS | VIDEO_EXTENSIONS):
        return scan(self.root, extensions, Event())[0]

    def test_nested_duplicates_and_equal_size_different_content(self):
        a = self.write('a.jpg', b'abc')
        b = self.write('nested/b.PNG', b'abc')
        self.write('different.jpg', b'xyz')
        self.write('ignored.txt', b'abc')
        groups = self.scan()
        self.assertEqual(len(groups), 1)
        self.assertEqual({f.path for f in groups[0].files}, {a, b})

    def test_type_filter(self):
        self.write('a.mp4', b'video')
        self.write('b.mov', b'video')
        self.assertEqual(self.scan(IMAGE_EXTENSIONS), [])
        self.assertEqual(len(self.scan(VIDEO_EXTENSIONS)), 1)

    def test_cannot_select_every_copy(self):
        self.write('a.png', b'abc')
        self.write('b.png', b'abc')
        group = self.scan()[0]
        with self.assertRaises(ValueError):
            validate_selection(group, {f.path for f in group.files}, Event())
        self.assertEqual(len(validate_selection(group, {group.files[1].path}, Event())), 1)

    def test_changed_keeper_blocks_cleanup_even_with_same_size_and_mtime(self):
        a = self.write('a.png', b'abc')
        b = self.write('b.png', b'abc')
        group = self.scan()[0]
        previous = a.stat()
        a.write_bytes(b'xyz')
        os.utime(a, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.assertRaises((OSError, ValueError)):
            validate_selection(group, {b}, Event())

    def test_missing_keeper_blocks_cleanup(self):
        a = self.write('a.png', b'abc')
        b = self.write('b.png', b'abc')
        group = self.scan()[0]
        a.unlink()
        with self.assertRaises(OSError):
            validate_selection(group, {b}, Event())

    def test_hard_links_are_not_wasted_space(self):
        a = self.write('a.png', b'abc')
        os.link(a, self.root / 'b.png')
        self.assertEqual(self.scan(), [])

    def test_cancel(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            scan(self.root, IMAGE_EXTENSIONS, cancel)


if __name__ == '__main__':
    unittest.main()
