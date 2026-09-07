import tempfile
import gc
import time
import tkinter as tk
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch
from dataclasses import replace

from PIL import Image, ImageDraw

from app import App
from duplicate_finder import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, scan, snapshot, expected_digest
from similarity import scan_similar


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        image = Image.new('RGB', (400, 300), 'lightblue')
        draw = ImageDraw.Draw(image)
        draw.ellipse((25, 50, 200, 220), fill='red')
        draw.rectangle((210, 20, 360, 270), fill='green')
        image.save(root / 'original.png')
        image.resize((200, 150)).save(root / 'small.jpg')
        self.app = App()
        self.app.withdraw()
        self.app.update()
        self.addCleanup(self.dispose_app)
        self.app.groups, errors = scan_similar(root, IMAGE_EXTENSIONS, Event())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.app.groups), 1)
        self.app.render()

    def dispose_app(self):
        self.app.destroy()
        self.app.preview_loader.worker.join(timeout=3)
        self.app = None
        gc.collect()

    def wait_for(self, condition):
        deadline = time.monotonic() + 8
        while not condition() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertTrue(condition(), 'UI worker did not complete')

    def add_second_group(self):
        group = self.app.groups[0]
        copies = []
        for file in group.files:
            path = file.path.with_name('second-' + file.path.name)
            path.write_bytes(file.path.read_bytes())
            copies.append(snapshot(path))
        second = replace(group, files=tuple(copies))
        self.app.groups.append(second)
        self.app.render()
        return second

    def comparison_buttons(self, window, prefix, widget_class='TButton'):
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        return [w for w in descendants(window) if w.winfo_class() == widget_class and str(w.cget('text')).startswith(prefix)]

    def test_four_files_can_keep_two_with_draft_and_all_selected_guard(self):
        group = self.app.groups[0]
        copies = []
        for i in (2, 3):
            path = Path(self.temp.name) / f'extra{i}.png'
            path.write_bytes(group.files[0].path.read_bytes())
            copies.append(snapshot(path))
        group = replace(group, files=group.files + tuple(copies),
                        details=group.details + (group.details[0],) * 2,
                        digests=group.digests + (group.digests[0],) * 2)
        self.app.groups = [group]
        self.app.render()
        self.app.compare()
        window = self.app.comparison_window
        self.wait_for(lambda: len(window.preview_photos) == 4)
        checks = self.comparison_buttons(window, '휴지통으로', 'TCheckbutton')
        self.assertEqual(len(checks), 4)
        self.assertTrue(all(not check.instate(['selected']) for check in checks))
        checks[1].invoke()
        checks[3].invoke()
        checks[0].invoke()
        checks[2].invoke()  # The last retained file cannot be selected.
        self.assertFalse(checks[2].instate(['selected']))
        checks[0].invoke()
        self.assertEqual(self.app.selected, set())  # Not committed yet.
        self.comparison_buttons(window, '닫기')[0].invoke()
        self.app.compare()
        window = self.app.comparison_window
        self.wait_for(lambda: len(window.preview_photos) == 4)
        checks = self.comparison_buttons(window, '휴지통으로', 'TCheckbutton')
        self.assertEqual([check.instate(['selected']) for check in checks], [False, True, False, True])
        self.comparison_buttons(window, '선택 확정')[0].invoke()
        self.assertEqual(self.app.selected, {group.files[1].path, group.files[3].path})
        self.assertIn(id(group), self.app.reviewed)

    def test_comparison_advances_and_previous_can_change_keeper(self):
        self.add_second_group()
        first = self.app.groups[0]
        self.app.compare()
        window = self.app.comparison_window
        self.wait_for(lambda: len(window.preview_photos) == 2)
        self.comparison_buttons(window, '이 파일만 보관')[0].invoke()
        self.comparison_buttons(window, '선택 확정')[0].invoke()
        self.assertIn('2/2', self.app.comparison_window.title())
        self.assertEqual(self.app.selected, {first.files[1].path})
        self.comparison_buttons(self.app.comparison_window, '← 이전')[0].invoke()
        window = self.app.comparison_window
        self.wait_for(lambda: len(window.preview_photos) == 2)
        self.comparison_buttons(window, '이 파일만 보관')[1].invoke()
        self.comparison_buttons(window, '선택 확정')[0].invoke()
        self.assertEqual(self.app.selected, {first.files[0].path})
        self.assertIn('2/2', self.app.comparison_window.title())
        self.comparison_buttons(self.app.comparison_window, '닫기')[0].invoke()

    def test_cleanup_preserves_other_search_results(self):
        other = self.add_second_group()
        first = self.app.groups[0]
        self.app.reviewed.add(id(first))
        self.app.selected = {first.files[1].path}
        with patch('app.messagebox.askyesno', return_value=True), patch('app.recycle_file') as recycle:
            self.app.start_cleanup()
            self.wait_for(lambda: not self.app.busy)
        self.assertEqual(recycle.call_count, 1)
        self.assertEqual(self.app.groups, [other])
        self.assertEqual(len(self.app.rows), 2)
        self.assertEqual(self.app.selected, set())

    def test_partial_cleanup_keeps_hashes_review_and_failed_selection(self):
        group = self.app.groups[0]
        path = Path(self.temp.name) / 'third.png'
        path.write_bytes(group.files[0].path.read_bytes())
        group = replace(group, files=group.files + (snapshot(path),),
                        details=group.details + (group.details[0],),
                        digests=group.digests + (group.digests[0],))
        self.app.groups = [group]
        self.app.render()
        self.app.reviewed.add(id(group))
        self.app.selected = {group.files[1].path, path}
        with patch('app.messagebox.askyesno', return_value=True), patch('app.messagebox.showerror'), \
                patch('app.recycle_file', side_effect=[None, OSError('test failure')]):
            self.app.start_cleanup()
            self.wait_for(lambda: not self.app.busy)
        remaining = self.app.groups[0]
        self.assertEqual([f.path for f in remaining.files], [group.files[0].path, path])
        self.assertEqual(self.app.selected, {path})
        self.assertIn(id(remaining), self.app.reviewed)
        self.assertEqual(expected_digest(remaining, remaining.files[1]), group.digests[0])
        self.app.apply_cleanup_result([])
        self.assertIs(self.app.groups[0], remaining)

    def test_bulk_selection_and_toggle_require_visual_review(self):
        self.app.select_duplicates()
        self.assertEqual(self.app.selected, set())
        self.app.toggle(next(iter(self.app.rows)))
        self.assertEqual(self.app.selected, set())

    def test_windowed_layout_keeps_cleanup_and_toolbar_visible_at_dpi_scales(self):
        original_init = tk.Tk.__init__
        for scale, geometry in ((1.333, '900x640'), (2.0, '1100x760'), (2.666, '1280x900')):
            with self.subTest(scale=scale, geometry=geometry):
                self.dispose_app()
                def scaled_init(root, *args, **kwargs):
                    original_init(root, *args, **kwargs)
                    root.tk.call('tk', 'scaling', scale)
                with patch.object(tk.Tk, '__init__', scaled_init):
                    self.app = App()
                self.app.geometry(geometry)
                self.app.update()
                for button in [self.app.clean, *self.app.action_buttons]:
                    self.assertTrue(button.winfo_ismapped())
                    x = button.winfo_rootx() - self.app.winfo_rootx()
                    y = button.winfo_rooty() - self.app.winfo_rooty()
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + button.winfo_width(), self.app.winfo_width())
                    self.assertLessEqual(y + button.winfo_height(), self.app.winfo_height())
                self.assertGreater(self.app.tree.winfo_height(), 60)
                self.assertLess(self.app.clean.winfo_rooty(), self.app.tree.winfo_rooty())

    def test_visible_thumbnails_and_detail_do_not_select_for_cleanup(self):
        self.app.deiconify()
        self.app.update()
        self.app.load_visible_thumbnails()
        self.wait_for(lambda: len(self.app.thumbnail_photos) == 2 and self.app.detail_photo is not None)
        for row, photo in self.app.thumbnail_photos.items():
            self.assertEqual((photo.width(), photo.height()), (112, 70))
            self.assertTrue(self.app.tree.item(row, 'image'))
        self.assertEqual((self.app.detail_photo.width(), self.app.detail_photo.height()), (320, 180))
        second = list(self.app.rows)[1]
        self.app.tree.focus(second)
        self.app.tree.selection_set(second)
        self.app.show_detail()
        self.wait_for(lambda: self.app.detail_photo is not None)
        self.assertEqual(self.app.detail_title.get(), 'small.jpg')
        self.assertEqual(self.app.selected, set())

    def test_old_preview_cannot_replace_new_selection_or_results(self):
        first, second = list(self.app.rows)
        self.app.tree.focus(first)
        self.app.show_detail()
        old_token = self.app.detail_token
        self.app.tree.focus(second)
        self.app.show_detail()
        picture = Image.new('RGB', (320, 180), 'red')
        self.app.receive_preview((old_token, first, True, .5, picture, (400, 300), None))
        self.assertEqual(self.app.detail_title.get(), 'small.jpg')
        self.assertIsNone(self.app.detail_photo)
        self.app.groups = []
        self.app.render()
        self.app.receive_preview((old_token, first, True, .5, picture, (400, 300), None))
        self.assertIsNone(self.app.detail_photo)
        self.assertEqual(self.app.thumbnail_photos, {})

    def test_unreadable_preview_keeps_result_usable(self):
        root = Path(self.temp.name)
        (root / 'bad-a.png').write_bytes(b'invalid image')
        (root / 'bad-b.png').write_bytes(b'invalid image')
        self.app.groups, _ = scan(root, IMAGE_EXTENSIONS, Event())
        self.app.render()
        self.app.deiconify()
        self.app.update()
        self.app.load_visible_thumbnails()
        self.wait_for(lambda: len(self.app.thumbnail_photos) == 2)
        self.assertTrue(all(photo is None for photo in self.app.thumbnail_photos.values()))
        self.assertEqual(len(self.app.rows), 2)
        self.assertIsNone(self.app.detail_photo)
        self.assertEqual(str(self.app.detail_open.cget('state')), 'normal')

    def test_video_thumbnail_and_frame_switch(self):
        import cv2
        import numpy as np
        from PIL import ImageTk
        root = Path(self.temp.name)
        path = root / 'clip.avi'
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10, (160, 120))
        self.assertTrue(writer.isOpened())
        try:
            for index in range(30):
                color = (0, 0, 255) if index < 15 else (255, 0, 0)
                writer.write(np.full((120, 160, 3), color, dtype=np.uint8))
        finally:
            writer.release()
        (root / 'copy.avi').write_bytes(path.read_bytes())
        self.app.groups, _ = scan(root, VIDEO_EXTENSIONS, Event())
        self.app.render()
        self.app.deiconify()
        self.app.update()
        self.app.load_visible_thumbnails()
        self.wait_for(lambda: len(self.app.thumbnail_photos) == 2)
        self.assertTrue(all(photo is not None for photo in self.app.thumbnail_photos.values()))
        self.assertTrue(self.app.frame_controls.winfo_manager())
        self.app.show_detail(point=.1)
        self.wait_for(lambda: self.app.detail_photo is not None)
        early = ImageTk.getimage(self.app.detail_photo).getpixel((160, 90))
        self.app.show_detail(point=.9)
        self.wait_for(lambda: self.app.detail_photo is not None)
        late = ImageTk.getimage(self.app.detail_photo).getpixel((160, 90))
        self.assertGreater(early[0], 240)
        self.assertGreater(late[2], 240)
        self.assertNotEqual(early, late)
        self.assertIn('90%', self.app.detail_hint.get())
        self.assertEqual(self.app.selected, set())
        self.assertLessEqual(self.app.detail_open.winfo_y() + self.app.detail_open.winfo_height(),
                             self.app.detail_open.master.winfo_height())

    def test_preview_keep_and_cleanup_uses_per_file_hash(self):
        self.app.tree.focus(next(iter(self.app.rows)))
        self.app.compare()
        window = next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))
        window.withdraw()
        self.wait_for(lambda: len(window.preview_photos) == 2)
        self.assertTrue(all(photo.width() == 400 for photo in window.preview_photos))

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        buttons = [w for w in descendants(window) if w.winfo_class() == 'TButton' and str(w.cget('text')).startswith('이 파일만 보관')]
        self.assertEqual(str(buttons[0].cget('state')), 'normal')
        buttons[0].invoke()
        self.comparison_buttons(window, '선택 확정')[0].invoke()
        self.assertEqual({p.name for p in self.app.selected}, {'small.jpg'})
        with patch('app.messagebox.askyesno', return_value=True), patch('app.recycle_file') as recycle:
            self.app.start_cleanup()
            self.wait_for(lambda: not self.app.busy)
            self.assertEqual(recycle.call_count, 1)
            self.assertEqual(recycle.call_args.args[0].name, 'small.jpg')
        self.assertTrue((Path(self.temp.name) / 'small.jpg').exists())


if __name__ == '__main__':
    unittest.main()
