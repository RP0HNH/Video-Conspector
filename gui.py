"""Простой графический интерфейс Video Conspector (tkinter)."""

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import requests


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
import video_conspector as vc  # noqa: E402

SETTINGS_FILE = config.BASE_DIR / "gui_settings.json"
WHISPER_MODELS = ("tiny", "base", "small", "medium")
MIN_CONTEXT = 8192
COLORS = {"ok": "#2e7d32", "warn": "#ef6c00", "bad": "#c62828", "idle": "#757575"}


def lm_studio_status() -> tuple:
    """Проверяет LM Studio; возвращает (уровень, текст) для индикатора."""
    base = config.LM_STUDIO_URL.split("/v1/")[0]
    try:
        try:
            data = requests.get(f"{base}/api/v0/models", timeout=4).json()["data"]
            models = [m for m in data if m.get("type") in (None, "llm", "vlm")]
        except (requests.RequestException, KeyError, ValueError):
            ids = requests.get(f"{base}/v1/models", timeout=4).json()["data"]
            models = [{"id": m["id"], "state": "loaded"} for m in ids]
    except (requests.RequestException, KeyError, ValueError):
        return "bad", "LM Studio недоступна. Запустите сервер (порт 1234)."

    mine = next((m for m in models if m["id"] == config.LM_STUDIO_MODEL), None)
    if mine is None:
        names = ", ".join(m["id"] for m in models[:4]) or "нет моделей"
        return "warn", f"Сервер работает, но модели {config.LM_STUDIO_MODEL} нет ({names})."
    if mine.get("state") not in (None, "loaded"):
        return "warn", f"Сервер работает, модель {mine['id']} не загружена в память."
    ctx = mine.get("loaded_context_length")
    if ctx and ctx < MIN_CONTEXT:
        return "warn", f"Модель загружена, но контекст мал ({ctx}). Нужно от {MIN_CONTEXT}."
    suffix = f", контекст {ctx}" if ctx else ""
    return "ok", f"Подключено: {mine['id']}{suffix}."


class _QueueHandler(logging.Handler):
    """Передаёт сообщения логов в очередь окна."""

    def __init__(self, target: queue.Queue):
        super().__init__(logging.INFO)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        self.target.put(("log", self.format(record)))


class App(tk.Tk):
    """Главное окно приложения."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Video Conspector")
        self.minsize(640, 520)
        self.messages: queue.Queue = queue.Queue()
        self.result_path: Optional[Path] = None
        self.worker: Optional[threading.Thread] = None
        self._build()
        self._load_settings()
        self._setup_logging()
        self.after(150, self._poll)
        self.check_llm()

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(6, weight=1)

        ttk.Label(frame, text="LM Studio:").grid(row=0, column=0, sticky="w", **pad)
        self.llm_label = ttk.Label(frame, text="проверка...", foreground=COLORS["idle"])
        self.llm_label.grid(row=0, column=1, sticky="w", **pad)
        self.check_btn = ttk.Button(frame, text="Проверить", command=self.check_llm)
        self.check_btn.grid(row=0, column=2, **pad)

        ttk.Label(frame, text="Ссылка на видео:").grid(row=1, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(frame, textvariable=self.url_var)
        url_entry.grid(row=1, column=1, sticky="ew", **pad)
        url_entry.focus_set()
        ttk.Button(frame, text="Вставить", command=self.paste_url).grid(
            row=1, column=2, **pad)

        ttk.Label(frame, text="Папка результата:").grid(row=2, column=0, sticky="w", **pad)
        self.out_var = tk.StringVar(value=str(config.BASE_DIR / "outputs"))
        ttk.Entry(frame, textvariable=self.out_var).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frame, text="Обзор...", command=self.choose_folder).grid(
            row=2, column=2, **pad)

        options = ttk.Frame(frame)
        options.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(options, text="Модель речи:").pack(side="left")
        self.model_var = tk.StringVar(value=config.WHISPER_MODEL)
        ttk.Combobox(options, textvariable=self.model_var, values=WHISPER_MODELS,
                     width=8, state="readonly").pack(side="left", padx=(4, 16))
        self.concepts_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Заметки для понятий",
                        variable=self.concepts_var).pack(side="left", padx=(0, 12))
        self.transcript_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Транскрипт в заметке",
                        variable=self.transcript_var).pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.start_btn = ttk.Button(buttons, text="Начать конспектирование",
                                    command=self.start)
        self.start_btn.pack(side="left")
        self.open_note_btn = ttk.Button(buttons, text="Открыть заметку",
                                        command=self.open_note, state="disabled")
        self.open_note_btn.pack(side="left", padx=8)
        self.open_dir_btn = ttk.Button(buttons, text="Открыть папку",
                                       command=self.open_folder)
        self.open_dir_btn.pack(side="left")

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)

        log_frame = ttk.Frame(frame)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        self.log = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.status_var = tk.StringVar(value="Готов к работе.")
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=7, column=0, columnspan=3, sticky="w", **pad)

    def _setup_logging(self) -> None:
        handler = _QueueHandler(self.messages)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        # Служебные сообщения библиотек в окне не нужны
        for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.out_var.set(data.get("output", self.out_var.get()))
        self.model_var.set(data.get("whisper_model", self.model_var.get()))
        self.concepts_var.set(data.get("concepts", True))
        self.transcript_var.set(data.get("transcript", True))

    def _save_settings(self) -> None:
        data = {
            "output": self.out_var.get(),
            "whisper_model": self.model_var.get(),
            "concepts": self.concepts_var.get(),
            "transcript": self.transcript_var.get(),
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except OSError:
            pass

    def paste_url(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.out_var.get() or None)
        if folder:
            self.out_var.set(folder)

    def check_llm(self) -> None:
        self.check_btn.configure(state="disabled")
        self.llm_label.configure(text="проверка...", foreground=COLORS["idle"])

        def work() -> None:
            self.messages.put(("llm", lm_studio_status()))

        threading.Thread(target=work, daemon=True).start()

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self) -> None:
        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("Ссылка", "Вставьте ссылку на видео (http/https).")
            return
        level, text = lm_studio_status()
        if level != "ok" and not messagebox.askyesno(
                "LM Studio", f"{text}\n\nБез модели будет сохранён только "
                "сырой текст (транскрипт и OCR). Продолжить?"):
            return
        self._save_settings()
        config.WHISPER_MODEL = self.model_var.get()
        output = Path(self.out_var.get())
        self.result_path = None
        self.open_note_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set("Идёт обработка... Окно можно не закрывать.")

        def work() -> None:
            try:
                result = vc.run_pipeline(
                    url, output,
                    make_concepts=self.concepts_var.get(),
                    include_transcript=self.transcript_var.get(),
                )
            except Exception as exc:  # окно не должно падать из-за сбоя
                logging.getLogger("video_conspector").exception("Сбой: %s", exc)
                result = ("error", None)
            self.messages.put(("done", result))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _finish(self, status: str, path: Optional[Path]) -> None:
        self.progress.stop()
        self.start_btn.configure(state="normal")
        self.result_path = path
        if path:
            self.open_note_btn.configure(state="normal")
        self.status_var.set({
            "ok": f"Готово: {path.name}" if path else "Готово.",
            "raw": "LM Studio недоступна — сохранён только сырой текст.",
            "error": "Ошибка. Подробности в журнале выше.",
        }[status])

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "llm":
                    level, text = payload
                    self.llm_label.configure(text=text, foreground=COLORS[level])
                    self.check_btn.configure(state="normal")
                elif kind == "done":
                    self._finish(*payload)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def open_note(self) -> None:
        if self.result_path and self.result_path.exists():
            os.startfile(self.result_path)  # noqa: S606 (локальный файл)

    def open_folder(self) -> None:
        folder = Path(self.out_var.get())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)  # noqa: S606


def main() -> None:
    """Запускает окно; `--run URL ПАПКА` выполняет обработку без окна."""
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
