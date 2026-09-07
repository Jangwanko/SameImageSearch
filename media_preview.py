"""Bounded, background preview loading; no Tk calls from worker threads."""
import itertools
import queue
import threading

from duplicate_finder import VIDEO_EXTENSIONS


class PreviewLoader:
    def __init__(self, events):
        self.events = events
        self.jobs = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.generation = 0
        self.detail_request = 0
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def reset(self):
        self.generation += 1
        self.detail_request += 1
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break

    def submit(self, row, path, detail=False, point=.5):
        if detail:
            self.detail_request += 1
        token = (self.generation, self.detail_request if detail else 0)
        self.jobs.put((0 if detail else 1, next(self.sequence), token, row, path, detail, point))
        return token

    def current(self, token, detail):
        return not self.closed.is_set() and token[0] == self.generation and (
            not detail or token[1] == self.detail_request)

    def close(self):
        self.closed.set()
        self.reset()
        self.jobs.put((-1, next(self.sequence), None, None, None, None, None))

    def _run(self):
        while not self.closed.is_set():
            _, _, token, row, path, detail, point = self.jobs.get()
            if token is None:
                return
            if not self.current(token, detail):
                continue
            picture, error, dimensions = None, None, None
            try:
                from PIL import Image, ImageDraw, ImageOps
                from similarity import preview
                source = preview(path, point)
                dimensions = source.size
                picture = ImageOps.pad(source, (320, 180) if detail else (112, 70), color='#e5e7eb')
                if not detail and path.suffix.lower() in VIDEO_EXTENSIONS:
                    draw = ImageDraw.Draw(picture)
                    draw.rounded_rectangle((5, 45, 32, 66), radius=4, fill='#172033')
                    draw.polygon(((15, 49), (15, 62), (25, 55)), fill='white')
            except ImportError:
                error = '미리보기 패키지가 없습니다. setup.bat을 실행해 주세요.'
            except Exception as exc:
                error = f'미리보기를 읽을 수 없습니다.\n{exc}'
            if self.current(token, detail):
                self.events.put(('media_preview', (token, row, detail, point, picture, dimensions, error)))
