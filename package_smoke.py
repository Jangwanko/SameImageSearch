"""Check the actual frozen runtime using temporary media, without deleting files."""
import json
import tempfile
import time
import traceback
from pathlib import Path
from threading import Event


def run(report_path):
    result = {'ok': False, 'checks': []}
    app = None
    try:
        import sys
        import cv2
        import numpy as np
        from PIL import Image, ImageDraw
        from app import App
        from similarity import scan_similar, preview
        from duplicate_finder import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, scan
        result['frozen'] = bool(getattr(sys, 'frozen', False))
        with tempfile.TemporaryDirectory(prefix='SameImage-check-') as directory:
            root = Path(directory)
            picture = Image.new('RGB', (640, 480), 'lightblue')
            draw = ImageDraw.Draw(picture)
            draw.ellipse((25, 50, 260, 340), fill='red')
            draw.rectangle((310, 20, 520, 420), fill='green')
            picture.save(root / 'original.png')
            picture.resize((320, 240)).save(root / 'small.jpg', quality=90)
            picture.save(root / 'sample.heic', format='HEIF')
            assert preview(root / 'sample.heic').size == (640, 480)
            result['checks'].append('HEIF encoder/decoder')
            (root / 'copy.png').write_bytes((root / 'original.png').read_bytes())
            exact, errors = scan(root, IMAGE_EXTENSIONS, Event())
            assert not errors and len(exact) == 1
            result['checks'].append('SHA-256 duplicate detection')
            groups, errors = scan_similar(root, IMAGE_EXTENSIONS, Event(), threshold=5)
            assert not errors and any(g.kind == 'similar' for g in groups)
            result['checks'].append('resized image similarity')
            for name, codec, size in [('movie.avi', 'MJPG', (640, 480)), ('movie.mp4', 'mp4v', (320, 240))]:
                writer = cv2.VideoWriter(str(root / name), cv2.VideoWriter_fourcc(*codec), 10, size)
                assert writer.isOpened(), codec
                try:
                    for _ in range(20):
                        writer.write(cv2.cvtColor(np.asarray(picture.resize(size)), cv2.COLOR_RGB2BGR))
                finally:
                    writer.release()
            video_groups, errors = scan_similar(root, VIDEO_EXTENSIONS, Event())
            assert not errors and len(video_groups) == 1
            assert preview(root / 'movie.mp4').size == (320, 240)
            result['checks'].append('OpenCV AVI/MP4 codecs and video similarity')
            app = App()
            app.withdraw()
            app.groups = groups
            app.render()
            deadline = time.monotonic() + 15
            while app.detail_photo is None and time.monotonic() < deadline:
                app.update()
                time.sleep(.02)
            assert app.detail_photo is not None, 'Tk preview was not loaded'
            result['checks'].append('Tkinter GUI and background image preview')
            app.destroy()
            app.preview_loader.worker.join(timeout=3)
            app = None
        result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
    finally:
        if app is not None:
            app.destroy()
        Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1
