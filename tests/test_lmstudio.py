"""Тесты работы с LM Studio на поддельном сервере (без реальной модели)."""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import lmstudio  # noqa: E402
import video_conspector as vc  # noqa: E402

MODELS = [
    {"id": "text-embedding-x", "type": "embeddings", "state": "loaded"},
    {"id": "model-a", "type": "llm", "state": "not-loaded",
     "max_context_length": 32768},
    {"id": "model-b", "type": "llm", "state": "loaded",
     "loaded_context_length": 8192, "max_context_length": 32768},
]
META = {
    "title": "Тестовая заметка", "tldr": "Коротко.", "tags": ["test"],
    "difficulty": "средний", "prerequisites": [],
    "concepts": [{"term": "Термин", "definition": "Определение."}],
    "steps": ["Шаг один", "Шаг два"],
    "questions": [{"question": "Вопрос?", "answer": "Ответ."}],
    "action_items": ["Сделать"],
}


class FakeLMStudio(BaseHTTPRequestHandler):
    """Минимальная имитация API LM Studio."""

    token = "secret"
    reject_schema = False
    context_tokens = 8192
    models = MODELS
    seen_models: list = []

    def log_message(self, *_args):  # тишина в тестах
        pass

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        if self.token and self.headers.get("Authorization") != f"Bearer {self.token}":
            self._send(401, {"error": {"code": "invalid_api_key"}})
            return False
        return True

    def do_GET(self):  # noqa: N802
        if self._authorized():
            self._send(200, {"data": self.models})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        type(self).seen_models.append(body["model"])
        if body.get("response_format") and self.reject_schema:
            return self._send(400, {"error": "response_format is not supported"})
        if len(prompt) > self.context_tokens * 3:
            return self._send(400, {"error": "n_keep >= n_ctx: context overflow"})
        if "метаданные" in prompt:
            text = json.dumps(META, ensure_ascii=False)
        elif "кода с экрана" in prompt or "OCR" in prompt:
            text = "#### Блок\n\n```rust\nfn main() {}\n```\nОбъяснение."
        else:
            text = ("<think>долгие рассуждения</think>Заголовок раздела\n"
                    "- Автор показывает интерфейс\n- Параметр times=6: 6 итераций")
        self._send(200, {"choices": [{"message": {"content": text}}]})


class LMStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeLMStudio)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        port = cls.server.server_address[1]
        cls.old = (config.LM_STUDIO_URL, config.LM_STUDIO_API_KEY,
                   config.LM_STUDIO_MODEL)
        config.LM_STUDIO_URL = f"http://127.0.0.1:{port}/v1/chat/completions"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        (config.LM_STUDIO_URL, config.LM_STUDIO_API_KEY,
         config.LM_STUDIO_MODEL) = cls.old

    def setUp(self):
        config.LM_STUDIO_API_KEY = "secret"
        config.LM_STUDIO_MODEL = "auto"
        FakeLMStudio.reject_schema = False
        FakeLMStudio.context_tokens = 8192
        FakeLMStudio.seen_models = []
        lmstudio.active.update(id=None, context=None)

    def test_embeddings_are_not_offered(self):
        ids = [m["id"] for m in lmstudio.list_models()]
        self.assertNotIn("text-embedding-x", ids)

    def test_auto_picks_loaded_model(self):
        status = lmstudio.status()
        self.assertEqual(status["model"]["id"], "model-b")
        self.assertEqual(status["level"], "ok")
        self.assertIn("8192", status["text"])

    def test_explicit_model_wins(self):
        self.assertEqual(lmstudio.pick_model(lmstudio.list_models(), "model-a")["id"],
                         "model-a")

    def test_missing_token_is_reported(self):
        config.LM_STUDIO_API_KEY = ""
        status = lmstudio.status()
        self.assertEqual(status["level"], "bad")
        self.assertTrue(status.get("auth"))
        with self.assertRaises(lmstudio.LMStudioAuthError):
            lmstudio.activate()

    def test_no_loaded_model(self):
        old = FakeLMStudio.models
        FakeLMStudio.models = [MODELS[1]]
        try:
            self.assertIsNone(lmstudio.status()["model"])
            with self.assertRaises(lmstudio.LMStudioError):
                lmstudio.activate()
        finally:
            FakeLMStudio.models = old

    def test_summary_uses_loaded_model_and_cleans_answers(self):
        transcript = "\n".join(f"[00:00:{i:02d}] текст {i}" for i in range(30))
        parts = vc.summarize_with_lm_studio(transcript, "--- [00:00:05] ---\nfn main() {}",
                                            {"title": "Видео"})
        self.assertEqual(set(FakeLMStudio.seen_models), {"model-b"})
        heading, body = parts["sections"][0][1:]
        self.assertEqual(heading, "Заголовок раздела")
        self.assertNotIn("think", body)
        self.assertNotIn("Автор показывает", body)
        self.assertIn("times=6", body)
        self.assertEqual(parts["meta"]["title"], "Тестовая заметка")
        self.assertIn("```rust", parts["code_md"])

    def test_schema_rejected_falls_back(self):
        FakeLMStudio.reject_schema = True
        parts = vc.summarize_with_lm_studio("[00:00:01] текст", "", {"title": "В"})
        self.assertEqual(parts["meta"]["title"], "Тестовая заметка")

    def test_context_overflow_shrinks_prompt(self):
        FakeLMStudio.context_tokens = 400  # ~1200 символов
        answer = vc._chat("слово " * 2000, 100)
        self.assertIn("Заголовок раздела", answer)

    def test_budgets_follow_context(self):
        small, large = lmstudio.budgets(4096), lmstudio.budgets(32768)
        self.assertLess(small["chunk_chars"], large["chunk_chars"])
        self.assertLessEqual(large["chunk_chars"], 6000)


if __name__ == "__main__":
    unittest.main()
