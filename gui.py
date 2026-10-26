"""Графический интерфейс Video Conspector (tkinter + тема Sun Valley)."""

import ctypes
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional


class _NullStream:
    """Заглушка для stdout/stderr в exe без консоли."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass


for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, _NullStream())

import config  # noqa: E402  (после подмены потоков)
import lmstudio  # noqa: E402
import video_conspector as vc  # noqa: E402

try:
    import sv_ttk
except ImportError:  # без темы интерфейс остаётся рабочим, но проще
    sv_ttk = None

SETTINGS_FILE = config.BASE_DIR / "gui_settings.json"
WHISPER_MODELS = ("tiny", "base", "small", "medium")
LANGUAGES = {"Определять автоматически": "", "Русский": "ru", "English": "en",
             "Українська": "uk", "Deutsch": "de", "Español": "es"}
AUTO_LABEL = "Авто — использовать модель, загруженную сейчас"
URL_RE = re.compile(r"^https?://\S+$", re.I)
# Доли общего прогресса по этапам: скачивание, речь, кадры, код, конспект, запись
STAGE_WEIGHTS = (0.08, 0.32, 0.05, 0.15, 0.35, 0.05)
LLM_POLL_MS = 8000
COLORS = {"ok": "#2e9e5b", "warn": "#d98a00", "bad": "#d64545",
          "idle": "#8a8f98", "accent": "#3b8ef3"}
ITEM_COLORS = {"queued": "#8a8f98", "running": "#3b8ef3", "ok": "#2e9e5b",
               "raw": "#d98a00", "error": "#d64545", "cancelled": "#8a8f98"}
ITEM_TEXT = {"queued": "В очереди", "running": "Обработка", "ok": "Готово",
             "raw": "Только сырой текст", "error": "Ошибка",
             "cancelled": "Отменено"}


def enable_dpi_awareness() -> None:
    """Чёткий текст на экранах с масштабированием Windows."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def system_prefers_dark() -> bool:
    """Тёмная ли тема приложений в Windows."""
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return False


def format_elapsed(seconds: float) -> str:
    """Секунды в строку М:СС или Ч:ММ:СС."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    return (f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours
            else f"{rest // 60}:{rest % 60:02d}")


@dataclass
class Item:
    """Элемент очереди."""

    url: str
    title: str = ""
    state: str = "queued"
    path: Optional[Path] = None


class _QueueHandler(logging.Handler):
    """Передаёт сообщения логов в очередь окна."""

    def __init__(self, target: queue.Queue):
        super().__init__(logging.INFO)
        self.target = target
        self.last_error = ""

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        if record.levelno >= logging.ERROR:
            self.last_error = record.getMessage()
        if "HTTP Request:" not in message:  # служебный шум библиотек
            self.target.put(("log", record.levelno, message))


class SettingsDialog(tk.Toplevel):
    """Окно настроек (модальное)."""

    def __init__(self, app: "App", focus_token: bool = False):
        super().__init__(app)
        self.app = app
        self.title("Настройки")
        self.transient(app)
        self.resizable(False, False)
        s = app.settings
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        def row(index: int, text: str) -> None:
            ttk.Label(frame, text=text).grid(row=index, column=0, sticky="w",
                                             pady=6, padx=(0, 16))

        row(0, "Модель распознавания речи")
        self.whisper = tk.StringVar(value=s["whisper_model"])
        ttk.Combobox(frame, textvariable=self.whisper, values=WHISPER_MODELS,
                     state="readonly", width=14).grid(row=0, column=1, sticky="w")

        row(1, "Язык речи")
        names = list(LANGUAGES)
        current = next((n for n, c in LANGUAGES.items() if c == s["language"]),
                       names[0])
        self.language = tk.StringVar(value=current)
        ttk.Combobox(frame, textvariable=self.language, values=names,
                     state="readonly", width=30).grid(row=1, column=1, sticky="w")

        row(2, "Интервал кадров, сек")
        self.interval = tk.IntVar(value=s["interval"])
        ttk.Spinbox(frame, from_=2, to=30, textvariable=self.interval,
                    width=8).grid(row=2, column=1, sticky="w")

        row(3, "API-токен LM Studio")
        self.token = tk.StringVar(value=s["api_key"])
        self.token_entry = ttk.Entry(frame, textvariable=self.token, show="•",
                                     width=34)
        self.token_entry.grid(row=3, column=1, sticky="ew")
        ttk.Label(frame, foreground=COLORS["idle"], wraplength=360,
                  text="Нужен, только если в LM Studio включена авторизация "
                       "сервера (Developer → Server Settings)."
                  ).grid(row=4, column=1, sticky="w", pady=(0, 6))

        self.concepts = tk.BooleanVar(value=s["concepts"])
        ttk.Checkbutton(frame, text="Создавать заметки для понятий ([[ссылки]])",
                        variable=self.concepts).grid(row=5, column=0,
                                                     columnspan=2, sticky="w", pady=2)
        self.transcript = tk.BooleanVar(value=s["transcript"])
        ttk.Checkbutton(frame, text="Включать транскрипт в заметку",
                        variable=self.transcript).grid(row=6, column=0,
                                                       columnspan=2, sticky="w", pady=2)

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Сохранить", style="Accent.TButton",
                   command=self.save).pack(side="right", padx=8)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self.save())
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + 80
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        (self.token_entry if focus_token else self).focus_set()

    def save(self) -> None:
        try:
            interval = max(2, min(30, int(self.interval.get())))
        except (tk.TclError, ValueError):
            interval = self.app.settings["interval"]
        self.app.settings.update(
            whisper_model=self.whisper.get(),
            language=LANGUAGES[self.language.get()],
            interval=interval,
            api_key=self.token.get().strip(),
            concepts=self.concepts.get(),
            transcript=self.transcript.get(),
        )
        self.app.apply_settings()
        self.destroy()


class App(tk.Tk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        enable_dpi_awareness()
        super().__init__()
        self.title("Video Conspector")
        self.minsize(820, 680)
        self.msgs: queue.Queue = queue.Queue()
        self.items: dict = {}
        self.worker: Optional[threading.Thread] = None
        self.started_at = 0.0
        self.stage = -1
        self.status: dict = {"level": "idle", "models": [], "model": None}
        self.model_ids: dict = {AUTO_LABEL: lmstudio.AUTO}
        self.settings = self._load_settings()
        self._setup_logging()
        self._apply_theme(self.settings["theme"])
        self._build()
        self.apply_settings(refresh=False)
        self.geometry(self.settings.get("geometry") or "900x760")
        self.bind("<Escape>", lambda _e: self.cancel() if self.running else None)
        self.bind("<F5>", lambda _e: self.refresh_llm())
        self.bind("<Control-Return>", lambda _e: self.start())
        self.bind("<Control-l>", lambda _e: self.url_entry.focus_set())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)
        self.after(500, self._tick)
        self.refresh_llm()
        self.after(LLM_POLL_MS, self._auto_refresh)
        self._prefill_from_clipboard()

    # ---------- настройки и тема ----------
    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _load_settings(self) -> dict:
        defaults = {
            "output": str(config.BASE_DIR / "outputs"),
            "whisper_model": config.WHISPER_MODEL,
            "language": config.WHISPER_LANGUAGE or "",
            "interval": config.FRAME_INTERVAL,
            "api_key": config.LM_STUDIO_API_KEY,
            "concepts": True, "transcript": True,
            "theme": "auto", "model": lmstudio.AUTO,
            "geometry": "", "log_open": False,
        }
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update({k: v for k, v in data.items() if k in defaults})
        except (OSError, ValueError):
            pass
        return defaults

    def save_settings(self) -> None:
        self.settings["geometry"] = self.geometry()
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    def apply_settings(self, refresh: bool = True) -> None:
        """Передаёт настройки в движок и сохраняет их."""
        s = self.settings
        config.WHISPER_MODEL = s["whisper_model"]
        config.WHISPER_LANGUAGE = s["language"] or None
        config.LM_STUDIO_API_KEY = s["api_key"]
        config.LM_STUDIO_MODEL = s["model"] or lmstudio.AUTO
        self.save_settings()
        if refresh:
            self.refresh_llm()

    def _dark(self, theme: str) -> bool:
        return system_prefers_dark() if theme == "auto" else theme == "dark"

    def _apply_theme(self, theme: str) -> None:
        dark = self._dark(theme)
        if sv_ttk:
            sv_ttk.set_theme("dark" if dark else "light")
        self.dark = dark
        try:  # тёмный заголовок окна в Windows 10/11
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except (AttributeError, OSError):
            pass
        if hasattr(self, "log"):
            self._style_log()

    def toggle_theme(self) -> None:
        self.settings["theme"] = "light" if self.dark else "dark"
        self._apply_theme(self.settings["theme"])
        self.save_settings()

    def _style_log(self) -> None:
        bg, fg = ("#1f1f1f", "#e6e6e6") if self.dark else ("#ffffff", "#1f1f1f")
        self.log.configure(background=bg, foreground=fg, insertbackground=fg,
                           relief="flat", highlightthickness=1,
                           highlightbackground="#3a3a3a" if self.dark else "#d0d0d0")
        self.log.tag_configure("warn", foreground=COLORS["warn"])
        self.log.tag_configure("error", foreground=COLORS["bad"])

    # ---------- построение интерфейса ----------
    def _build(self) -> None:
        root = ttk.Frame(self, padding=(20, 16))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        # Шапка
        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Video Conspector",
                  font=("Segoe UI Semibold", 18)).grid(row=0, column=0, sticky="w")
        ttk.Label(header, foreground=COLORS["idle"],
                  text="Конспект видео в формате Obsidian на локальной модели"
                  ).grid(row=1, column=0, sticky="w")
        ttk.Button(header, text="Тема", command=self.toggle_theme
                   ).grid(row=0, column=1, rowspan=2, padx=4)
        ttk.Button(header, text="Настройки",
                   command=lambda: SettingsDialog(self)
                   ).grid(row=0, column=2, rowspan=2)

        # LM Studio
        llm = ttk.LabelFrame(root, text="Языковая модель (LM Studio)", padding=12)
        llm.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        llm.columnconfigure(1, weight=1)
        self.llm_dot = ttk.Label(llm, text="●", foreground=COLORS["idle"],
                                 font=("Segoe UI", 14))
        self.llm_dot.grid(row=0, column=0, padx=(0, 8))
        self.llm_text = ttk.Label(llm, text="Проверка соединения…", wraplength=560)
        self.llm_text.grid(row=0, column=1, sticky="w")
        self.token_btn = ttk.Button(
            llm, text="Ввести токен…",
            command=lambda: SettingsDialog(self, focus_token=True))
        self.token_btn.grid(row=0, column=2, padx=8)
        self.token_btn.grid_remove()
        self.model_var = tk.StringVar(value=AUTO_LABEL)
        self.model_box = ttk.Combobox(llm, textvariable=self.model_var,
                                      state="readonly", values=[AUTO_LABEL])
        self.model_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.model_box.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.refresh_btn = ttk.Button(llm, text="Обновить", command=self.refresh_llm)
        self.refresh_btn.grid(row=1, column=2, padx=8, pady=(10, 0))

        # Добавление видео
        add = ttk.LabelFrame(root, text="Видео", padding=12)
        add.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        add.columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_var.trace_add("write", lambda *_: self._validate_url())
        self.url_entry = ttk.Entry(add, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self.add_url())
        ttk.Button(add, text="Вставить", command=self.paste_url
                   ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(add, text="Добавить в очередь", command=self.add_url
                   ).grid(row=0, column=2, padx=(8, 0))
        self.url_hint = ttk.Label(add, text="Вставьте ссылку на видео (YouTube и "
                                  "другие сайты, поддерживаемые yt-dlp)",
                                  foreground=COLORS["idle"])
        self.url_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Очередь
        queue_frame = ttk.Frame(add)
        queue_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        queue_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(queue_frame, columns=("status",), height=4,
                                 show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Видео", anchor="w")
        self.tree.heading("status", text="Статус", anchor="w")
        self.tree.column("#0", stretch=True, width=480)
        self.tree.column("status", width=190, stretch=False)
        for state, color in ITEM_COLORS.items():
            self.tree.tag_configure(state, foreground=color)
        self.tree.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(queue_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda _e: self.open_note())
        self.tree.bind("<Delete>", lambda _e: self.remove_selected())
        self.tree.bind("<Button-3>", self._context_menu)
        self.queue_hint = ttk.Label(queue_frame, foreground=COLORS["idle"],
                                    text="Очередь пуста: добавьте одну или "
                                    "несколько ссылок.")
        self.queue_hint.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Открыть заметку", command=self.open_note)
        self.menu.add_command(label="Копировать ссылку", command=self.copy_url)
        self.menu.add_separator()
        self.menu.add_command(label="Удалить из очереди", command=self.remove_selected)

        # Папка результата
        out = ttk.LabelFrame(root, text="Папка результата", padding=12)
        out.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        out.columnconfigure(0, weight=1)
        self.out_var = tk.StringVar(value=self.settings["output"])
        ttk.Entry(out, textvariable=self.out_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out, text="Обзор…", command=self.choose_folder
                   ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(out, text="Открыть папку", command=self.open_folder
                   ).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(out, foreground=COLORS["idle"],
                  text="Можно указать папку хранилища Obsidian — заметки и "
                       "папка «Понятия» появятся сразу в нём."
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Ход работы
        run = ttk.Frame(root)
        run.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        run.columnconfigure(0, weight=1)
        stepper = ttk.Frame(run)
        stepper.grid(row=0, column=0, sticky="ew")
        self.stage_labels = []
        for i, name in enumerate(vc.STAGES):
            label = ttk.Label(stepper, text=f"○ {name}", foreground=COLORS["idle"])
            label.grid(row=0, column=i, padx=(0, 18), sticky="w")
            self.stage_labels.append(label)
        self.progress = ttk.Progressbar(run, maximum=1000, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.detail_var = tk.StringVar(value="Готов к работе")
        ttk.Label(run, textvariable=self.detail_var).grid(row=2, column=0,
                                                          sticky="w", pady=(6, 0))
        buttons = ttk.Frame(run)
        buttons.grid(row=0, column=1, rowspan=3, padx=(16, 0))
        self.start_btn = ttk.Button(buttons, text="Начать", style="Accent.TButton",
                                    command=self.start, width=14)
        self.start_btn.pack(fill="x")
        self.cancel_btn = ttk.Button(buttons, text="Отмена", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(fill="x", pady=(6, 0))
        self.note_btn = ttk.Button(buttons, text="Открыть заметку",
                                   command=self.open_note, state="disabled")
        self.note_btn.pack(fill="x", pady=(6, 0))

        # Журнал
        self.log_frame = ttk.Frame(root)
        self.log_frame.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(1, weight=1)
        bar = ttk.Frame(self.log_frame)
        bar.grid(row=0, column=0, sticky="ew")
        self.log_toggle = ttk.Button(bar, command=self.toggle_log)
        self.log_toggle.pack(side="left")
        ttk.Button(bar, text="Копировать", command=self.copy_log).pack(side="left", padx=6)
        self.log_box = ttk.Frame(self.log_frame)
        self.log_box.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.log_box.columnconfigure(0, weight=1)
        self.log_box.rowconfigure(0, weight=1)
        self.log = tk.Text(self.log_box, height=6, wrap="word", state="disabled",
                           font=("Consolas", 9), padx=8, pady=6)
        log_scroll = ttk.Scrollbar(self.log_box, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")
        self._style_log()
        self._set_log_open(self.settings["log_open"])
        self._refresh_queue_hint()

    # ---------- LM Studio ----------
    def refresh_llm(self) -> None:
        self.refresh_btn.configure(state="disabled")
        preferred = self.settings["model"] or lmstudio.AUTO
        key = self.settings["api_key"]

        def work() -> None:
            config.LM_STUDIO_API_KEY = key
            self.msgs.put(("llm", lmstudio.status(preferred)))

        threading.Thread(target=work, daemon=True).start()

    def _auto_refresh(self) -> None:
        if not self.running:
            self.refresh_llm()
        self.after(LLM_POLL_MS, self._auto_refresh)

    def _show_llm(self, status: dict) -> None:
        self.status = status
        self.llm_dot.configure(foreground=COLORS.get(status["level"], COLORS["idle"]))
        self.llm_text.configure(text=status["text"])
        if status.get("auth"):
            self.token_btn.grid()
        else:
            self.token_btn.grid_remove()
        # Список моделей: выбранная пользователем остаётся, даже если пропала
        self.model_ids = {AUTO_LABEL: lmstudio.AUTO}
        for m in status["models"]:
            mark = "●" if m["loaded"] else "○"
            ctx = f" · контекст {m['context']}" if m.get("context") else ""
            self.model_ids[f"{mark} {m['id']}{ctx}"] = m["id"]
        chosen = self.settings["model"] or lmstudio.AUTO
        if chosen != lmstudio.AUTO and chosen not in self.model_ids.values():
            self.model_ids[f"○ {chosen} (недоступна)"] = chosen
        self.model_box.configure(values=list(self.model_ids))
        label = next((k for k, v in self.model_ids.items() if v == chosen), AUTO_LABEL)
        self.model_var.set(label)
        self.refresh_btn.configure(state="normal")

    def _on_model_selected(self, _event=None) -> None:
        self.settings["model"] = self.model_ids.get(self.model_var.get(),
                                                    lmstudio.AUTO)
        self.apply_settings()

    # ---------- очередь ----------
    def _validate_url(self) -> bool:
        text = self.url_var.get().strip()
        if not text:
            self.url_hint.configure(foreground=COLORS["idle"], text=(
                "Вставьте ссылку на видео (YouTube и другие сайты, "
                "поддерживаемые yt-dlp)"))
            return False
        if not URL_RE.match(text):
            self.url_hint.configure(foreground=COLORS["bad"], text=(
                "Это не похоже на ссылку: она должна начинаться с http:// или https://"))
            return False
        self.url_hint.configure(foreground=COLORS["ok"], text="Ссылка принята")
        return True

    def _prefill_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if URL_RE.match(text) and any(h in text for h in ("youtube.com", "youtu.be")):
            self.url_var.set(text)
        self.url_entry.focus_set()

    def paste_url(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            return
        self.url_entry.focus_set()

    def add_url(self) -> Optional[str]:
        if not self._validate_url():
            self.url_entry.focus_set()
            return None
        url = self.url_var.get().strip()
        for iid, item in self.items.items():
            if item.url == url and item.state in ("queued", "running"):
                self.url_hint.configure(foreground=COLORS["warn"],
                                        text="Эта ссылка уже в очереди")
                return iid
        iid = self.tree.insert("", "end", text=url, values=(ITEM_TEXT["queued"],),
                               tags=("queued",))
        self.items[iid] = Item(url=url)
        self.url_var.set("")
        self._refresh_queue_hint()
        threading.Thread(target=lambda: self.msgs.put(
            ("title", iid, vc.fetch_title(url))), daemon=True).start()
        return iid

    def remove_selected(self) -> None:
        for iid in self.tree.selection():
            item = self.items.get(iid)
            if item and item.state == "running":
                continue
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._refresh_queue_hint()

    def copy_url(self) -> None:
        urls = [self.items[i].url for i in self.tree.selection() if i in self.items]
        if urls:
            self.clipboard_clear()
            self.clipboard_append("\n".join(urls))

    def _context_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _set_item(self, iid: str, state: str, path: Optional[Path] = None) -> None:
        item = self.items.get(iid)
        if not item:
            return
        item.state = state
        if path:
            item.path = path
        label = ITEM_TEXT[state]
        if state == "running" and self.stage >= 0:
            label = f"{ITEM_TEXT[state]}: {vc.STAGES[self.stage].lower()}"
        self.tree.item(iid, values=(label,), tags=(state,))

    def _refresh_queue_hint(self) -> None:
        if self.items:
            self.queue_hint.grid_remove()
        else:
            self.queue_hint.grid()

    # ---------- запуск ----------
    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.out_var.get() or None)
        if folder:
            self.out_var.set(folder)
            self.settings["output"] = folder
            self.save_settings()

    def start(self) -> None:
        if self.running:
            return
        if self.url_var.get().strip():
            self.add_url()
        pending = [i for i, it in self.items.items()
                   if it.state in ("queued", "error", "cancelled")]
        if not pending:
            self.detail_var.set("Добавьте ссылку на видео, затем нажмите «Начать»")
            self.url_entry.focus_set()
            return
        status = self.status
        if status["level"] == "bad" or status.get("model") is None:
            reason = status["text"]
            if not messagebox.askyesno(
                    "Языковая модель недоступна",
                    f"{reason}\n\nБез модели сохранится только сырой текст "
                    "(транскрипт и распознанный код). Продолжить?"):
                return
        self.settings["output"] = self.out_var.get()
        self.apply_settings(refresh=False)
        output = Path(self.settings["output"])
        options = (self.settings["interval"], self.settings["concepts"],
                   self.settings["transcript"])
        for iid in pending:
            self._set_item(iid, "queued")
        vc.cancel_event.clear()
        self.queue_handler.last_error = ""
        self.started_at = time.time()
        self.stage = -1
        self._set_stage(0, 0.0, "Подготовка")
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.note_btn.configure(state="disabled")
        self.worker = threading.Thread(
            target=self._work, args=(pending, output, options), daemon=True)
        self.worker.start()

    def _work(self, ids: list, output: Path, options: tuple) -> None:
        interval, concepts, transcript = options
        current = {"id": None}
        vc.progress_hook = lambda s, f, d: self.msgs.put(
            ("progress", current["id"], s, f, d))
        try:
            for iid in ids:
                if vc.cancel_event.is_set():
                    break
                current["id"] = iid
                self.msgs.put(("item", iid, "running", None))
                try:
                    state, path = vc.run_pipeline(
                        self.items[iid].url, output, interval,
                        make_concepts=concepts, include_transcript=transcript)
                except Exception as exc:  # окно не должно падать из-за сбоя
                    logging.getLogger("video_conspector").exception("Сбой: %s", exc)
                    state, path = "error", None
                self.msgs.put(("item", iid, state, path))
                if state == "cancelled":
                    break
        finally:
            vc.progress_hook = None
            self.msgs.put(("finished",))

    def cancel(self) -> None:
        if not self.running:
            return
        vc.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self.detail_var.set("Отмена… дождитесь завершения текущего шага")

    # ---------- ход работы ----------
    def _set_stage(self, stage: int, fraction: float, detail: str) -> None:
        if stage != self.stage:
            self.stage = stage
            for i, label in enumerate(self.stage_labels):
                name = vc.STAGES[i]
                if i < stage:
                    label.configure(text=f"✓ {name}", foreground=COLORS["ok"])
                elif i == stage:
                    label.configure(text=f"● {name}", foreground=COLORS["accent"])
                else:
                    label.configure(text=f"○ {name}", foreground=COLORS["idle"])
        done = sum(STAGE_WEIGHTS[:stage]) + STAGE_WEIGHTS[stage] * fraction
        self.progress["value"] = done * 1000
        percent = int(fraction * 100)
        self._detail = f"{vc.STAGES[stage]} · {detail}" if detail else vc.STAGES[stage]
        if detail and 0 < fraction < 1 and "из" not in detail:
            self._detail += f" · {percent}%"
        self._update_detail()

    def _update_detail(self) -> None:
        if self.running:
            elapsed = format_elapsed(time.time() - self.started_at)
            self.detail_var.set(f"{getattr(self, '_detail', '')}   ·   {elapsed}")

    def _tick(self) -> None:
        self._update_detail()
        self.after(500, self._tick)

    def _reset_stages(self) -> None:
        self.stage = -1
        for i, label in enumerate(self.stage_labels):
            label.configure(text=f"○ {vc.STAGES[i]}", foreground=COLORS["idle"])
        self.progress["value"] = 0

    def _finish(self) -> None:
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        states = [it.state for it in self.items.values()]
        done = [it for it in self.items.values() if it.path]
        if done:
            self.note_btn.configure(state="normal")
        elapsed = format_elapsed(time.time() - self.started_at)
        if vc.cancel_event.is_set():
            self.detail_var.set(f"Отменено · {elapsed}")
            self._reset_stages()
            return
        ok = states.count("ok")
        raw = states.count("raw")
        failed = states.count("error")
        parts = [f"готово: {ok}"]
        if raw:
            parts.append(f"только сырой текст: {raw}")
        if failed:
            parts.append(f"ошибок: {failed}")
        self.detail_var.set(f"Завершено · {', '.join(parts)} · {elapsed}")
        if ok and not failed:
            self.progress["value"] = 1000
            for i, label in enumerate(self.stage_labels):
                label.configure(text=f"✓ {vc.STAGES[i]}", foreground=COLORS["ok"])
        else:
            self._reset_stages()
        self.bell()
        if raw and not failed:
            messagebox.showwarning(
                "Конспект не построен",
                f"{self.queue_handler.last_error or 'Языковая модель недоступна.'}"
                "\n\nТранскрипт и распознанный код сохранены в raw_text.md — "
                "после настройки LM Studio запустите обработку ещё раз.")
        if failed:
            self._set_log_open(True)
            reason = self.queue_handler.last_error or "см. журнал"
            messagebox.showerror(
                "Не удалось обработать видео",
                f"{reason}\n\nПолный журнал: {config.BASE_DIR / 'video_conspector.log'}")

    # ---------- журнал ----------
    def _setup_logging(self) -> None:
        self.queue_handler = _QueueHandler(self.msgs)
        self.queue_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
        root = logging.getLogger()
        root.addHandler(self.queue_handler)
        root.setLevel(logging.INFO)
        try:  # полный журнал рядом с программой — удобно присылать при ошибках
            file_handler = logging.FileHandler(
                config.BASE_DIR / "video_conspector.log", encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s"))
            root.addHandler(file_handler)
        except OSError:
            pass
        for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    def _append_log(self, level: int, text: str) -> None:
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")
        if level >= logging.ERROR and not self.settings["log_open"]:
            self._set_log_open(True)

    def _set_log_open(self, opened: bool) -> None:
        self.settings["log_open"] = opened
        self.log_toggle.configure(text="Журнал ▾" if opened else "Журнал ▸")
        if opened:
            self.log_box.grid()
            self.log_frame.rowconfigure(1, weight=1)
        else:
            self.log_box.grid_remove()
            self.log_frame.rowconfigure(1, weight=0)

    def toggle_log(self) -> None:
        self._set_log_open(not self.settings["log_open"])

    def copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end").strip())

    # ---------- результат ----------
    def _selected_path(self) -> Optional[Path]:
        for iid in self.tree.selection():
            item = self.items.get(iid)
            if item and item.path:
                return item.path
        paths = [it.path for it in self.items.values() if it.path]
        return paths[-1] if paths else None

    def open_note(self) -> None:
        path = self._selected_path()
        if path and path.exists():
            os.startfile(path)  # noqa: S606 (локальный файл)

    def open_folder(self) -> None:
        folder = Path(self.out_var.get())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606

    # ---------- очередь сообщений ----------
    def _poll(self) -> None:
        try:
            while True:
                kind, *payload = self.msgs.get_nowait()
                if kind == "log":
                    self._append_log(*payload)
                elif kind == "llm":
                    self._show_llm(payload[0])
                elif kind == "title":
                    iid, title = payload
                    if title and iid in self.items:
                        self.items[iid].title = title
                        self.tree.item(iid, text=title)
                elif kind == "progress":
                    _iid, stage, fraction, detail = payload
                    self._set_stage(stage, fraction, detail)
                    if _iid in self.items:
                        self._set_item(_iid, "running")
                elif kind == "item":
                    iid, state, path = payload
                    self._set_item(iid, state, path)
                elif kind == "finished":
                    self._finish()
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("Идёт обработка",
                                       "Остановить обработку и выйти?"):
                return
            vc.cancel_event.set()
        self.settings["output"] = self.out_var.get()
        self.save_settings()
        self.destroy()


def selftest(audio: Path, report: Path) -> None:
    """Проверяет зависимости и распознаёт короткое аудио; пишет отчёт в файл."""
    lines = []
    try:
        import av  # noqa: F401
        import ctranslate2
        import faster_whisper
        import pytesseract
        import yt_dlp  # noqa: F401
        lines.append(f"imports ok (ctranslate2 {ctranslate2.__version__}, "
                     f"faster_whisper {faster_whisper.__version__})")
        lines.append(f"ffmpeg: {bool(__import__('shutil').which('ffmpeg'))}")
        lines.append(f"tesseract: {config.TESSERACT_CMD or pytesseract.pytesseract.tesseract_cmd}")
        root = tk.Tk()
        root.withdraw()
        if sv_ttk:
            sv_ttk.set_theme("dark")
        lines.append(f"theme: {ttk.Style(root).theme_use()}")
        root.destroy()
        text = vc.transcribe_audio(audio)
        lines.append(f"transcript: {text!r}")
    except Exception as exc:  # отчёт важнее трассировки
        lines.append(f"FAILED: {type(exc).__name__}: {exc}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    """Запускает окно; `--run URL ПАПКА` — обработка без окна,
    `--selftest АУДИО ОТЧЁТ` — проверка сборки."""
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        selftest(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--run":
        log_file = Path(sys.argv[3]) / "run.log"
        Path(sys.argv[3]).mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, filename=log_file,
                            encoding="utf-8", format="%(asctime)s | %(message)s")
        status, path = vc.run_pipeline(sys.argv[2], Path(sys.argv[3]))
        logging.info("STATUS=%s PATH=%s", status, path)
        return
    App().mainloop()


if __name__ == "__main__":
    main()
