"""Работа с LM Studio: какая модель загружена сейчас, статус, размеры запросов."""

import re
from typing import Optional

import requests

import config

AUTO = "auto"
MIN_CONTEXT = 4096          # меньше — конспект не поместится
GOOD_CONTEXT = 8192         # меньше — предупреждаем

# Модель, выбранная для текущего запуска: {"id": ..., "context": ...}
active: dict = {"id": None, "context": None}


class LMStudioError(RuntimeError):
    """LM Studio недоступна или в ней нет подходящей модели."""


class LMStudioAuthError(LMStudioError):
    """Сервер требует API-токен, а он не задан или неверен."""


def headers() -> dict:
    """Заголовки запросов: токен LM Studio, если он задан."""
    key = (config.LM_STUDIO_API_KEY or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def base_url() -> str:
    """Адрес сервера LM Studio без пути API."""
    return config.LM_STUDIO_URL.split("/v1/")[0]


def _is_chat_model(entry: dict) -> bool:
    """Отсекает эмбеддинг-модели: чат они не умеют."""
    if entry.get("type") in ("embeddings", "embedding"):
        return False
    return "embed" not in str(entry.get("id", "")).lower()


def list_models(timeout: float = 4) -> list:
    """Возвращает модели LM Studio: [{id, loaded, context, max_context}].

    `loaded` равно None, если сервер не сообщает состояние (старый API).
    Выбрасывает requests.RequestException, если сервер недоступен.
    """
    base = base_url()
    try:
        response = requests.get(f"{base}/api/v0/models", timeout=timeout,
                                headers=headers())
        if response.status_code in (401, 403):
            raise LMStudioAuthError("Нужен API-токен LM Studio")
        response.raise_for_status()
        data = response.json()["data"]
        return [
            {
                "id": m["id"],
                "loaded": m.get("state") == "loaded",
                "context": m.get("loaded_context_length"),
                "max_context": m.get("max_context_length"),
            }
            for m in data if _is_chat_model(m)
        ]
    except (requests.HTTPError, KeyError, ValueError):
        response = requests.get(f"{base}/v1/models", timeout=timeout,
                                headers=headers())
        if response.status_code in (401, 403):
            raise LMStudioAuthError("Нужен API-токен LM Studio")
        response.raise_for_status()
        return [
            {"id": m["id"], "loaded": None, "context": None, "max_context": None}
            for m in response.json()["data"] if _is_chat_model(m)
        ]


def pick_model(models: list, preferred: str = AUTO) -> Optional[dict]:
    """Выбирает модель для работы.

    Явно заданную (не auto) берём, даже если она не загружена: LM Studio
    подгрузит её при запросе. В режиме auto берём загруженную; если их
    несколько — с наибольшим контекстом.
    """
    if preferred and preferred != AUTO:
        found = next((m for m in models if m["id"] == preferred), None)
        return found or {"id": preferred, "loaded": None, "context": None,
                         "max_context": None}
    loaded = [m for m in models if m["loaded"]]
    if not loaded and any(m["loaded"] is None for m in models):
        loaded = models  # сервер не сообщает состояние — берём первую
    if not loaded:
        return None
    return max(loaded, key=lambda m: m.get("context") or 0)


def status(preferred: str = AUTO) -> dict:
    """Описывает состояние LM Studio для интерфейса.

    Ключи: level (ok/warn/bad), text, model (выбранная модель или None),
    models (все чат-модели).
    """
    try:
        models = list_models()
    except LMStudioAuthError:
        return {"level": "bad", "models": [], "model": None, "auth": True,
                "text": "LM Studio требует API-токен. Создайте его "
                        "(Developer → Server Settings) и вставьте в "
                        "настройки программы."}
    except requests.RequestException:
        return {"level": "bad", "models": [], "model": None,
                "text": "LM Studio недоступна. Запустите сервер (порт 1234)."}
    if not models:
        return {"level": "warn", "models": [], "model": None,
                "text": "В LM Studio нет моделей. Скачайте чат-модель."}
    model = pick_model(models, preferred)
    if model is None:
        names = ", ".join(m["id"] for m in models[:3])
        return {"level": "warn", "models": models, "model": None,
                "text": f"Ни одна модель не загружена (доступны: {names}). "
                        "Загрузите модель в LM Studio или выберите в списке."}
    ctx = model.get("context")
    if model.get("loaded") is False:
        return {"level": "warn", "models": models, "model": model,
                "text": f"{model['id']} не загружена — LM Studio загрузит "
                        "её при старте (первый запрос будет дольше)."}
    if ctx and ctx < MIN_CONTEXT:
        return {"level": "warn", "models": models, "model": model,
                "text": f"{model['id']}: контекст {ctx} слишком мал, "
                        f"нужно от {GOOD_CONTEXT}."}
    suffix = f" · контекст {ctx}" if ctx else ""
    level = "warn" if ctx and ctx < GOOD_CONTEXT else "ok"
    note = " (рекомендуется от 8192)" if level == "warn" else ""
    return {"level": level, "models": models, "model": model,
            "text": f"{model['id']}{suffix}{note}"}


def activate(preferred: str = AUTO) -> dict:
    """Выбирает модель для запуска и запоминает её в `active`.

    Выбрасывает LMStudioError, если подходящей модели нет.
    """
    try:
        models = list_models()
    except LMStudioError:
        raise
    except requests.RequestException as exc:
        raise LMStudioError("LM Studio недоступна") from exc
    model = pick_model(models, preferred)
    if model is None:
        raise LMStudioError("В LM Studio не загружена ни одна модель")
    active["id"] = model["id"]
    active["context"] = model.get("context") or model.get("max_context")
    return dict(active)


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


def budgets(context: Optional[int]) -> dict:
    """Размеры запросов (символы/токены) под контекст выбранной модели."""
    ctx = context or GOOD_CONTEXT
    section_tokens = _clamp(ctx // 4, 600, 1500)
    code_tokens = _clamp(ctx // 3, 800, 3500)
    meta_tokens = _clamp(ctx // 3, 800, 2000)
    return {
        "chunk_chars": _clamp((ctx - section_tokens - 900) * 2.2, 2000, 6000),
        "section_tokens": section_tokens,
        "meta_chars": _clamp((ctx - meta_tokens - 700) * 2.0, 3000, 14000),
        "meta_tokens": meta_tokens,
        "ocr_chars": _clamp((ctx - code_tokens - 800) * 2.0, 2500, 8000),
        "code_tokens": code_tokens,
    }


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Убирает блоки рассуждений (<think>...</think>) у reasoning-моделей."""
    if "</think>" in text.lower() and "<think>" not in text.lower():
        text = text.split("</think>", 1)[1]  # открывающий тег съеден шаблоном
    return _THINK_RE.sub("", text).strip()
