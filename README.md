# Video Conspector

Конспект YouTube-видео в формате Obsidian: речь (faster-whisper) + код с экрана (Tesseract) + локальная модель в LM Studio.

## Нужно установить

- **FFmpeg** (в `PATH`) и **Tesseract OCR** с языками `eng`, `rus`.
- **LM Studio**: сервер на порту `1234`, модель `qwen/qwen2.5-coder-14b` с контекстом от 16384:
  `lms load qwen/qwen2.5-coder-14b --context-length 16384 --gpu max -y`
- **Python 3.10–3.12 с python.org** (не Anaconda) — только если запускаете из исходников или собираете exe.

## Запуск

Из исходников (для повседневной работы exe не нужен):

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python gui.py
```

В окне виден статус LM Studio; вставьте ссылку и нажмите «Начать конспектирование». Из консоли:

```bash
python video_conspector.py --url "https://youtube.com/watch?v=..." --output "./outputs"
```

Готовый exe: `powershell -File build_exe.ps1` → `dist\VideoConspector\VideoConspector.exe` (папку копировать целиком). Сборка сама создаёт `.venv` из обычного Python.

## Настройка

Скопируйте `.env.example` в `.env` (для exe — рядом с `VideoConspector.exe`). Главное: `LM_STUDIO_MODEL`, `WHISPER_MODEL` (`small` по умолчанию), `TESSERACT_CMD`.

## Результат

В папке результата (можно указать хранилище Obsidian): заметка с названием по смыслу и папка `Понятия/`. Если LM Studio недоступна — `raw_text.md` с транскриптом и OCR.
