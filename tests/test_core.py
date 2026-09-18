import importlib.util
import os
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def install_telegram_stubs():
    telegram = types.ModuleType("telegram")

    class Dummy:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    telegram.BotCommand = Dummy
    telegram.ReplyKeyboardMarkup = Dummy
    telegram.ReplyKeyboardRemove = Dummy
    telegram.Update = Dummy
    sys.modules["telegram"] = telegram

    constants = types.ModuleType("telegram.constants")
    constants.ChatAction = types.SimpleNamespace(TYPING="typing")
    constants.ParseMode = types.SimpleNamespace(HTML="HTML")
    sys.modules["telegram.constants"] = constants

    errors = types.ModuleType("telegram.error")

    class TelegramError(Exception):
        pass

    class Forbidden(TelegramError):
        pass

    class BadRequest(TelegramError):
        pass

    class RetryAfter(TelegramError):
        def __init__(self, retry_after=1):
            self.retry_after = retry_after

    errors.TelegramError = TelegramError
    errors.Forbidden = Forbidden
    errors.BadRequest = BadRequest
    errors.RetryAfter = RetryAfter
    sys.modules["telegram.error"] = errors

    ext = types.ModuleType("telegram.ext")

    class ContextTypes:
        DEFAULT_TYPE = object

    for name in [
        "Application",
        "ChatMemberHandler",
        "CommandHandler",
        "ConversationHandler",
        "MessageHandler",
    ]:
        setattr(ext, name, Dummy)
    ext.ContextTypes = ContextTypes
    ext.filters = types.SimpleNamespace(COMMAND=object(), TEXT=object())
    sys.modules["telegram.ext"] = ext


install_telegram_stubs()

spec = importlib.util.spec_from_file_location("kmc_bot_under_test", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class CurriculumTests(unittest.TestCase):
    def test_stage_totals(self):
        expected = {1: "36", 2: "41", 3: "43", 4: "47", 5: "51", 6: "48"}
        for stage_no, expected_total in expected.items():
            stage = bot.STAGES[stage_no]
            total = sum((s.credits for s in stage.subjects), Decimal("0"))
            self.assertEqual(total, Decimal(expected_total))
            self.assertEqual(stage.total_credits, Decimal(expected_total))

    def test_weights_total_100(self):
        self.assertEqual(sum(bot.STAGE_WEIGHTS.values(), Decimal("0")), Decimal("100"))
        self.assertEqual(bot.STAGE_WEIGHTS, {
            1: Decimal("5"), 2: Decimal("5"), 3: Decimal("5"),
            4: Decimal("20"), 5: Decimal("25"), 6: Decimal("40")
        })

    def test_stage1_unchanged_snapshot(self):
        snapshot = [
            ("anatomy", "4"), ("medical_physics", "3"), ("cell_gene", "3"),
            ("foundation", "2"), ("human_rights", "2"), ("med_term_1", "1"),
            ("arabic_1", "1"), ("hsd", "5"), ("biochemistry", "3"),
            ("physiology", "3"), ("micro_immunity", "3"), ("health_disease", "2"),
            ("basic_computer", "2"), ("med_term_2", "1"), ("arabic_2", "1"),
        ]
        actual = [(s.key, str(s.credits)) for s in bot.STAGE1_SUBJECTS]
        self.assertEqual(actual, snapshot)

    def test_stage5_fractional_credits(self):
        credits = {s.key: s.credits for s in bot.STAGE5_SUBJECTS}
        self.assertEqual(credits["y5_dermatology"], Decimal("3.5"))
        self.assertEqual(credits["y5_ophthalmology"], Decimal("3.5"))
        self.assertEqual(credits["y5_radiology"], Decimal("3.5"))
        self.assertEqual(credits["y5_ent"], Decimal("3.5"))


class CalculationTests(unittest.TestCase):
    def numeric_answers(self, stage_no, score):
        return [
            {
                "subject_key": s.key,
                "subject_en": s.en,
                "subject_ar": s.ar,
                "credits": str(s.credits),
                "score": str(Decimal(score)),
            }
            for s in bot.STAGES[stage_no].subjects
        ]

    def grade_answers(self, stage_no, min_score="70", max_score="79"):
        return [
            {
                "subject_key": s.key,
                "subject_en": s.en,
                "subject_ar": s.ar,
                "credits": str(s.credits),
                "grade_ar": "جيد",
                "grade_en": "Good",
                "min_score": min_score,
                "max_score": max_score,
            }
            for s in bot.STAGES[stage_no].subjects
        ]

    def test_all_100_each_stage(self):
        expected_contrib = {1: "5", 2: "5", 3: "5", 4: "20", 5: "25", 6: "40"}
        for stage_no in range(1, 7):
            result = bot.calculate_stage_result(stage_no, self.numeric_answers(stage_no, "100"), "scores")
            self.assertEqual(Decimal(result["avg"]), Decimal("100"))
            self.assertEqual(Decimal(result["contribution"]), Decimal(expected_contrib[stage_no]))

    def test_all_80_stage5(self):
        result = bot.calculate_stage_result(5, self.numeric_answers(5, "80"), "scores")
        self.assertEqual(Decimal(result["avg"]), Decimal("80"))
        self.assertEqual(Decimal(result["contribution"]), Decimal("20"))

    def test_grade_range_stage2(self):
        result = bot.calculate_stage_result(2, self.grade_answers(2), "grades")
        self.assertEqual(Decimal(result["min_avg"]), Decimal("70"))
        self.assertEqual(Decimal(result["max_avg"]), Decimal("79"))
        self.assertEqual(Decimal(result["min_contribution"]), Decimal("3.5"))
        self.assertEqual(Decimal(result["max_contribution"]), Decimal("3.95"))

    def test_weighted_single_subject_effect(self):
        # Stage 6: one 12-credit course at 100, every other course at 0.
        answers = self.numeric_answers(6, "0")
        answers[0]["score"] = "100"
        result = bot.calculate_stage_result(6, answers, "scores")
        self.assertEqual(Decimal(result["avg"]), Decimal("25"))  # 12/48*100
        self.assertEqual(Decimal(result["contribution"]), Decimal("10"))  # 25*40%

    def test_cumulative_exact_stage4(self):
        values = {1: Decimal("80"), 2: Decimal("90"), 3: Decimal("70"), 4: Decimal("60")}
        result = bot.compute_cumulative_exact(values, 4)
        self.assertEqual(result["final_contribution"], Decimal("24"))
        self.assertEqual(result["completed_weight"], Decimal("35"))
        self.assertEqual(result["current_cumulative"], Decimal("24") * Decimal("100") / Decimal("35"))

    def test_cumulative_all_80_is_80(self):
        values = {i: Decimal("80") for i in range(1, 7)}
        result = bot.compute_cumulative_exact(values, 6)
        self.assertEqual(result["current_cumulative"], Decimal("80"))
        self.assertEqual(result["final_contribution"], Decimal("80"))
        self.assertEqual(result["completed_weight"], Decimal("100"))

    def test_cumulative_range(self):
        exact = {1: Decimal("80")}
        result = bot.compute_cumulative_range(exact, 2, 2, Decimal("70"), Decimal("79"))
        self.assertEqual(result["current_min"], Decimal("75"))
        self.assertEqual(result["current_max"], Decimal("79.5"))
        self.assertEqual(result["final_min"], Decimal("7.5"))
        self.assertEqual(result["final_max"], Decimal("7.95"))

    def test_parse_score_security(self):
        for bad in ["NaN", "Infinity", "-1", "100.01", "abc", ""]:
            self.assertIsNone(bot.parse_score(bad), bad)
        self.assertEqual(bot.parse_score("89.75%"), Decimal("89.75"))
        self.assertEqual(bot.parse_score("89,75"), Decimal("89.75"))

    def test_reject_credit_mismatch(self):
        answers = self.numeric_answers(2, "80")
        answers[0]["credits"] = "999"
        with self.assertRaises(ValueError):
            bot.calculate_stage_result(2, answers, "scores")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.db = bot.Database(database_url="", sqlite_path=self.db_path)
        self.db.init_schema()

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings_and_maintenance(self):
        self.assertEqual(self.db.get_setting("maintenance_enabled"), "0")
        self.db.set_setting("maintenance_enabled", "1")
        self.assertEqual(self.db.get_setting("maintenance_enabled"), "1")

    def test_user_upsert_stage_and_ban(self):
        self.db.upsert_user(101, "A", "B", "testuser", "Student", 4)
        row = self.db.get_user(101)
        self.assertEqual(row[4], "Student")
        self.assertEqual(row[10], 4)
        self.assertTrue(self.db.ban(101))
        self.assertEqual(int(self.db.get_ban_info(101)[0]), 1)
        self.assertTrue(self.db.unban(101))
        self.assertEqual(int(self.db.get_ban_info(101)[0]), 0)

    def test_stage_result_round_trip(self):
        self.db.upsert_user(102, "A", "", "u2")
        answers = [{"credits": "41", "score": "83.25"}]
        result = {"avg": "83.25", "contribution": "4.1625"}
        self.db.save_stage_result(102, 2, "scores", result, answers)
        row = self.db.get_stage_result(102, 2)
        self.assertEqual(row[0], "scores")
        self.assertEqual(Decimal(row[1]), Decimal("83.25"))

    def test_report_content_persists_in_db(self):
        self.db.upsert_user(103, "A", "", "u3")
        self.db.record_report(103, "Student", "u3", "r.html", 3, "scores", "ok", "<html>persist</html>")
        rows = self.db.recent_reports(24)
        self.assertEqual(len(rows), 1)
        self.assertIn("persist", rows[0][9])

    def test_recipients_by_stage(self):
        self.db.upsert_user(201, "A", "", "a", current_stage=2)
        self.db.upsert_user(202, "B", "", "b", current_stage=3)
        self.db.upsert_user(203, "C", "", "c", current_stage=2)
        self.db.ban(203)
        self.assertEqual(self.db.recipient_ids("stage:2"), [201])
        self.assertEqual(set(self.db.recipient_ids("all")), {201, 202})


    def test_old_sqlite_schema_migrates(self):
        old_path = os.path.join(self.tmp.name, "old.db")
        import sqlite3
        con = sqlite3.connect(old_path)
        con.execute("CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, username TEXT, student_name TEXT, calculations INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'unknown', last_status_check TEXT, banned INTEGER DEFAULT 0, ban_reason TEXT, banned_at TEXT)")
        con.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, student_name TEXT, username TEXT, filename TEXT, path TEXT, mode TEXT, summary TEXT, created_at TEXT)")
        con.commit(); con.close()
        migrated = bot.Database(database_url="", sqlite_path=old_path)
        migrated.init_schema()
        self.assertTrue(migrated.column_exists("users", "current_stage"))
        self.assertTrue(migrated.column_exists("reports", "stage"))
        self.assertTrue(migrated.column_exists("reports", "html_content"))
        self.assertIsNotNone(migrated.get_setting("maintenance_enabled"))

    def test_broadcast_lifecycle(self):
        self.db.upsert_user(301, "A", "", "a", current_stage=2)
        bid = self.db.create_broadcast(999, "stage:2", 999, 10, 1)
        self.assertGreater(bid, 0)
        self.db.update_broadcast(bid, 1, 0, 0, "completed", finished=True)
        row = self.db.fetchone("SELECT success, failed, blocked, status, finished_at FROM broadcasts WHERE id=?", (bid,))
        self.assertEqual((row[0], row[1], row[2], row[3]), (1, 0, 0, "completed"))
        self.assertTrue(row[4])

    def test_html_report_generation(self):
        answers = []
        for subj in bot.STAGES[5].subjects:
            answers.append({"subject_key": subj.key, "subject_en": subj.en, "subject_ar": subj.ar, "credits": str(subj.credits), "score": "85"})
        result = bot.calculate_stage_result(5, answers, "scores")
        path, filename, html = bot.create_html_report("طالب اختبار", 5, answers, result, "scores", 123)
        try:
            self.assertTrue(os.path.exists(path))
            self.assertIn("Stage 5", html)
            self.assertIn("51 Cr / 25%", html)
            self.assertIn("Dermatology", html)
            self.assertNotIn("Telegram ID", html)
            self.assertTrue(filename.endswith("_Stage5.html"))
        finally:
            os.remove(path)

    def test_postgres_query_adapter(self):
        pg = bot.Database(database_url="postgresql://user:pass@example.com/db", sqlite_path=self.db_path)
        self.assertEqual(pg.backend, "postgres")
        self.assertEqual(pg._q("SELECT * FROM x WHERE a=? AND b=?"), "SELECT * FROM x WHERE a=%s AND b=%s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
