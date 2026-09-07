"""SameImage desktop application. Run with Python 3.10 or newer."""
import os
import queue
import threading
import tkinter as tk
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from duplicate_finder import (IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, Cancelled,
                              digest_file, expected_digest, recycle_file, scan, validate_selection)
from media_preview import PreviewLoader


def size_text(size):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:,.1f} {unit}'
        size /= 1024


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('SameImage · 중복 미디어 정리')
        self.geometry('1280x800')
        self.minsize(900, 640)
        self.configure(bg='#f4f6fa')
        self.events = queue.Queue()
        self.preview_loader = PreviewLoader(self.events)
        self.thumbnail_photos = OrderedDict()
        self.thumbnail_pending = set()
        self.thumbnail_after = None
        self.detail_photo = None
        self.detail_row = None
        self.detail_token = None
        self.cancel = threading.Event()
        self.busy = False
        self.cleaning = False
        self.groups = []
        self.rows = {}
        self.selected = set()
        self.errors = []
        self.reviewed = set()
        self.comparison_drafts = {}
        self.folder = tk.StringVar()
        self.images = tk.BooleanVar(value=True)
        self.videos = tk.BooleanVar(value=True)
        self.similar = tk.BooleanVar(value=True)
        self.sensitivity = tk.StringVar(value='보통')
        self.status = tk.StringVar(value='검색할 폴더를 선택해 주세요.')
        self.summary = tk.StringVar(value='중복 파일을 찾아 저장 공간을 정리하세요.')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('맑은 고딕', 10))
        style.configure('Treeview', rowheight=84)
        style.configure('TButton', padding=(12, 7))
        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(6, weight=1)
        ttk.Label(body, text='SameImage', font=('맑은 고딕', 22, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(body, text='같은 이미지와 영상, 하나만 남기세요.').grid(row=1, column=0, sticky='w', pady=(2, 8))
        folder_row = ttk.Frame(body)
        folder_row.grid(row=2, column=0, sticky='ew')
        self.path_entry = ttk.Entry(folder_row, textvariable=self.folder)
        self.path_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.browse = ttk.Button(folder_row, text='폴더 선택', command=self.choose_folder)
        self.browse.pack(side='left')
        options = ttk.Frame(body)
        options.grid(row=3, column=0, sticky='ew', pady=6)
        self.image_check = ttk.Checkbutton(options, text='이미지', variable=self.images)
        self.image_check.pack(side='left')
        self.video_check = ttk.Checkbutton(options, text='영상', variable=self.videos)
        self.video_check.pack(side='left', padx=15)
        self.similar_check = ttk.Checkbutton(options, text='유사 파일 포함', variable=self.similar)
        self.similar_check.pack(side='left', padx=8)
        self.level = ttk.Combobox(options, textvariable=self.sensitivity, values=('엄격', '보통', '넓게'), width=6, state='readonly')
        self.level.pack(side='left', padx=8)
        ttk.Label(options, text='하위 폴더 포함').pack(side='left')
        self.search = ttk.Button(options, text='중복 검색', command=self.start_scan)
        self.search.pack(side='right')
        self.stop = ttk.Button(options, text='중단', command=self.cancel.set, state='disabled')
        self.stop.pack(side='right', padx=8)
        summary_label = ttk.Label(body, textvariable=self.summary, font=('맑은 고딕', 11, 'bold'), wraplength=850)
        summary_label.grid(row=4, column=0, sticky='ew', pady=6)
        results = ttk.Panedwindow(body, orient='horizontal')
        results.grid(row=6, column=0, sticky='nsew')
        table = ttk.Frame(results)
        results.add(table, weight=1)
        self.tree = ttk.Treeview(table, columns=('select', 'size', 'resolution', 'path'), selectmode='browse', height=3)
        self.tree.heading('#0', text='미리보기 / 파일명')
        self.tree.heading('select', text='정리')
        self.tree.column('select', width=48, minwidth=48, stretch=False, anchor='center')
        self.tree.heading('size', text='파일 크기')
        self.tree.heading('path', text='전체 경로')
        self.tree.heading('resolution', text='해상도 / 길이')
        self.tree.column('#0', width=310, minwidth=260)
        self.tree.column('size', width=100, minwidth=90, stretch=False)
        self.tree.column('path', width=250, minwidth=150)
        self.tree.column('resolution', width=150, minwidth=140, stretch=False)
        yscroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        xscroll = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        def on_scroll(first, last):
            yscroll.set(first, last)
            self.schedule_thumbnails()
        self.tree.configure(yscrollcommand=on_scroll, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        yscroll.grid(row=0, column=1, sticky='ns')
        xscroll.grid(row=1, column=0, sticky='ew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind('<Button-1>', self.click_row)
        self.tree.bind('<space>', self.toggle_focused)
        self.tree.bind('<<TreeviewSelect>>', self.show_detail)
        self.tree.bind('<<TreeviewOpen>>', lambda e: self.schedule_thumbnails())
        self.tree.bind('<Configure>', lambda e: self.schedule_thumbnails())
        detail_viewport = ttk.Frame(results, width=356)
        results.add(detail_viewport, weight=0)
        detail_viewport.columnconfigure(0, weight=1)
        detail_viewport.rowconfigure(0, weight=1)
        self.detail_scroll_canvas = tk.Canvas(detail_viewport, width=336, height=180, highlightthickness=0)
        detail_scrollbar = ttk.Scrollbar(detail_viewport, orient='vertical', command=self.detail_scroll_canvas.yview)
        self.detail_scroll_canvas.configure(yscrollcommand=detail_scrollbar.set)
        self.detail_scroll_canvas.grid(row=0, column=0, sticky='nsew')
        detail_scrollbar.grid(row=0, column=1, sticky='ns')
        detail = ttk.Frame(self.detail_scroll_canvas, padding=(8, 0, 0, 0))
        self.detail_scroll_canvas.create_window((0, 0), window=detail, anchor='nw')
        detail.bind('<Configure>', lambda e: self.detail_scroll_canvas.configure(scrollregion=self.detail_scroll_canvas.bbox('all')))
        ttk.Label(detail, text='선택 파일 미리보기', font=('맑은 고딕', 12, 'bold')).pack(anchor='w', pady=(0, 8))
        self.detail_canvas = tk.Canvas(detail, width=320, height=180, bg='#e5e7eb', highlightthickness=0)
        self.detail_canvas.pack(anchor='w')
        self.detail_title = tk.StringVar(value='파일을 선택하면 여기에 표시됩니다.')
        self.detail_meta = tk.StringVar()
        self.detail_path = tk.StringVar()
        ttk.Label(detail, textvariable=self.detail_title, wraplength=320, font=('맑은 고딕', 10, 'bold')).pack(anchor='w', pady=(8, 2))
        ttk.Label(detail, textvariable=self.detail_meta, wraplength=320).pack(anchor='w')
        self.frame_controls = ttk.Frame(detail)
        for point in (.1, .3, .5, .7, .9):
            ttk.Button(self.frame_controls, text=f'{point:.0%}', width=3,
                       command=lambda p=point: self.show_detail(point=p)).pack(side='left', padx=(0, 3))
        self.detail_hint = tk.StringVar()
        ttk.Label(detail, textvariable=self.detail_hint, wraplength=320, foreground='#626977').pack(anchor='w', pady=4)
        ttk.Label(detail, textvariable=self.detail_path, wraplength=320, foreground='#626977').pack(anchor='w', pady=4)
        self.detail_open = ttk.Button(detail, text='원본 열기', command=self.open_file, state='disabled')
        self.detail_open.pack(anchor='w')
        self.clear_detail()
        hint_label = ttk.Label(body, text='썸네일 클릭: 크게 보기 · 정리 열 / Space: 선택 · □ 보관 / ☑ 휴지통 이동', wraplength=850)
        hint_label.grid(row=7, column=0, sticky='ew', pady=4)
        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky='ew', pady=(0, 8))
        actions.columnconfigure(0, weight=1)
        secondary_actions = ttk.Frame(actions)
        secondary_actions.grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.action_buttons = []
        for label, command in [('완전 중복만 일괄 선택', self.select_duplicates), ('나란히 비교', self.compare),
                               ('선택 해제', self.clear_selection), ('파일 열기', self.open_file),
                               ('폴더 열기', self.open_folder), ('오류 내역', self.show_errors)]:
            button = ttk.Button(secondary_actions, text=label, command=command)
            self.action_buttons.append(button)
        style.configure('Clean.TButton', font=('맑은 고딕', 11, 'bold'), foreground='#ffffff', background='#245b94')
        style.map('Clean.TButton', background=[('disabled', '#999999'), ('active', '#184575')])
        self.clean = ttk.Button(actions, text='선택 파일 정리 → 휴지통', style='Clean.TButton', command=self.start_cleanup)
        self.clean.grid(row=0, column=1, sticky='ne')

        def wrap_actions(event):
            row, column, used = 0, 0, 0
            height = max(button.winfo_reqheight() for button in self.action_buttons) + 4
            for button in self.action_buttons:
                width = button.winfo_reqwidth() + 6
                if column and used + width > event.width:
                    row, column, used = row + 1, 0, 0
                button.place(x=used, y=row * height + 2)
                column += 1
                used += width
            secondary_actions.configure(height=(row + 1) * height)
        # Let the available width drive wrapping rather than the buttons' total width.
        secondary_actions.grid_propagate(False)
        secondary_actions.bind('<Configure>', wrap_actions)
        footer = ttk.Frame(body)
        footer.grid(row=8, column=0, sticky='ew')
        self.progress = ttk.Progressbar(footer, mode='indeterminate')
        self.progress.pack(fill='x', pady=(2, 4))
        status_label = ttk.Label(footer, textvariable=self.status, wraplength=850)
        status_label.pack(anchor='w')
        note_label = ttk.Label(footer, text='유사 후보는 비교 후 선택하세요. 실제 공간 확보는 휴지통을 비운 뒤 반영됩니다.',
                               wraplength=850, foreground='#626977')
        note_label.pack(anchor='w', pady=(2, 0))
        def wrap_labels(event):
            for label in (summary_label, hint_label, status_label, note_label):
                label.configure(wraplength=max(300, event.width - 24))
        body.bind('<Configure>', wrap_labels)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_after = self.after(100, self.poll)

    def choose_folder(self):
        folder = filedialog.askdirectory(title='중복 파일을 검색할 폴더')
        if folder:
            self.folder.set(folder)

    def set_busy(self, busy, cleaning=False):
        self.busy, self.cleaning = busy, cleaning
        for widget in [self.browse, self.path_entry, self.image_check, self.video_check,
                       self.similar_check, self.search, self.clean, *self.action_buttons]:
            widget.configure(state='disabled' if busy else 'normal')
        self.level.configure(state='disabled' if busy else 'readonly')
        self.stop.configure(state='normal' if busy and not cleaning else 'disabled')
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def start_scan(self):
        root = Path(self.folder.get().strip())
        if not self.folder.get().strip() or not root.is_dir():
            messagebox.showerror('폴더 확인', '존재하는 폴더를 선택해 주세요.')
            return
        extensions = (IMAGE_EXTENSIONS if self.images.get() else frozenset()) | (VIDEO_EXTENSIONS if self.videos.get() else frozenset())
        if not extensions:
            messagebox.showinfo('검색 유형', '이미지 또는 영상을 선택해 주세요.')
            return
        use_similar = self.similar.get()
        threshold = {'엄격': 5, '보통': 9, '넓게': 13}[self.sensitivity.get()]
        if use_similar:
            try:
                from similarity import scan_similar
            except ImportError:
                messagebox.showerror('패키지 설치 필요', 'setup.bat을 실행해 유사 검색 패키지를 설치해 주세요.\n유사 파일 포함을 해제하면 완전 중복 검색은 가능합니다.')
                return
        self.groups, self.errors = [], []
        self.reviewed.clear()
        self.selected.clear()
        self.render()
        self.cancel.clear()
        self.set_busy(True)
        self.status.set('폴더를 읽는 중…')

        def work():
            try:
                report = lambda msg: self.events.put(('status', msg))
                result = (scan_similar(root, extensions, self.cancel, report, threshold)
                          if use_similar else scan(root, extensions, self.cancel, report))
                self.events.put(('scan', result))
            except Cancelled:
                self.events.put(('cancel', None))
            except Exception as error:
                self.events.put(('error', str(error)))
        threading.Thread(target=work, daemon=True).start()

    def render(self):
        self.comparison_drafts.clear()
        self.preview_loader.reset()
        self.thumbnail_pending.clear()
        self.thumbnail_photos.clear()
        self.clear_detail()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        for index, group in enumerate(self.groups, 1):
            label = '완전 중복' if group.kind == 'exact' else f'유사 후보 · 점수 {group.score:.1f}'
            parent = self.tree.insert('', 'end', text=f'{index}. {label} · {len(group.files)}개', open=True)
            for i, file in enumerate(group.files):
                info = group.details[i] if group.details else None
                dimensions = f'{info.width}×{info.height}' if info else '—'
                if info and info.kind == 'video':
                    dimensions += f' / {info.duration:.1f}초'
                row = self.tree.insert(parent, 'end', text=file.path.name, values=('□', size_text(file.size), dimensions, str(file.path)))
                self.rows[row] = (group, file)
        self.refresh_selection()
        self.schedule_thumbnails()
        if self.rows:
            first = next(iter(self.rows))
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.show_detail()

    def refresh_selection(self):
        total = 0
        for row, (_, file) in self.rows.items():
            checked = file.path in self.selected
            self.tree.set(row, 'select', '☑' if checked else '□')
            if checked:
                total += file.size
        reclaim = sum(sum(f.size for f in g.files[1:]) for g in self.groups)
        exact = sum(g.kind == 'exact' for g in self.groups)
        self.summary.set(f'완전 중복 {exact} · 유사 후보 {len(self.groups) - exact}그룹 · 추천 보관 시 정리 {size_text(reclaim)}   |   선택 {len(self.selected):,}개 · {size_text(total)}')

    def click_row(self, event):
        if self.tree.identify_column(event.x) == '#1' and self.tree.identify_region(event.x, event.y) == 'cell':
            self.toggle(self.tree.identify_row(event.y))

    def schedule_thumbnails(self):
        if self.thumbnail_after is None:
            self.thumbnail_after = self.after(60, self.load_visible_thumbnails)

    def load_visible_thumbnails(self):
        self.thumbnail_after = None
        # Load only visible rows, so huge result lists do not decode every file.
        visible = {self.tree.identify_row(y) for y in range(0, self.tree.winfo_height(), 35)}
        for row in visible:
            if row not in self.rows:
                continue
            if row in self.thumbnail_photos:
                self.thumbnail_photos.move_to_end(row)
            elif row not in self.thumbnail_pending:
                self.thumbnail_pending.add(row)
                self.preview_loader.submit(row, self.rows[row][1].path)

    def clear_detail(self):
        self.preview_loader.detail_request += 1
        self.detail_token = None
        self.detail_row = None
        self.detail_photo = None
        self.detail_canvas.delete('all')
        self.detail_canvas.create_text(160, 90, text='이미지 · 영상 미리보기', fill='#626977')
        self.detail_title.set('파일을 선택하면 여기에 표시됩니다.')
        self.detail_meta.set('')
        self.detail_path.set('')
        self.detail_hint.set('')
        self.frame_controls.pack_forget()
        self.detail_open.configure(state='disabled')

    def show_detail(self, _event=None, point=.5):
        row = self.tree.focus()
        if row not in self.rows:
            self.clear_detail()
            return
        if self.detail_row == row and point == .5 and _event is not None:
            return
        self.detail_row = row
        group, file = self.rows[row]
        info = group.details[group.files.index(file)] if group.details else None
        video = file.path.suffix.lower() in VIDEO_EXTENSIONS
        self.detail_photo = None
        self.detail_canvas.delete('all')
        self.detail_canvas.create_text(160, 90, text='미리보기 읽는 중…', fill='#626977')
        self.detail_title.set(file.path.name)
        meta = ('영상' if video else '이미지') + f' · {size_text(file.size)}'
        if info:
            meta += f' · {info.width}×{info.height}'
            if info.duration:
                meta += f' · {info.duration:.1f}초'
        self.detail_meta.set(meta)
        self.detail_path.set(str(file.path))
        self.detail_open.configure(state='normal')
        if video:
            self.frame_controls.pack(before=self.detail_open, anchor='w', pady=4)
            self.detail_hint.set(f'{point:.0%} 지점의 정지 프레임 · 재생은 원본 열기')
        else:
            self.frame_controls.pack_forget()
            self.detail_hint.set('원본 비율로 표시 · 원본 열기로 크게 확인')
        self.detail_token = self.preview_loader.submit(row, file.path, detail=True, point=point)

    def receive_preview(self, payload):
        token, row, detail, point, picture, dimensions, error = payload
        if not self.preview_loader.current(token, detail) or row not in self.rows:
            return
        if detail:
            if row != self.detail_row or token != self.detail_token:
                return
            self.detail_canvas.delete('all')
            if error:
                self.detail_canvas.create_text(160, 90, text=error, width=292, fill='#626977')
                return
            from PIL import ImageTk
            self.detail_photo = ImageTk.PhotoImage(picture, master=self)
            self.detail_canvas.create_image(160, 90, image=self.detail_photo)
            group, file = self.rows[row]
            if not group.details:
                self.detail_meta.set(f'{"영상" if file.path.suffix.lower() in VIDEO_EXTENSIONS else "이미지"} · {size_text(file.size)} · {dimensions[0]}×{dimensions[1]}')
        else:
            self.thumbnail_pending.discard(row)
            if error:
                self.thumbnail_photos[row] = None
                self.tree.item(row, text=f'[미리보기 없음] {self.rows[row][1].path.name}')
            else:
                from PIL import ImageTk
                photo = ImageTk.PhotoImage(picture, master=self)
                self.thumbnail_photos[row] = photo
                self.tree.item(row, image=photo)
            while len(self.thumbnail_photos) > 128:
                old_row, _ = self.thumbnail_photos.popitem(last=False)
                if self.tree.exists(old_row):
                    self.tree.item(old_row, image='')

    def toggle_focused(self, _event):
        self.toggle(self.tree.focus())
        return 'break'

    def toggle(self, row):
        if self.busy or row not in self.rows:
            return
        group, file = self.rows[row]
        if group.kind == 'similar' and id(group) not in self.reviewed:
            self.status.set('유사 후보는 먼저 [나란히 비교]에서 보관할 파일을 선택해 주세요.')
            return
        if file.path in self.selected:
            self.selected.remove(file.path)
        elif sum(f.path in self.selected for f in group.files) >= len(group.files) - 1:
            self.status.set('각 그룹에서 최소 한 파일은 보관해야 합니다.')
        else:
            self.selected.add(file.path)
        self.refresh_selection()

    def select_duplicates(self):
        self.selected = {file.path for group in self.groups if group.kind == 'exact' for file in group.files[1:]}
        self.refresh_selection()
        self.status.set('완전 중복만 선택했습니다. 유사 후보는 [나란히 비교]에서 보관 파일을 직접 선택하세요.')

    def compare(self):
        row = self.tree.focus()
        if row not in self.rows:
            children = self.tree.get_children(row) if row else ()
            row = children[0] if children else ''
        if row not in self.rows:
            messagebox.showinfo('비교할 그룹', '결과에서 비교할 파일 또는 그룹을 선택해 주세요.')
            return
        try:
            from PIL import Image, ImageTk, ImageOps
            from similarity import preview, video_frames
        except ImportError:
            messagebox.showerror('패키지 설치 필요', '미리보기를 사용하려면 setup.bat을 실행해 주세요.')
            return
        group = self.rows[row][0]
        group_index = self.groups.index(group)
        window = tk.Toplevel(self)
        window.title(f'나란히 비교 · {group_index + 1}/{len(self.groups)} 그룹')
        window.geometry('1080x680')
        window.transient(self)
        window.grab_set()
        navigation = ttk.Frame(window, padding=(12, 8))
        navigation.pack(fill='x')
        ttk.Label(navigation, text=f'그룹 {group_index + 1} / {len(self.groups)} · 여러 파일을 보관할 수 있습니다.').pack(side='left')
        ttk.Label(window, text='고해상도 순으로 표시합니다. 해상도만으로 원본·화질을 보장하지는 않습니다.', padding=12).pack(anchor='w')
        ttk.Label(window, text='영상은 10·30·50·70·90% 지점의 샘플입니다. 음성·자막·샘플 사이 장면은 원본을 열어 확인하세요.', padding=(12, 0)).pack(anchor='w')
        viewport = ttk.Frame(window)
        viewport.pack(fill='both', expand=True, padx=12, pady=12)
        canvas = tk.Canvas(viewport, highlightthickness=0)
        scroll = ttk.Scrollbar(viewport, orient='horizontal', command=canvas.xview)
        vertical = ttk.Scrollbar(viewport, orient='vertical', command=canvas.yview)
        canvas.configure(xscrollcommand=scroll.set, yscrollcommand=vertical.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        scroll.grid(row=1, column=0, sticky='ew')
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        cards = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=cards, anchor='nw')
        cards.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        labels, buttons, photos = [], [], []
        window.preview_photos = photos
        pending = queue.Queue()
        preview_cancel = threading.Event()
        preview_after = None
        draft = set(self.comparison_drafts.get(id(group), {f.path for f in group.files if f.path in self.selected}))
        choices, choice_labels = {}, {}
        selection_summary = tk.StringVar(master=window)
        notice = tk.StringVar(master=window, value='미리보기를 읽는 중입니다. 체크하지 않은 파일은 보관합니다.')

        def refresh_choices():
            for path, variable in choices.items():
                variable.set(path in draft)
                choice_labels[path].set('휴지통 이동 예정' if path in draft else '보관')
            selection_summary.set(f'보관 {len(group.files) - len(draft)}개 · 휴지통 {len(draft)}개')

        def toggle_choice(path):
            if choices[path].get():
                if len(draft) == len(group.files) - 1:
                    notice.set('최소 한 파일은 보관해야 합니다. 다른 파일의 체크를 먼저 해제하세요.')
                else:
                    draft.add(path)
            else:
                draft.discard(path)
            refresh_choices()

        def keep(file):
            draft.clear()
            draft.update(f.path for f in group.files if f != file)
            refresh_choices()

        def confirm():
            self.reviewed.add(id(group))
            self.selected.difference_update(f.path for f in group.files)
            self.selected.update(draft)
            self.refresh_selection()
            self.status.set(f'선택 확정 · 보관 {len(group.files) - len(draft)}개 · 정리 {len(draft)}개')
            navigate(group_index + 1)

        def navigate(index):
            geometry = window.geometry()
            close_preview()
            if 0 <= index < len(self.groups):
                target = self.groups[index]
                row = next(row for row, (candidate, _) in self.rows.items() if candidate is target)
                self.tree.focus(row)
                self.tree.selection_set(row)
                self.tree.see(row)
                self.compare()
                self.comparison_window.geometry(geometry)

        def close_preview():
            committed = {f.path for f in group.files if f.path in self.selected}
            if draft != committed:
                self.comparison_drafts[id(group)] = set(draft)
            else:
                self.comparison_drafts.pop(id(group), None)
            preview_cancel.set()
            if preview_after is not None:
                window.after_cancel(preview_after)
            window.destroy()

        window.protocol('WM_DELETE_WINDOW', close_preview)
        self.comparison_window = window
        ttk.Button(navigation, text='닫기 · 선택 유지', command=close_preview).pack(side='right')
        ttk.Button(navigation, text='다음 그룹 →', command=lambda: navigate(group_index + 1),
                   state='normal' if group_index + 1 < len(self.groups) else 'disabled').pack(side='right', padx=4)
        ttk.Button(navigation, text='← 이전 그룹', command=lambda: navigate(group_index - 1),
                   state='normal' if group_index else 'disabled').pack(side='right', padx=4)
        decision = ttk.Frame(window, padding=(12, 6))
        decision.pack(before=viewport, fill='x')
        ttk.Label(decision, textvariable=selection_summary).pack(side='left')
        confirm_button = ttk.Button(decision, text='선택 확정 후 다음 그룹', command=confirm, state='disabled')
        confirm_button.pack(side='right')
        buttons.append(confirm_button)
        for i, file in enumerate(group.files):
            card = ttk.Frame(cards, padding=12, relief='ridge')
            card.grid(row=0, column=i, padx=6, sticky='ns')
            ttk.Label(card, text=file.path.name, wraplength=400, font=('맑은 고딕', 11, 'bold')).pack(anchor='w')
            choice_labels[file.path] = tk.StringVar(master=window)
            ttk.Label(card, textvariable=choice_labels[file.path], foreground='#245b94').pack(anchor='w', pady=3)
            info = group.details[i] if group.details else None
            meta = f'{info.width}×{info.height} · ' if info else ''
            if info and info.duration:
                meta += f'{info.duration:.2f}초 · '
            ttk.Label(card, text=meta + size_text(file.size)).pack(anchor='w', pady=5)
            label = ttk.Label(card, text='미리보기 읽는 중…', width=56, anchor='center')
            label.pack(pady=8)
            labels.append(label)
            ttk.Label(card, text=str(file.path), wraplength=400).pack(anchor='w', pady=6)
            ttk.Button(card, text='원본 열기', command=lambda p=file.path: self.open_external(p)).pack(fill='x')
            choices[file.path] = tk.BooleanVar(master=window)
            checkbox = ttk.Checkbutton(card, text='휴지통으로 보낼 파일', variable=choices[file.path],
                                       command=lambda p=file.path: toggle_choice(p), state='disabled')
            checkbox.pack(anchor='w', pady=6)
            buttons.append(checkbox)
            button = ttk.Button(card, text='이 파일만 보관', command=lambda f=file: keep(f), state='disabled')
            button.pack(fill='x', pady=8)
            buttons.append(button)
        refresh_choices()
        ttk.Label(window, textvariable=notice, padding=12).pack(anchor='w')

        def load():
            for i, file in enumerate(group.files):
                if preview_cancel.is_set():
                    return
                try:
                    if file.path.suffix.lower() in VIDEO_EXTENSIONS:
                        _, frames = video_frames(file.path, preview_cancel)
                        image = Image.new('RGB', (400, 300), '#e5e7eb')
                        for j, frame in enumerate(frames):
                            thumb = ImageOps.contain(frame, (196, 96))
                            image.paste(thumb, ((j % 2) * 200, (j // 2) * 100))
                    else:
                        image = ImageOps.pad(preview(file.path), (400, 300), color='#e5e7eb')
                    pending.put((i, image, None))
                except Exception as error:
                    pending.put((i, None, str(error)))
        loaded, failed = set(), set()

        def poll_preview():
            nonlocal preview_after
            preview_after = None
            if not window.winfo_exists():
                return
            while not pending.empty():
                i, picture, error = pending.get_nowait()
                loaded.add(i)
                if error:
                    failed.add(i)
                    labels[i].configure(text=f'미리보기 실패\n{error}', wraplength=390)
                else:
                    photo = ImageTk.PhotoImage(picture)
                    photos.append(photo)
                    labels[i].configure(image=photo, text='', width=0)
            if len(loaded) == len(group.files):
                if not failed:
                    for button in buttons:
                        button.configure(state='normal')
                    notice.set('없앨 파일만 체크하고 선택을 확정하세요. 미확정 변경은 다시 열면 이어집니다. 실제 이동은 메인 화면에서 진행합니다.')
                else:
                    notice.set('읽지 못한 미리보기가 있어 이 화면에서는 정리 선택을 할 수 없습니다.')
                return
            preview_after = window.after(100, poll_preview)
        threading.Thread(target=load, daemon=True).start()
        preview_after = window.after(100, poll_preview)

    def open_external(self, path):
        try:
            os.startfile(str(path))
        except OSError as error:
            messagebox.showerror('열기 실패', str(error))

    def clear_selection(self):
        self.selected.clear()
        self.refresh_selection()

    def open_file(self):
        self.open_path(False)

    def open_folder(self):
        self.open_path(True)

    def open_path(self, folder):
        row = self.tree.focus()
        if row in self.rows:
            path = self.rows[row][1].path
            try:
                os.startfile(str(path.parent if folder else path))
            except OSError as error:
                messagebox.showerror('열기 실패', str(error))

    def show_errors(self):
        window = tk.Toplevel(self)
        window.title('검색 / 정리 오류 내역')
        text = tk.Text(window, width=100, height=24, wrap='word')
        text.pack(fill='both', expand=True)
        text.insert('1.0', '\n\n'.join(self.errors) or '오류가 없습니다.')
        text.configure(state='disabled')

    def start_cleanup(self):
        if not self.selected:
            messagebox.showinfo('파일 선택', '정리할 파일을 먼저 선택해 주세요.')
            return
        if any(g.kind == 'similar' and id(g) not in self.reviewed and any(f.path in self.selected for f in g.files) for g in self.groups):
            messagebox.showinfo('유사 후보 확인', '유사 후보는 나란히 비교에서 먼저 확인해 주세요.')
            return
        similar_count = sum(g.kind == 'similar' and any(f.path in self.selected for f in g.files) for g in self.groups)
        if not messagebox.askyesno('선택 파일 정리 확인',
                f'선택한 {len(self.selected):,}개 파일을 휴지통으로 이동할까요?\n\n'
                f'유사 후보 {similar_count}그룹 포함 — 내용·화질·음성이 다를 수 있습니다.\n'
                '각 그룹에서 선택하지 않은 파일은 보관합니다.\n'
                '휴지통을 지원하지 않는 위치에서는 Windows가 영구 삭제를 물을 수 있습니다. 이 경우 취소해 주세요.'):
            return
        selected = set(self.selected)
        self.cancel.clear()
        self.set_busy(True, cleaning=True)
        self.status.set('보관 파일과 선택 파일의 내용을 다시 확인하는 중…')

        def work():
            moved, errors = [], []
            try:
                plan = [(group, validate_selection(group, selected, self.cancel)) for group in self.groups]
                for group, targets in plan:
                    keeper = next(file for file in group.files if file.path not in selected)
                    for file in targets:
                        # Check retained copy again immediately before each operation.
                        if digest_file(keeper, self.cancel) != expected_digest(group, keeper) or digest_file(file, self.cancel) != expected_digest(group, file):
                            raise ValueError('파일 내용이 변경되었습니다. 다시 검색해 주세요.')
                        self.events.put(('status', f'휴지통 이동 중 · {file.path.name}'))
                        recycle_file(file.path)
                        moved.append(file.path)
            except Exception as error:
                errors.append(str(error))
            self.events.put(('clean', (moved, errors)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        for _ in range(150):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'status':
                self.status.set(payload)
            elif kind == 'media_preview':
                self.receive_preview(payload)
            elif kind == 'scan':
                self.groups, self.errors = payload
                self.set_busy(False)
                self.render()
                self.status.set(f'검색 완료 · 중복 {len(self.groups)}그룹 · 읽기 오류 {len(self.errors)}개')
            elif kind == 'cancel':
                self.set_busy(False)
                self.status.set('검색을 중단했습니다. 파일은 변경되지 않았습니다.')
            elif kind == 'error':
                self.set_busy(False)
                self.status.set('작업 실패')
                messagebox.showerror('작업 실패', payload)
            elif kind == 'clean':
                moved, errors = payload
                self.errors.extend(errors)
                self.apply_cleanup_result(moved)
                self.set_busy(False)
                self.status.set(f'{len(moved)}개 파일 휴지통 이동 · 남은 {len(self.groups)}그룹은 계속 확인할 수 있습니다.'
                                + (' 이동하지 못한 파일의 선택을 유지했습니다.' if errors else ''))
                if errors:
                    messagebox.showerror('정리 중단', '\n'.join(errors))
        self.poll_after = self.after(100, self.poll)

    def apply_cleanup_result(self, moved):
        if not moved:
            return
        removed = set(moved)
        focused = self.rows.get(self.tree.focus())
        focus_path = focused[1].path if focused else None
        scroll = self.tree.yview()[0]
        remaining, reviewed = [], set()
        for group in self.groups:
            indices = [i for i, file in enumerate(group.files) if file.path not in removed]
            if len(indices) < 2:
                continue  # One retained copy is no longer a duplicate group.
            updated = group if len(indices) == len(group.files) else replace(
                group, files=tuple(group.files[i] for i in indices),
                details=tuple(group.details[i] for i in indices) if group.details else (),
                digests=tuple(group.digests[i] for i in indices) if group.digests else ())
            remaining.append(updated)
            if id(group) in self.reviewed:
                reviewed.add(id(updated))
        self.groups = remaining
        self.reviewed = reviewed
        self.selected.intersection_update(file.path for group in remaining for file in group.files)
        self.render()
        self.tree.yview_moveto(scroll)
        for row, (_, file) in self.rows.items():
            if file.path == focus_path:
                self.tree.focus(row)
                self.tree.selection_set(row)
                self.show_detail()
                break

    def destroy(self):
        self.cancel.set()
        self.preview_loader.close()
        self.thumbnail_photos.clear()
        self.detail_photo = None
        if self.thumbnail_after is not None:
            self.after_cancel(self.thumbnail_after)
            self.thumbnail_after = None
        if getattr(self, 'poll_after', None) is not None:
            self.after_cancel(self.poll_after)
            self.poll_after = None
        super().destroy()

    def close(self):
        if self.cleaning:
            messagebox.showinfo('정리 진행 중', '파일 정리가 끝난 뒤 창을 닫아 주세요.')
            return
        self.cancel.set()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
