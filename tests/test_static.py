import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StaticTests(unittest.TestCase):
    def test_python_files_parse(self):
        for name in ["bot.py", "keep_alive.py"]:
            ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)

    def test_no_literal_bot_token(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        # Typical Telegram token shape: digits:long_alnum_secret
        self.assertIsNone(re.search(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b", text))

    def test_render_is_web_service(self):
        text = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("type: web", text)
        self.assertIn("startCommand: python bot.py", text)
        self.assertIn("healthCheckPath: /health", text)
        self.assertIn("PYTHON_VERSION", text)

    def test_requirements_no_old_python_bidi_problem(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("python-bidi", text)
        self.assertNotIn("reportlab", text)
        self.assertIn("python-telegram-bot==22.8", text)
        self.assertIn("psycopg[binary]", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
