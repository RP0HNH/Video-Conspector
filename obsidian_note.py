"""Формирование заметки в формате Obsidian: frontmatter, callouts, wikilinks."""

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import config

_CODE_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
_LINK_RE = re.compile(r"\[\[.*?\]\]|\[[^\]]*\]\([^)]*\)")
_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]]')
_TS_RE = re.compile(r"\[(\d{2}):(\d{2}):(\d{2})\](?!\()")


def safe_filename(name: str, fallback: str = "Конспект") -> str:
    """Очищает название от символов, недопустимых в имени файла/ссылки."""
    name = _BAD_NAME_RE.sub("", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[: config.MAX_FILENAME_CHARS].strip(" .") or fallback


def video_url(info: dict, seconds: Optional[int] = None) -> str:
    """Возвращает ссылку на видео (с переходом на секунду, если задана)."""
    video_id = info.get("id")
    if not video_id:
        return info.get("webpage_url") or ""
    url = f"https://youtu.be/{video_id}"
    return f"{url}?t={int(seconds)}" if seconds else url


def format_duration(seconds: Optional[float]) -> str:
    """Преобразует секунды в строку ЧЧ:ММ:СС."""
    total = int(seconds or 0)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def linkify_timestamps(text: str, info: dict) -> str:
    """Превращает [ЧЧ:ММ:СС] в ссылки на соответствующий момент видео."""
    def replace(match: re.Match) -> str:
        hours, minutes, secs = (int(g) for g in match.groups())
        url = video_url(info, hours * 3600 + minutes * 60 + secs)
        label = match.group(0)[1:-1]
        return f"[{label}]({url})" if url else match.group(0)

    return _TS_RE.sub(replace, text)


def link_concepts(text: str, terms: list) -> str:
    """Оборачивает первое упоминание каждого понятия в [[вики-ссылку]].

    Код (блоки и inline) и заголовки не затрагиваются.
    """
    parts = _CODE_RE.split(text)  # нечётные элементы — код
    for term in sorted({t for t in terms if t.strip()}, key=len, reverse=True):
        pattern = re.compile(
            rf"(?<![\w\[|/]){re.escape(term)}(?![\w\]|/])", re.I
        )
        target = safe_filename(term, fallback=term)
        linked = False
        for i in range(0, len(parts), 2):
            lines = parts[i].split("\n")
            for j, line in enumerate(lines):
                if line.lstrip().startswith("#"):
                    continue
                protected = [m.span() for m in _LINK_RE.finditer(line)]
                for match in pattern.finditer(line):
                    if any(a < match.end() and match.start() < b
                           for a, b in protected):
                        continue
                    shown = match.group(0)
                    link = (f"[[{target}]]" if shown == target
                            else f"[[{target}|{shown}]]")
                    lines[j] = line[: match.start()] + link + line[match.end():]
                    linked = True
                    break
                if linked:
                    break
            if linked:
                parts[i] = "\n".join(lines)
                break
    return "".join(parts)


def _yaml(value) -> str:
    """Сериализует значение в YAML (JSON — подмножество YAML)."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return json.dumps(value, ensure_ascii=False)


def frontmatter(fields: dict) -> str:
    """Формирует блок свойств (frontmatter) заметки."""
    lines = ["---"]
    lines += [f"{key}: {_yaml(value)}" for key, value in fields.items()]
    lines.append("---")
    return "\n".join(lines)


def clean_tag(tag: str) -> str:
    """Приводит тег к допустимому в Obsidian виду."""
    tag = re.sub(r"[^\w/-]+", "-", str(tag).lower().strip("# "))
    return tag.strip("-_")


def _published(info: dict) -> Optional[date]:
    """Разбирает дату публикации yt-dlp (ГГГГММДД)."""
    try:
        return datetime.strptime(info.get("upload_date") or "", "%Y%m%d").date()
    except ValueError:
        return None


def _callout(kind: str, title: str, lines: list, fold: str = "") -> str:
    """Собирает callout-блок Obsidian."""
    head = f"> [!{kind}]{fold} {title}".rstrip()
    body = [f"> {line}" if line else ">" for line in lines]
    return "\n".join([head, *body])


def _mermaid(steps: list) -> str:
    """Строит mermaid-схему последовательности этапов."""
    nodes = []
    for i, step in enumerate(steps[:10], 1):
        label = re.sub(r"[\"\[\]{}()<>|`]", "", step).strip()[:60]
        nodes.append(f'S{i}["{i}. {label}"]')
    return "```mermaid\nflowchart TD\n    " + " --> ".join(nodes) + "\n```"


def render_note(parts: dict, info: dict, transcript: str, has_code: bool,
                include_transcript: bool = True) -> tuple:
    """Собирает итоговую заметку; возвращает (название, текст Markdown)."""
    meta = parts["meta"]
    title = safe_filename(
        meta.get("title") or info.get("title"), fallback="Конспект видео"
    )
    concepts = meta.get("concepts", [])
    url = video_url(info)
    published = _published(info)

    props = {
        "title": title,
        "aliases": [info["title"]] if info.get("title") else [],
        "tags": sorted({"video-note", "youtube"}
                       | {clean_tag(t) for t in meta.get("tags", []) if t}),
        "type": "video-note",
        "source": url,
        "channel": info.get("uploader") or info.get("channel") or "",
        "video_id": info.get("id") or "",
        "duration": format_duration(info.get("duration")),
        "difficulty": meta.get("difficulty") or "",
        "has_code": has_code,
        "created": date.today(),
        "status": "to-review",
    }
    if published:
        props["published"] = published

    out = [f"# {title}", ""]
    if url:
        out += [f"![]({url})", ""]

    out += [_callout("abstract", "Кратко", meta.get("tldr", "").split("\n")), ""]

    details = [f"**Видео:** [{info.get('title') or url}]({url})"]
    if props["channel"]:
        details.append(f"**Канал:** {props['channel']}")
    details.append(f"**Длительность:** {props['duration']}")
    if props["difficulty"]:
        details.append(f"**Уровень:** {props['difficulty']}")
    out += [_callout("info", "О видео", details), ""]

    if not has_code:
        out += [_callout(
            "warning", "Код на экране не обнаружен",
            ["Конспект построен только по транскрипту речи."]), ""]

    if meta.get("prerequisites"):
        out += ["## Что полезно знать заранее", ""]
        out += [f"- {item}" for item in meta["prerequisites"]] + [""]

    if concepts:
        out += ["## Ключевые понятия", "", "| Понятие | Что это |", "|---|---|"]
        for concept in concepts:
            target = safe_filename(concept["term"], fallback=concept["term"])
            link = (f"[[{target}]]" if target == concept["term"]
                    else f"[[{target}|{concept['term']}]]")
            definition = concept["definition"].replace("|", "\\|")
            out.append(f"| {link} | {definition} |")
        out.append("")

    if len(meta.get("steps", [])) >= 2:
        out += ["## Схема видео", "", _mermaid(meta["steps"]), ""]

    out += ["## Подробный конспект", ""]
    for start, heading, body in parts["sections"]:
        stamp = format_duration(start)
        out += [f"### {heading}", "",
                f"⏱ [{stamp}]({video_url(info, start)})", "", body.strip(), ""]

    if parts.get("code_md"):
        out += ["## Код с пояснениями", "", parts["code_md"].strip(), ""]

    if meta.get("questions"):
        out += ["## Вопросы для самопроверки", ""]
        for qa in meta["questions"]:
            out += [_callout("question", qa["question"],
                             qa["answer"].split("\n"), fold="-"), ""]

    if meta.get("action_items"):
        out += ["## Практика", ""]
        out += [f"- [ ] {item}" for item in meta["action_items"]] + [""]

    if concepts:
        out += ["## Связанные заметки", ""]
        out += [f"- [[{safe_filename(c['term'], fallback=c['term'])}]]"
                for c in concepts] + [""]

    if include_transcript and transcript.strip():
        out += [_callout("note", "Транскрипт", transcript.split("\n"),
                         fold="-"), ""]

    body = "\n".join(out)
    if concepts:
        body = link_concepts(body, [c["term"] for c in concepts])
    return title, frontmatter(props) + "\n\n" + linkify_timestamps(body, info)


def note_path(output_dir: Path, title: str, info: dict) -> Path:
    """Выбирает имя файла; чужую заметку с тем же названием не перезаписывает."""
    path = output_dir / f"{title}.md"
    video_id = info.get("id") or ""
    if path.exists() and video_id and video_id not in path.read_text(
            encoding="utf-8", errors="ignore"):
        path = output_dir / f"{title} ({video_id}).md"
    return path


def write_concept_stubs(output_dir: Path, concepts: list, note_title: str) -> int:
    """Создаёт заметки-заглушки для понятий или дополняет существующие."""
    folder = output_dir / config.CONCEPTS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    backlink = f"- [[{note_title}]]"
    written = 0
    for concept in concepts:
        name = safe_filename(concept["term"], fallback=concept["term"])
        path = folder / f"{name}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if backlink not in content:
                path.write_text(content.rstrip() + f"\n{backlink}\n",
                                encoding="utf-8")
            continue
        aliases = [] if name == concept["term"] else [concept["term"]]
        path.write_text(
            "\n".join([
                frontmatter({"aliases": aliases, "tags": ["concept"],
                             "type": "concept"}),
                "", f"# {name}", "", concept["definition"], "",
                "## Упоминается в", backlink, "",
            ]),
            encoding="utf-8",
        )
        written += 1
    return written
