"""Video Conspector: конспект YouTube-видео с помощью локального ИИ."""

import argparse
import difflib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests

import config
import obsidian_note

logger = logging.getLogger("video_conspector")

_TS_LINE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")


def setup_logging() -> None:
    """Настраивает вывод прогресса в консоль."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def format_timestamp(seconds: float) -> str:
    """Преобразует секунды в строку HH:MM:SS."""
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def run_ffmpeg(args: list) -> None:
    """Запускает ffmpeg и выбрасывает RuntimeError при ошибке."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg завершился с ошибкой: {result.stderr.strip()}")


def download_video(url: str, temp_dir: Path) -> Optional[tuple]:
    """Скачивает видео (не выше 1080p).

    Возвращает (путь к файлу, метаданные видео) или None при ошибке.
    """
    try:
        import yt_dlp

        height = config.MAX_VIDEO_HEIGHT
        options = {
            "format": (
                f"bv*[height<={height}]+ba/b[height<={height}]/b"
            ),
            "outtmpl": str(Path(temp_dir) / "video.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            raw_info = ydl.extract_info(url, download=True)
        files = sorted(Path(temp_dir).glob("video.*"))
        if not files:
            raise FileNotFoundError("yt-dlp не создал файл видео")
        keys = ("id", "title", "uploader", "channel", "upload_date",
                "duration", "webpage_url")
        return files[0], {k: raw_info.get(k) for k in keys}
    except Exception as exc:
        logger.error("Ошибка скачивания видео: %s", exc)
        return None


def transcribe_audio(video_path: Path) -> str:
    """Извлекает аудио, распознаёт речь и возвращает текст с таймкодами."""
    audio_path = Path(video_path).with_suffix(".wav")
    try:
        from faster_whisper import WhisperModel

        run_ffmpeg(
            ["-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
             str(audio_path)]
        )
        model = WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
        segments, info = model.transcribe(
            str(audio_path), language=config.WHISPER_LANGUAGE,
            vad_filter=config.WHISPER_VAD,
        )
        logger.info("Язык аудио: %s", info.language)
        lines = [
            f"[{format_timestamp(seg.start)}] {seg.text.strip()}"
            for seg in segments
            if seg.text.strip()
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.error("Ошибка распознавания речи: %s", exc)
        return ""
    finally:
        audio_path.unlink(missing_ok=True)


def extract_frames(
    video_path: Path, temp_dir: Path, interval: int = config.FRAME_INTERVAL
) -> Optional[Path]:
    """Извлекает кадр каждые `interval` секунд в temp_dir/frames/."""
    frames_dir = Path(temp_dir) / "frames"
    try:
        frames_dir.mkdir(parents=True, exist_ok=True)
        run_ffmpeg(
            ["-i", str(video_path), "-vf", f"fps=1/{interval}", "-q:v", "2",
             str(frames_dir / "frame_%05d.jpg")]
        )
        return frames_dir
    except Exception as exc:
        logger.error("Ошибка извлечения кадров: %s", exc)
        return None


def _is_code_line(line: str) -> bool:
    """Проверяет, похожа ли строка на строку кода."""
    return bool(config.CODE_LINE_RE.search(line))


def _ocr_lines(image, pytesseract) -> list:
    """Возвращает уверенно распознанные строки кадра с сохранением отступов."""
    data = pytesseract.image_to_data(
        image, lang=config.OCR_LANGS, config=config.OCR_CONFIG,
        output_type=pytesseract.Output.DICT,
    )
    grouped = {}
    for i, word in enumerate(data["text"]):
        word = word.strip()
        try:
            conf = float(data["conf"][i])
        except ValueError:
            continue
        if not word or conf < config.OCR_MIN_CONF:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        grouped.setdefault(key, []).append(
            (data["left"][i], data["width"][i], word)
        )
    if not grouped:
        return []
    widths = [w / len(t) for ws in grouped.values() for _, w, t in ws]
    char_w = max(sum(widths) / len(widths), 1.0)
    lefts = sorted(ws[0][0] for ws in grouped.values())
    min_left = lefts[len(lefts) // 5]
    lines = []
    for ws in grouped.values():
        text = " ".join(t for _, _, t in ws)
        symbols = [c for c in text if not c.isspace()]
        good = sum(c.isalnum() or c in config.CODE_SYMBOLS for c in symbols)
        if len(symbols) < 3 or good / len(symbols) < config.OCR_MIN_GOOD_RATIO:
            continue
        indent = max(int((ws[0][0] - min_left) / char_w), 0)
        lines.append(" " * indent + text)
    return lines


def _new_lines(lines: list, seen: list) -> list:
    """Возвращает строки кадра, которых (даже примерно) ещё не было."""
    fresh = []
    for line in lines:
        norm = " ".join(line.split())
        if len(norm) < 6 or not _is_code_line(norm) or difflib.get_close_matches(
            norm, seen, n=1, cutoff=config.OCR_LINE_SIMILARITY
        ):
            continue
        seen.append(norm)
        fresh.append(line)
    return fresh


def extract_code_from_frames(
    frames_dir: Optional[Path], interval: int = config.FRAME_INTERVAL
) -> str:
    """Распознаёт код на кадрах (OCR); шум и повторы отбрасываются.

    В результат попадают только строки, не встречавшиеся на прошлых кадрах,
    с таймкодом кадра. Возвращает пустую строку, если кода на экране нет.
    """
    if frames_dir is None:
        return ""
    try:
        import pytesseract
        from PIL import Image, ImageOps

        if config.TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD

        blocks = []
        seen = []
        for index, frame in enumerate(sorted(Path(frames_dir).glob("*.jpg"))):
            try:
                with Image.open(frame) as img:
                    gray = ImageOps.autocontrast(img.convert("L"))
                    lines = _ocr_lines(gray, pytesseract)
            except pytesseract.TesseractNotFoundError:
                logger.error(
                    "Tesseract не найден: установите его или задайте "
                    "TESSERACT_CMD в .env. OCR пропущен."
                )
                return ""
            except Exception as exc:
                logger.warning("OCR не удался для %s: %s", frame.name, exc)
                continue
            code_lines = sum(_is_code_line(line) for line in lines)
            if (code_lines < config.OCR_MIN_CODE_LINES
                    or code_lines / len(lines) < config.OCR_MIN_CODE_RATIO):
                continue
            fresh = _new_lines(lines, seen)
            if len(fresh) < config.OCR_MIN_NEW_LINES:
                continue
            stamp = format_timestamp(index * interval)
            blocks.append(f"--- [{stamp}] ---\n" + "\n".join(fresh))
        return "\n\n".join(blocks)
    except Exception as exc:
        logger.error("Ошибка OCR: %s", exc)
        return ""


def _truncate(text: str, limit: int) -> str:
    """Обрезает текст до limit символов, сохраняя начало и конец."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[...пропущено...]\n{text[-half:]}"


def _chat(prompt: str, max_tokens: int, json_schema: Optional[dict] = None) -> str:
    """Отправляет один запрос в LM Studio и возвращает текст ответа.

    При переполнении контекста (HTTP 400) сокращает запрос вдвое и повторяет.
    """
    payload = {
        "model": config.LM_STUDIO_MODEL,
        "temperature": config.LM_STUDIO_TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "note_meta", "strict": True,
                            "schema": json_schema},
        }
    for attempt in range(config.LM_STUDIO_MAX_RETRIES + 1):
        payload["messages"] = [{"role": "user", "content": prompt}]
        response = requests.post(
            config.LM_STUDIO_URL, json=payload, timeout=config.LM_STUDIO_TIMEOUT
        )
        if response.status_code == 400 and attempt < config.LM_STUDIO_MAX_RETRIES:
            prompt = _truncate(prompt, len(prompt) // 2)
            logger.warning("Запрос не принят (контекст?), повтор с меньшим "
                           "входом (%d символов)", len(prompt))
            continue
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError("LM Studio не вернула ответ")


def _split_transcript(transcript: str, size: int) -> list:
    """Делит транскрипт на фрагменты: [(секунда начала, текст), ...]."""
    chunks, current, length, start = [], [], 0, 0
    for line in transcript.splitlines():
        if current and length + len(line) > size:
            chunks.append((start, "\n".join(current)))
            current, length = [], 0
        if not current:
            match = _TS_LINE_RE.match(line)
            if match:
                h, m, s = (int(g) for g in match.groups())
                start = h * 3600 + m * 60 + s
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append((start, "\n".join(current)))
    return chunks


def _parse_section(answer: str, index: int) -> tuple:
    """Отделяет заголовок раздела (первая строка) от текста."""
    lines = answer.strip().splitlines()
    heading = lines[0].strip(" #*:-") if lines else ""
    body = "\n".join(lines[1:]).strip()
    if not heading or len(heading) > 80 or not body:
        heading, body = f"Часть {index}", answer.strip()
    body = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"**\1**", body)
    return heading, body


def _as_list(value) -> list:
    """Приводит значение к списку непустых строк."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _normalize_meta(raw: dict, fallback_title: str) -> dict:
    """Приводит ответ модели к ожидаемым типам и размерам."""
    concepts = [
        {"term": str(c["term"]).strip(), "definition": str(c["definition"]).strip()}
        for c in raw.get("concepts", [])
        if isinstance(c, dict) and str(c.get("term", "")).strip()
        and str(c.get("definition", "")).strip()
    ][:12]
    questions = [
        {"question": str(q["question"]).strip(), "answer": str(q["answer"]).strip()}
        for q in raw.get("questions", [])
        if isinstance(q, dict) and str(q.get("question", "")).strip()
        and str(q.get("answer", "")).strip()
    ][:6]
    return {
        "title": str(raw.get("title") or fallback_title).strip(),
        "tldr": str(raw.get("tldr") or "").strip(),
        "tags": _as_list(raw.get("tags"))[:7],
        "difficulty": str(raw.get("difficulty") or "").strip(),
        "prerequisites": _as_list(raw.get("prerequisites"))[:5],
        "concepts": concepts,
        "steps": _as_list(raw.get("steps"))[:10],
        "questions": questions,
        "action_items": _as_list(raw.get("action_items"))[:6],
    }


def _generate_meta(notes: str, video_title: str) -> dict:
    """Просит модель выделить название, теги, понятия, вопросы и т.д."""
    prompt = config.META_PROMPT.format(
        video_title=video_title,
        notes=_truncate(notes, config.MAX_NOTES_CHARS),
    )
    answer = _chat(prompt, config.MAX_TOKENS_META, json_schema=config.META_SCHEMA)
    start, end = answer.find("{"), answer.rfind("}")
    return _normalize_meta(json.loads(answer[start:end + 1]), video_title)


def _generate_code_notes(ocr_text: str, video_title: str) -> str:
    """Просит модель оформить и объяснить код, найденный на экране."""
    blocks = ocr_text.split("\n\n")
    chunks, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) > config.OCR_CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current += block + "\n\n"
    chunks.append(current)
    answers = [
        _chat(config.CODE_PROMPT.format(video_title=video_title, ocr=chunk),
              config.MAX_TOKENS_CODE)
        for chunk in chunks if chunk.strip()
    ]
    return "\n\n".join(answers)


def summarize_with_lm_studio(
    transcript: str, ocr_text: str, info: Optional[dict] = None
) -> dict:
    """Строит конспект через LM Studio поэтапно.

    Возвращает {"meta": ..., "sections": [(секунда, заголовок, текст)],
    "code_md": ...}. Если LM Studio недоступна, выбрасывает исключение.
    """
    info = info or {}
    video_title = info.get("title") or "Видео"
    try:
        chunks = _split_transcript(transcript, config.CHUNK_CHARS)
        sections = []
        for i, (start, chunk) in enumerate(chunks, 1):
            logger.info("Конспект: часть %d из %d...", i, len(chunks))
            answer = _chat(
                config.SECTION_PROMPT.format(
                    video_title=video_title, index=i, total=len(chunks),
                    chunk=chunk),
                config.MAX_TOKENS_SECTION,
            )
            sections.append((start, *_parse_section(answer, i)))

        notes = "\n\n".join(f"{h}\n{b}" for _, h, b in sections)
        try:
            logger.info("Метаданные: название, теги, понятия, вопросы...")
            meta = _generate_meta(notes or ocr_text, video_title)
        except (requests.ConnectionError, requests.Timeout):
            raise
        except Exception as exc:
            logger.warning("Метаданные не получены (%s), используются "
                           "значения по умолчанию", exc)
            meta = _normalize_meta({}, video_title)

        code_md = ""
        if ocr_text.strip():
            logger.info("Пояснения к коду...")
            try:
                code_md = _generate_code_notes(ocr_text, video_title)
            except (requests.ConnectionError, requests.Timeout):
                raise
            except Exception as exc:
                logger.warning("Пояснения к коду не получены: %s", exc)
                code_md = f"```\n{ocr_text}\n```"
        return {"meta": meta, "sections": sections, "code_md": code_md}
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        logger.error("Ошибка запроса к LM Studio: %s", exc)
        raise


def save_raw_text(output_dir: Path, transcript: str, ocr_text: str) -> Path:
    """Сохраняет транскрипт и OCR в raw_text.md."""
    path = output_dir / config.RAW_FILENAME
    path.write_text(
        f"# Транскрипт\n\n{transcript}\n\n# Текст с экрана (OCR)\n\n"
        f"{ocr_text.strip() or config.NO_CODE_PLACEHOLDER}\n",
        encoding="utf-8",
    )
    return path


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Конспект YouTube-видео через локальный ИИ (формат Obsidian)"
    )
    parser.add_argument("--url", required=True, help="Ссылка на YouTube-видео")
    parser.add_argument("--output", default="./outputs",
                        help="Папка результата (можно указать папку хранилища Obsidian)")
    parser.add_argument("--interval", type=int, default=config.FRAME_INTERVAL,
                        help="Интервал между кадрами, сек")
    parser.add_argument("--whisper-model", default=None,
                        help="Модель Whisper (base, small, ...)")
    parser.add_argument("--keep-temp", action="store_true",
                        help="Не удалять временные файлы")
    parser.add_argument("--no-concepts", action="store_true",
                        help="Не создавать заметки-заглушки для понятий")
    parser.add_argument("--no-transcript", action="store_true",
                        help="Не включать транскрипт в заметку")
    return parser.parse_args()


def run_pipeline(
    url: str,
    output_dir: Path,
    interval: int = config.FRAME_INTERVAL,
    keep_temp: bool = False,
    make_concepts: bool = True,
    include_transcript: bool = True,
) -> tuple:
    """Выполняет весь процесс; возвращает (статус, путь к результату).

    Статус: "ok" — заметка создана, "raw" — LM Studio недоступна и сохранён
    сырой текст, "error" — результата нет.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="video_conspector_"))

    try:
        logger.info("Скачивание видео...")
        downloaded = download_video(url, temp_dir)
        if downloaded is None:
            return "error", None
        video_path, info = downloaded

        logger.info("Распознавание речи...")
        transcript = transcribe_audio(video_path)

        logger.info("Извлечение кадров...")
        frames_dir = extract_frames(video_path, temp_dir, interval)

        logger.info("Распознавание кода на кадрах (OCR)...")
        ocr_text = extract_code_from_frames(frames_dir, interval)

        if not transcript and not ocr_text:
            logger.error("Не удалось получить ни транскрипт, ни текст с экрана.")
            return "error", None

        logger.info("Генерация конспекта...")
        try:
            parts = summarize_with_lm_studio(transcript, ocr_text, info)
        except Exception:
            raw_path = save_raw_text(output_dir, transcript, ocr_text)
            logger.error(
                "LM Studio недоступна. Сырой текст сохранён: %s", raw_path
            )
            return "raw", raw_path

        title, text = obsidian_note.render_note(
            parts, info, transcript, has_code=bool(ocr_text.strip()),
            include_transcript=include_transcript,
        )
        note_path = obsidian_note.note_path(output_dir, title, info)
        note_path.write_text(text + "\n", encoding="utf-8")
        if make_concepts and parts["meta"]["concepts"]:
            obsidian_note.write_concept_stubs(
                output_dir, parts["meta"]["concepts"], note_path.stem
            )
        logger.info("Готово: %s", note_path)
        return "ok", note_path
    finally:
        if keep_temp:
            logger.info("Временные файлы: %s", temp_dir)
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    """Точка входа CLI; возвращает код выхода."""
    setup_logging()
    args = parse_args()
    if args.whisper_model:
        config.WHISPER_MODEL = args.whisper_model
    status, _ = run_pipeline(
        args.url, Path(args.output), args.interval, args.keep_temp,
        make_concepts=not args.no_concepts,
        include_transcript=not args.no_transcript,
    )
    return 1 if status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
