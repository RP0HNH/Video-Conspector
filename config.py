"""Константы и настройки Video Conspector."""

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# В собранном exe настройки (.env) лежат рядом с исполняемым файлом
BASE_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
load_dotenv(BASE_DIR / ".env")

# LM Studio
LM_STUDIO_URL = os.getenv(
    "LM_STUDIO_URL", "http://localhost:1234/v1/chat/completions"
)
# "auto" — использовать модель, которая сейчас загружена в LM Studio
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "auto")
# Токен нужен, если в LM Studio включена авторизация сервера
LM_STUDIO_API_KEY = os.getenv("LM_STUDIO_API_KEY", "")
LM_STUDIO_TIMEOUT = int(os.getenv("LM_STUDIO_TIMEOUT", "900"))
LM_STUDIO_TEMPERATURE = 0.2
# Сколько раз сокращать запрос вдвое, если контекст модели переполнен
LM_STUDIO_MAX_RETRIES = 4

# Whisper
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE") or None
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
# VAD (onnxruntime) отсекает тишину; отключите, если процесс падает
WHISPER_VAD = os.getenv("WHISPER_VAD", "0") == "1"

# Видео и кадры
MAX_VIDEO_HEIGHT = 1080
FRAME_INTERVAL = int(os.getenv("FRAME_INTERVAL", "5"))

# OCR
_DEFAULT_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
TESSERACT_CMD = os.getenv("TESSERACT_CMD") or (
    str(_DEFAULT_TESSERACT) if _DEFAULT_TESSERACT.exists() else None
)
OCR_LANGS = "eng+rus"
# psm 6 — единый блок текста, хорошо подходит для кода
OCR_CONFIG = "--oem 3 --psm 6"
OCR_MIN_CONF = 60              # минимальная уверенность Tesseract для слова
OCR_MIN_GOOD_RATIO = 0.75      # доля букв/цифр/символов кода в строке
OCR_MIN_CODE_LINES = 3         # минимум «кодовых» строк на кадре
OCR_MIN_CODE_RATIO = 0.3       # доля «кодовых» строк среди всех на кадре
OCR_MIN_NEW_LINES = 4          # минимум новых строк, чтобы кадр попал в результат
OCR_LINE_SIMILARITY = 0.85     # порог «такая же строка уже была»
CODE_SYMBOLS = "{}()[]<>;:=+-*/\\&|!?.,_#@$%^~\"'`"
CODE_LINE_RE = re.compile(
    r"[{}();=<>\[\]]|->|=>|::|[:,]\s*$|\b(?:def|fn|let|mut|class|import|return|"
    r"func|var|const|pub|impl|struct|enum)\b"
)

# Заметка Obsidian
SUMMARY_FILENAME = "summary.md"   # имя, если название определить не удалось
RAW_FILENAME = "raw_text.md"
CONCEPTS_DIRNAME = "Понятия"      # папка заглушек-заметок для [[понятий]]
MAX_FILENAME_CHARS = 80

NO_CODE_PLACEHOLDER = "(код на экране не обнаружен)"

SECTION_PROMPT = """Ты составляешь сжатый технический конспект. Ниже фрагмент \
транскрипта видео «{video_title}» (часть {index} из {total}; таймкоды \
[ЧЧ:ММ:СС]). Пиши по-русски.

Правила:
- Первая строка — заголовок раздела до 8 слов, без символа #.
- Дальше только факты списком. Одна строка — одно конкретное утверждение: \
определение, параметр и его значение, число, название инструмента или \
функции, команда, причина, вывод, сравнение.
- Запрещено: пересказ хода видео («автор показывает / рассказывает / \
объясняет / демонстрирует»), вступления, выводы «в итоге», оценки, описание \
каждого действия, повторы, приветствия, реклама, спонсоры, просьбы поставить \
лайк. Такие места пропускай целиком.
- Нет конкретного значения, имени или причины — пункт не пиши.
- Термин при первом упоминании поясняй одной короткой фразой.
- Сравнение вариантов — таблицей. Не более двух callout на раздел: \
`> [!important]` для главного, `> [!warning]` для типичной ошибки.
- К ключевым пунктам добавляй таймкод [ЧЧ:ММ:СС] из фрагмента.
- Объём 80–220 слов. Без заголовков # внутри. Ничего не выдумывай.

Фрагмент:
{chunk}"""

META_PROMPT = """Ниже конспект видео «{video_title}». Заполни метаданные \
заметки для Obsidian. Пиши по-русски, только факты, без воды и общих \
формулировок; термины и теги можно латиницей.
- title: название по сути содержания, 3–8 слов, без двоеточий и спецсимволов \
(не копируй кликбейтное название ролика);
- tldr: 2–3 предложения — тема и главный результат или вывод, без вводных слов;
- tags: 3–7 тегов в нижнем регистре, без пробелов и #, слова через дефис;
- difficulty: начальный, средний или продвинутый;
- prerequisites: что нужно знать заранее (0–4 пункта);
- concepts: 5–10 ключевых понятий (term — название точно как в тексте \
конспекта; definition — одно короткое предложение);
- steps: 4–8 ключевых этапов по порядку, до 8 слов каждый;
- questions: 3–5 вопросов для самопроверки с короткими ответами;
- action_items: 2–4 конкретных практических задания.

Конспект:
{notes}"""

CODE_PROMPT = """Ниже текст кода с экрана видео «{video_title}», распознанный \
OCR с ошибками (в нём бывают обрывки строк, мусор от интерфейса и повторы).
Задача:
1. Полностью удали мусор интерфейса редактора и приложения (подсказки клавиш \
вроде «tap: R to reset seed», «file picker», статус-строки с путями файлов, \
номера строк, проценты, часы) и повторы. В блок кода должен попасть только код.
2. Сгруппируй код в логические блоки. Очевидные OCR-ошибки исправляй \
аккуратно, не добавляя того, чего нет на экране; обрезанный фрагмент оставь \
как есть.
3. Для каждого блока выведи: заголовок `#### Название блока`, блок кода с \
указанием языка (```язык), затем объяснение для новичка: что делает блок, за \
что отвечают ключевые параметры и строки (списком), как он связан с остальным. \
Если у блока есть таймкод вида `--- [ЧЧ:ММ:СС] ---`, добавь строку \
«Показано на [ЧЧ:ММ:СС]».
Код бери только из текста ниже, ничего не выдумывай.

Текст:
{ocr}"""

META_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "tldr": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "difficulty": {"type": "string"},
        "prerequisites": {"type": "array", "items": {"type": "string"}},
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                },
                "required": ["term", "definition"],
            },
        },
        "steps": {"type": "array", "items": {"type": "string"}},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                },
                "required": ["question", "answer"],
            },
        },
        "action_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title", "tldr", "tags", "difficulty", "prerequisites", "concepts",
        "steps", "questions", "action_items",
    ],
}
