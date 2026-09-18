import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, getcontext
from html import escape
from typing import Dict, Iterable, List, Optional, Set, Tuple

from keep_alive import start_health_server
from telegram import BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# Keep enough precision for weighted-credit calculations (including 3.5 credits).
getcontext().prec = 28

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("kmc_grade_bot")


# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
(
    MAIN,
    ASK_NAME,
    SELECT_STAGE,
    MODE,
    COLLECT,
    MATERIALS_STAGE,
    ASK_CUMULATIVE,
    CUMULATIVE_STAGE,
    CUMULATIVE_INPUT,
    ASK_BAN,
    ASK_UNBAN,
    BROADCAST_TARGET,
    BROADCAST_CONTENT,
    BROADCAST_CONFIRM,
    MAINTENANCE_CONFIRM,
) = range(15)


# ---------------------------------------------------------------------------
# Project metadata
# ---------------------------------------------------------------------------
BOT_TITLE = "KMC | Grade Calculator"
REPORT_TITLE = "KMC GRADE CALCULATOR"
COLLEGE_NAME = "Al-Kindy College of Medicine"
DEVELOPER_NAME = "Osama"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
IRAQ_TZ = ZoneInfo("Asia/Baghdad") if ZoneInfo else timezone(timedelta(hours=3))

# Stage weights used for six-year graduation GPA/rank calculation.
STAGE_WEIGHTS: Dict[int, Decimal] = {
    1: Decimal("5"),
    2: Decimal("5"),
    3: Decimal("5"),
    4: Decimal("20"),
    5: Decimal("25"),
    6: Decimal("40"),
}


def iraq_now() -> datetime:
    return datetime.now(IRAQ_TZ).replace(tzinfo=None)


def now_str() -> str:
    return iraq_now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Curriculum
# IMPORTANT: Stage 1 is intentionally preserved exactly from the user's
# original bot: same 15 subjects, same credits, total 36, weight 5%.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Subject:
    key: str
    en: str
    ar: str
    credits: Decimal


@dataclass(frozen=True)
class StageConfig:
    number: int
    en_name: str
    ar_name: str
    total_credits: Decimal
    weight_percent: Decimal
    subjects: Tuple[Subject, ...]


def S(key: str, en: str, ar: str, credits: str) -> Subject:
    return Subject(key, en, ar, Decimal(credits))


# Stage 1: copied from the original bot without changing its subjects/credits.
STAGE1_SUBJECTS: Tuple[Subject, ...] = (
    S("anatomy", "Human Anatomy", "التشريح البشري", "4"),
    S("medical_physics", "Medical Physics", "الفيزياء الطبية", "3"),
    S("cell_gene", "Human Cell & Gene", "الخلية والموروثة الجينية", "3"),
    S("foundation", "Foundation of Medicine", "أساسيات الطب", "2"),
    S("human_rights", "Human Rights", "حقوق الإنسان", "2"),
    S("med_term_1", "Medical Terminology 1", "المصطلحات الطبية 1", "1"),
    S("arabic_1", "Arabic Language 1", "اللغة العربية 1", "1"),
    S("hsd", "Human Structure & Development", "التركيب والنشوء البشري", "5"),
    S("biochemistry", "Biochemistry", "الكيمياء الحياتية", "3"),
    S("physiology", "Physiology", "الفسلجة", "3"),
    S("micro_immunity", "Microbiology & Immunity", "الأحياء المجهرية والمناعة", "3"),
    S("health_disease", "Concept of Health & Disease", "مفاهيم الصحة والمرض", "2"),
    S("basic_computer", "Basic Computer", "أساسيات الحاسوب", "2"),
    S("med_term_2", "Medical Terminology 2", "المصطلحات الطبية 2", "1"),
    S("arabic_2", "Arabic Language 2", "اللغة العربية 2", "1"),
)

STAGE2_SUBJECTS: Tuple[Subject, ...] = (
    S("y2_idt", "Introduction to Disease & Therapy", "مقدمة في المرض والعلاج", "3"),
    S("y2_metabolism", "Metabolism", "الأيض", "2"),
    S("y2_hemopoietic", "Hemopoietic & Lymphatic", "الدم والجهاز اللمفاوي", "5"),
    S("y2_msk", "Musculoskeletal", "الجهاز العضلي الهيكلي", "4"),
    S("y2_ece1", "Early Clinical Exposure & Ethics I", "التعرض السريري المبكر والأخلاقيات 1", "2"),
    S("y2_adv_computer", "Advanced Computer", "الحاسوب المتقدم", "2"),
    S("y2_elective_hd_civil", "Elective: Human Development & Civil Defense", "اختياري: التنمية البشرية والدفاع المدني", "2"),
    S("y2_endocrine", "Endocrine", "الغدد الصماء", "5"),
    S("y2_cardiovascular", "Cardiovascular", "القلب والأوعية الدموية", "5"),
    S("y2_respiratory", "Respiratory", "الجهاز التنفسي", "5"),
    S("y2_ece2", "Early Clinical Exposure & Ethics II", "التعرض السريري المبكر والأخلاقيات 2", "2"),
    S("y2_adv_english", "Elective: Advanced English", "اختياري: اللغة الإنكليزية المتقدمة", "2"),
    S("y2_elective_behavior", "Elective: Behavioral Regulation & Thinking Technique", "اختياري: تنظيم السلوك وتقنيات التفكير", "2"),
)

STAGE3_SUBJECTS: Tuple[Subject, ...] = (
    S("y3_mhe", "Measuring Health Events", "قياس الأحداث الصحية", "2"),
    S("y3_neurosciences", "Neurosciences", "علوم الأعصاب", "8"),
    S("y3_integumentary", "Integumentary", "الجهاز اللحافي", "2"),
    S("y3_reproductive", "Reproductive", "الجهاز التناسلي", "4"),
    S("y3_ece1", "Early Clinical Exposure & Ethics I", "التعرض السريري المبكر والأخلاقيات 1", "3"),
    S("y3_research1", "Research Project I", "مشروع البحث 1", "2"),
    S("y3_preventive", "Preventive Medicine", "الطب الوقائي", "6"),
    S("y3_git", "GIT, Liver, Biliary & Pancreas", "الجهاز الهضمي والكبد والطرق الصفراوية والبنكرياس", "6"),
    S("y3_renal", "Renal", "الجهاز الكلوي", "4"),
    S("y3_ece2", "Early Clinical Exposure & Ethics II", "التعرض السريري المبكر والأخلاقيات 2", "2"),
    S("y3_research2", "Research Project II", "مشروع البحث 2", "2"),
    S("y3_elective_english3", "Elective: English Language III", "اختياري: اللغة الإنكليزية 3", "2"),
)

STAGE4_SUBJECTS: Tuple[Subject, ...] = (
    S("y4_internal_medicine", "Internal Medicine", "الطب الباطني", "12"),
    S("y4_primary_healthcare", "Primary Healthcare", "الرعاية الصحية الأولية", "6"),
    S("y4_forensic_pathology", "Forensic Medicine & Clinical Pathology", "الطب العدلي وعلم الأمراض السريري", "5"),
    S("y4_general_surgery", "General Surgery", "الجراحة العامة", "12"),
    S("y4_obstetrics", "Obstetrics", "التوليد", "9"),
    S("y4_elective_communication", "Elective: Advanced Communication Skills in English", "اختياري: مهارات التواصل المتقدمة باللغة الإنكليزية", "3"),
)

STAGE5_SUBJECTS: Tuple[Subject, ...] = (
    S("y5_medicine_subspecialties", "Medicine Subspecialties", "اختصاصات الطب الباطني الفرعية", "8"),
    S("y5_psychiatry", "Psychiatry", "الطب النفسي", "4"),
    S("y5_dermatology", "Dermatology", "الأمراض الجلدية", "3.5"),
    S("y5_ophthalmology", "Ophthalmology", "طب العيون", "3.5"),
    S("y5_pediatrics", "Pediatrics", "طب الأطفال", "7"),
    S("y5_surgery_subspecialties", "Surgery Subspecialties", "اختصاصات الجراحة الفرعية", "5"),
    S("y5_orthopedics", "Orthopedics", "جراحة العظام", "6"),
    S("y5_radiology", "Radiology", "الأشعة", "3.5"),
    S("y5_ent", "ENT", "الأنف والأذن والحنجرة", "3.5"),
    S("y5_gynecology", "Gynecology", "النسائية", "7"),
)

STAGE6_SUBJECTS: Tuple[Subject, ...] = (
    S("y6_internal_medicine", "Internal Medicine", "الطب الباطني", "12"),
    S("y6_surgery", "Surgery", "الجراحة", "12"),
    S("y6_obgyn", "Obstetrics & Gynecology", "التوليد والنسائية", "11"),
    S("y6_pediatrics", "Pediatrics", "طب الأطفال", "11"),
    S("y6_family_medicine", "Family Medicine", "طب الأسرة", "2"),
)

STAGES: Dict[int, StageConfig] = {
    1: StageConfig(1, "First Year", "المرحلة الأولى", Decimal("36"), STAGE_WEIGHTS[1], STAGE1_SUBJECTS),
    2: StageConfig(2, "Second Year", "المرحلة الثانية", Decimal("41"), STAGE_WEIGHTS[2], STAGE2_SUBJECTS),
    3: StageConfig(3, "Third Year", "المرحلة الثالثة", Decimal("43"), STAGE_WEIGHTS[3], STAGE3_SUBJECTS),
    4: StageConfig(4, "Fourth Year", "المرحلة الرابعة", Decimal("47"), STAGE_WEIGHTS[4], STAGE4_SUBJECTS),
    5: StageConfig(5, "Fifth Year", "المرحلة الخامسة", Decimal("51"), STAGE_WEIGHTS[5], STAGE5_SUBJECTS),
    6: StageConfig(6, "Sixth Year", "المرحلة السادسة", Decimal("48"), STAGE_WEIGHTS[6], STAGE6_SUBJECTS),
}

# Original Stage-1 categories are preserved. Grade-mode is necessarily an estimate.
GRADES: Dict[str, Tuple[str, Decimal, Decimal]] = {
    "امتياز": ("Excellent", Decimal("90"), Decimal("100")),
    "جيد جدًا": ("Very Good", Decimal("80"), Decimal("89")),
    "جيد جدا": ("Very Good", Decimal("80"), Decimal("89")),
    "جيد": ("Good", Decimal("70"), Decimal("79")),
    "متوسط": ("Fair", Decimal("60"), Decimal("69")),
    "مقبول": ("Pass", Decimal("50"), Decimal("59")),
    "ضعيف": ("Weak", Decimal("0"), Decimal("49")),
    "راسب": ("Weak", Decimal("0"), Decimal("49")),
}


def validate_curriculum() -> None:
    expected = {
        1: Decimal("36"),
        2: Decimal("41"),
        3: Decimal("43"),
        4: Decimal("47"),
        5: Decimal("51"),
        6: Decimal("48"),
    }
    seen_keys: Set[str] = set()
    for stage_no, stage in STAGES.items():
        calculated = sum((s.credits for s in stage.subjects), Decimal("0"))
        if calculated != expected[stage_no] or stage.total_credits != expected[stage_no]:
            raise RuntimeError(
                f"Curriculum validation failed for stage {stage_no}: "
                f"subjects={calculated}, configured={stage.total_credits}, expected={expected[stage_no]}"
            )
        for subject in stage.subjects:
            if subject.credits <= 0:
                raise RuntimeError(f"Invalid credits for {subject.key}")
            if subject.key in seen_keys:
                raise RuntimeError(f"Duplicate subject key: {subject.key}")
            seen_keys.add(subject.key)
    if sum(STAGE_WEIGHTS.values(), Decimal("0")) != Decimal("100"):
        raise RuntimeError("Stage weights must total 100%")


validate_curriculum()


# ---------------------------------------------------------------------------
# Formatting / parsing helpers
# ---------------------------------------------------------------------------
def d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def parse_score(text: str) -> Optional[Decimal]:
    raw = (text or "").strip().replace("%", "").replace("٫", ".").replace(",", ".")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0 or value > 100:
        return None
    return value


def fmt(value: Decimal, places: int = 2) -> str:
    value = d(value)
    quantum = Decimal("1") if places == 0 else Decimal("1." + ("0" * places))
    return f"{value.quantize(quantum):.{places}f}"


def fmt_credit(value: Decimal) -> str:
    value = d(value)
    if value == value.to_integral():
        return str(int(value))
    return format(value.normalize(), "f")


def safe_filename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_\-\u0600-\u06FF ]+", "", name or "").strip().replace(" ", "_")
    clean = clean[:50].strip("_")
    return clean if clean else "student"


def html_escape_text(value) -> str:
    return escape(str(value or ""), quote=True)


def logo_data_uri() -> str:
    if not os.path.exists(LOGO_PATH):
        return ""
    try:
        with open(LOGO_PATH, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return ""


def get_admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    result: Set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            result.add(int(part))
    return result


def is_admin_user(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in get_admin_ids())


def stage_from_button(text: str) -> Optional[int]:
    mapping = {
        "1️⃣ المرحلة الأولى": 1,
        "2️⃣ المرحلة الثانية": 2,
        "3️⃣ المرحلة الثالثة": 3,
        "4️⃣ المرحلة الرابعة": 4,
        "5️⃣ المرحلة الخامسة": 5,
        "6️⃣ المرحلة السادسة": 6,
    }
    return mapping.get((text or "").strip())


def stage_keyboard(include_stage1: bool = True, cancel: bool = True) -> ReplyKeyboardMarkup:
    rows: List[List[str]] = []
    if include_stage1:
        rows.append(["1️⃣ المرحلة الأولى", "2️⃣ المرحلة الثانية"])
    else:
        rows.append(["2️⃣ المرحلة الثانية", "3️⃣ المرحلة الثالثة"])
    if include_stage1:
        rows.append(["3️⃣ المرحلة الثالثة", "4️⃣ المرحلة الرابعة"])
    else:
        rows.append(["4️⃣ المرحلة الرابعة", "5️⃣ المرحلة الخامسة"])
    if include_stage1:
        rows.append(["5️⃣ المرحلة الخامسة", "6️⃣ المرحلة السادسة"])
    else:
        rows.append(["6️⃣ المرحلة السادسة"])
    if cancel:
        rows.append(["❌ إلغاء"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


MODE_KEYBOARD = ReplyKeyboardMarkup(
    [["📊 حساب بالتقديرات"], ["🔢 حساب بالدرجات الرقمية"], ["❌ إلغاء"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

GRADE_KEYBOARD = ReplyKeyboardMarkup(
    [["امتياز", "جيد جدًا"], ["جيد", "متوسط"], ["مقبول", "ضعيف"], ["❌ إلغاء"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

CUMULATIVE_OFFER_KEYBOARD = ReplyKeyboardMarkup(
    [["✅ نعم، احسب التراكمي"], ["❌ لا، رجوع للقائمة"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# ---------------------------------------------------------------------------
# Database layer: PostgreSQL when DATABASE_URL is set, SQLite fallback.
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, database_url: Optional[str] = None, sqlite_path: Optional[str] = None):
        self.database_url = (database_url if database_url is not None else os.getenv("DATABASE_URL", "")).strip()
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        self.backend = "postgres" if self.database_url else "sqlite"
        data_dir = os.getenv("BOT_DATA_DIR", os.path.join(os.getcwd(), "bot_data"))
        os.makedirs(data_dir, exist_ok=True)
        self.sqlite_path = sqlite_path or os.getenv("USERS_DB_PATH", os.path.join(data_dir, "users.db"))

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.backend == "postgres" else sql

    @contextmanager
    def connection(self):
        if self.backend == "postgres":
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed") from exc
            con = psycopg.connect(self.database_url, connect_timeout=10)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(self.sqlite_path)), exist_ok=True)
            con = sqlite3.connect(self.sqlite_path, timeout=30)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def execute(self, sql: str, params: Iterable = ()) -> None:
        with self.connection() as con:
            cur = con.cursor()
            try:
                cur.execute(self._q(sql), tuple(params))
            finally:
                cur.close()

    def fetchone(self, sql: str, params: Iterable = ()):
        with self.connection() as con:
            cur = con.cursor()
            try:
                cur.execute(self._q(sql), tuple(params))
                return cur.fetchone()
            finally:
                cur.close()

    def fetchall(self, sql: str, params: Iterable = ()):
        with self.connection() as con:
            cur = con.cursor()
            try:
                cur.execute(self._q(sql), tuple(params))
                return cur.fetchall()
            finally:
                cur.close()

    def column_exists(self, table: str, column: str) -> bool:
        if self.backend == "sqlite":
            rows = self.fetchall(f"PRAGMA table_info({table})")
            return any(str(row[1]) == column for row in rows)
        row = self.fetchone(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=? AND column_name=?",
            (table, column),
        )
        return bool(row)

    def ensure_column(self, table: str, column: str, ddl_type: str) -> None:
        if not self.column_exists(table, column):
            self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    def init_schema(self) -> None:
        report_pk = "BIGSERIAL PRIMARY KEY" if self.backend == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        history_pk = report_pk
        broadcast_pk = report_pk
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                student_name TEXT,
                calculations INTEGER DEFAULT 0,
                first_seen TEXT,
                last_seen TEXT,
                status TEXT DEFAULT 'unknown',
                last_status_check TEXT,
                banned INTEGER DEFAULT 0,
                ban_reason TEXT,
                banned_at TEXT,
                current_stage INTEGER
            )
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS reports (
                id {report_pk},
                telegram_id BIGINT,
                student_name TEXT,
                username TEXT,
                filename TEXT,
                path TEXT,
                stage INTEGER,
                mode TEXT,
                summary TEXT,
                html_content TEXT,
                created_at TEXT
            )
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS stage_results (
                id {history_pk},
                telegram_id BIGINT NOT NULL,
                stage INTEGER NOT NULL,
                mode TEXT NOT NULL,
                avg TEXT,
                min_avg TEXT,
                mid_avg TEXT,
                max_avg TEXT,
                contribution TEXT,
                min_contribution TEXT,
                mid_contribution TEXT,
                max_contribution TEXT,
                answers_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        self.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_stage_results_user_stage ON stage_results(telegram_id, stage)"
        )
        self.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id {broadcast_pk},
                admin_id BIGINT,
                target TEXT,
                source_chat_id BIGINT,
                source_message_id BIGINT,
                total INTEGER DEFAULT 0,
                success INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                blocked INTEGER DEFAULT 0,
                status TEXT,
                created_at TEXT,
                finished_at TEXT
            )
            """
        )
        self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS cumulative_history (
                id {history_pk},
                telegram_id BIGINT,
                target_stage INTEGER,
                result_type TEXT,
                current_min TEXT,
                current_max TEXT,
                final_contribution_min TEXT,
                final_contribution_max TEXT,
                values_json TEXT,
                created_at TEXT
            )
            """
        )

        # Migrate old SQLite / PostgreSQL installations created by the original bot.
        for table, column, ddl_type in [
            ("users", "current_stage", "INTEGER"),
            ("reports", "stage", "INTEGER"),
            ("reports", "html_content", "TEXT"),
            ("reports", "path", "TEXT"),
        ]:
            self.ensure_column(table, column, ddl_type)

        if self.get_setting("maintenance_enabled") is None:
            self.set_setting("maintenance_enabled", "0")
        if self.get_setting("maintenance_message") is None:
            self.set_setting("maintenance_message", "البوت متوقف مؤقتًا للصيانة. حاول مرة أخرى لاحقًا.")

    def upsert_user(
        self,
        telegram_id: int,
        first_name: str,
        last_name: str,
        username: str,
        student_name: str = "",
        current_stage: Optional[int] = None,
    ) -> None:
        ts = now_str()
        self.execute(
            """
            INSERT INTO users (
                telegram_id, first_name, last_name, username, student_name,
                calculations, first_seen, last_seen, status, banned, current_stage
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'unknown', 0, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                student_name=CASE WHEN excluded.student_name <> '' THEN excluded.student_name ELSE users.student_name END,
                last_seen=excluded.last_seen,
                current_stage=COALESCE(excluded.current_stage, users.current_stage)
            """,
            (telegram_id, first_name, last_name, username, student_name, ts, ts, current_stage),
        )

    def get_user(self, telegram_id: int):
        return self.fetchone(
            """
            SELECT telegram_id, first_name, last_name, username, student_name,
                   calculations, status, banned, ban_reason, banned_at, current_stage
            FROM users WHERE telegram_id=?
            """,
            (telegram_id,),
        )

    def set_student_name(self, telegram_id: int, student_name: str) -> None:
        self.execute("UPDATE users SET student_name=?, last_seen=? WHERE telegram_id=?", (student_name, now_str(), telegram_id))

    def clear_student_name(self, telegram_id: int) -> None:
        self.execute("UPDATE users SET student_name=NULL, last_seen=? WHERE telegram_id=?", (now_str(), telegram_id))

    def set_current_stage(self, telegram_id: int, stage: int) -> None:
        self.execute("UPDATE users SET current_stage=?, last_seen=? WHERE telegram_id=?", (stage, now_str(), telegram_id))

    def increment_calculation(self, telegram_id: int) -> None:
        self.execute(
            "UPDATE users SET calculations=COALESCE(calculations,0)+1, last_seen=? WHERE telegram_id=?",
            (now_str(), telegram_id),
        )

    def get_ban_info(self, telegram_id: int):
        return self.fetchone("SELECT banned, ban_reason, banned_at FROM users WHERE telegram_id=?", (telegram_id,))

    def set_status(self, telegram_id: int, status: str, auto_ban: bool = False) -> None:
        if auto_ban:
            self.execute(
                """
                UPDATE users SET status=?, last_status_check=?, banned=1,
                    ban_reason=COALESCE(NULLIF(ban_reason,''),'blocked_bot'),
                    banned_at=COALESCE(banned_at, ?)
                WHERE telegram_id=?
                """,
                (status, now_str(), now_str(), telegram_id),
            )
        else:
            self.execute(
                "UPDATE users SET status=?, last_status_check=? WHERE telegram_id=?",
                (status, now_str(), telegram_id),
            )

    def ban(self, telegram_id: int, reason: str = "manual_admin") -> bool:
        if not self.fetchone("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)):
            return False
        self.execute(
            """
            UPDATE users SET banned=1, status='banned', ban_reason=?, banned_at=?, last_status_check=?
            WHERE telegram_id=?
            """,
            (reason, now_str(), now_str(), telegram_id),
        )
        return True

    def unban(self, telegram_id: int) -> bool:
        if not self.fetchone("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)):
            return False
        self.execute(
            """
            UPDATE users SET banned=0, status='restored', ban_reason=NULL, banned_at=NULL, last_status_check=?
            WHERE telegram_id=?
            """,
            (now_str(), telegram_id),
        )
        return True

    def find_user(self, identifier: str):
        ident = (identifier or "").strip()
        clean = ident[1:] if ident.startswith("@") else ident
        if clean.isdigit():
            row = self.fetchone(
                "SELECT telegram_id, first_name, last_name, username, student_name, current_stage FROM users WHERE telegram_id=?",
                (int(clean),),
            )
            if row:
                return row
        return self.fetchone(
            "SELECT telegram_id, first_name, last_name, username, student_name, current_stage FROM users WHERE lower(username)=lower(?)",
            (clean,),
        )

    def all_users(self):
        return self.fetchall(
            """
            SELECT telegram_id, first_name, last_name, username, student_name, calculations,
                   first_seen, last_seen, status, banned, current_stage
            FROM users ORDER BY last_seen DESC
            """
        )

    def banned_users(self):
        return self.fetchall(
            """
            SELECT telegram_id, first_name, last_name, username, student_name, ban_reason, banned_at, status, last_seen
            FROM users WHERE banned=1 ORDER BY COALESCE(banned_at,last_seen) DESC
            """
        )

    def stats(self) -> dict:
        total = self.fetchone("SELECT COUNT(*), COALESCE(SUM(calculations),0), COALESCE(SUM(CASE WHEN banned=1 THEN 1 ELSE 0 END),0) FROM users")
        reports = self.fetchone("SELECT COUNT(*) FROM reports")
        stage_results = self.fetchone("SELECT COUNT(*) FROM stage_results")
        by_stage = self.fetchall("SELECT current_stage, COUNT(*) FROM users WHERE current_stage IS NOT NULL GROUP BY current_stage ORDER BY current_stage")
        return {
            "users": int(total[0] or 0),
            "calculations": int(total[1] or 0),
            "banned": int(total[2] or 0),
            "reports": int(reports[0] or 0),
            "stage_results": int(stage_results[0] or 0),
            "by_stage": {int(row[0]): int(row[1]) for row in by_stage if row[0] is not None},
        }

    def save_stage_result(self, telegram_id: int, stage: int, mode: str, result: dict, answers: List[dict]) -> None:
        ts = now_str()
        payload = json.dumps(answers, ensure_ascii=False)
        self.execute(
            """
            INSERT INTO stage_results (
                telegram_id, stage, mode, avg, min_avg, mid_avg, max_avg,
                contribution, min_contribution, mid_contribution, max_contribution,
                answers_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id, stage) DO UPDATE SET
                mode=excluded.mode,
                avg=excluded.avg,
                min_avg=excluded.min_avg,
                mid_avg=excluded.mid_avg,
                max_avg=excluded.max_avg,
                contribution=excluded.contribution,
                min_contribution=excluded.min_contribution,
                mid_contribution=excluded.mid_contribution,
                max_contribution=excluded.max_contribution,
                answers_json=excluded.answers_json,
                updated_at=excluded.updated_at
            """,
            (
                telegram_id,
                stage,
                mode,
                result.get("avg"),
                result.get("min_avg"),
                result.get("mid_avg"),
                result.get("max_avg"),
                result.get("contribution"),
                result.get("min_contribution"),
                result.get("mid_contribution"),
                result.get("max_contribution"),
                payload,
                ts,
                ts,
            ),
        )

    def get_stage_result(self, telegram_id: int, stage: int):
        return self.fetchone(
            """
            SELECT mode, avg, min_avg, mid_avg, max_avg, contribution,
                   min_contribution, mid_contribution, max_contribution, updated_at
            FROM stage_results WHERE telegram_id=? AND stage=?
            """,
            (telegram_id, stage),
        )

    def record_report(
        self,
        telegram_id: int,
        student_name: str,
        username: str,
        filename: str,
        stage: int,
        mode: str,
        summary: str,
        html_content: str,
        path: str = "",
    ) -> None:
        self.execute(
            """
            INSERT INTO reports (telegram_id, student_name, username, filename, path, stage, mode, summary, html_content, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, student_name, username, filename, path, stage, mode, summary, html_content, now_str()),
        )

    def recent_reports(self, hours: int = 24):
        cutoff = (iraq_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        return self.fetchall(
            """
            SELECT id, telegram_id, student_name, username, filename, path, stage, mode, summary, html_content, created_at
            FROM reports WHERE created_at>=? ORDER BY created_at DESC
            """,
            (cutoff,),
        )

    def get_setting(self, key: str) -> Optional[str]:
        row = self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return None if not row else row[0]

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_str()),
        )

    def recipient_ids(self, target: str) -> List[int]:
        if target == "all":
            rows = self.fetchall("SELECT telegram_id FROM users WHERE COALESCE(banned,0)=0")
        elif target.startswith("stage:"):
            stage = int(target.split(":", 1)[1])
            rows = self.fetchall(
                "SELECT telegram_id FROM users WHERE COALESCE(banned,0)=0 AND current_stage=?",
                (stage,),
            )
        else:
            return []
        admins = get_admin_ids()
        return [int(r[0]) for r in rows if int(r[0]) not in admins]

    def create_broadcast(self, admin_id: int, target: str, source_chat_id: int, source_message_id: int, total: int) -> int:
        sql = """
            INSERT INTO broadcasts (admin_id, target, source_chat_id, source_message_id, total, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?)
        """
        with self.connection() as con:
            cur = con.cursor()
            try:
                if self.backend == "postgres":
                    cur.execute(self._q(sql) + " RETURNING id", (admin_id, target, source_chat_id, source_message_id, total, now_str()))
                    bid = int(cur.fetchone()[0])
                else:
                    cur.execute(sql, (admin_id, target, source_chat_id, source_message_id, total, now_str()))
                    bid = int(cur.lastrowid)
                return bid
            finally:
                cur.close()

    def update_broadcast(self, broadcast_id: int, success: int, failed: int, blocked: int, status: str, finished: bool = False) -> None:
        self.execute(
            """
            UPDATE broadcasts SET success=?, failed=?, blocked=?, status=?, finished_at=? WHERE id=?
            """,
            (success, failed, blocked, status, now_str() if finished else None, broadcast_id),
        )

    def record_cumulative(
        self,
        telegram_id: int,
        target_stage: int,
        result_type: str,
        current_min: Decimal,
        current_max: Decimal,
        final_min: Decimal,
        final_max: Decimal,
        values: dict,
    ) -> None:
        self.execute(
            """
            INSERT INTO cumulative_history (
                telegram_id, target_stage, result_type, current_min, current_max,
                final_contribution_min, final_contribution_max, values_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                target_stage,
                result_type,
                str(current_min),
                str(current_max),
                str(final_min),
                str(final_max),
                json.dumps(values, ensure_ascii=False),
                now_str(),
            ),
        )


DB = Database()


# ---------------------------------------------------------------------------
# Core calculations (pure functions, covered by tests)
# ---------------------------------------------------------------------------
def calculate_stage_result(stage_no: int, answers: List[dict], mode: str) -> dict:
    stage = STAGES[stage_no]
    if len(answers) != len(stage.subjects):
        raise ValueError("Incomplete answers")

    credits_sum = sum((d(item["credits"]) for item in answers), Decimal("0"))
    if credits_sum != stage.total_credits:
        raise ValueError(f"Credit mismatch: got {credits_sum}, expected {stage.total_credits}")

    if mode == "grades":
        min_total = sum((d(item["min_score"]) * d(item["credits"]) for item in answers), Decimal("0"))
        max_total = sum((d(item["max_score"]) * d(item["credits"]) for item in answers), Decimal("0"))
        min_avg = min_total / stage.total_credits
        max_avg = max_total / stage.total_credits
        mid_avg = (min_avg + max_avg) / Decimal("2")
        return {
            "min_avg": str(min_avg),
            "mid_avg": str(mid_avg),
            "max_avg": str(max_avg),
            "min_contribution": str(min_avg * stage.weight_percent / Decimal("100")),
            "mid_contribution": str(mid_avg * stage.weight_percent / Decimal("100")),
            "max_contribution": str(max_avg * stage.weight_percent / Decimal("100")),
        }

    if mode != "scores":
        raise ValueError("Unknown mode")
    total = sum((d(item["score"]) * d(item["credits"]) for item in answers), Decimal("0"))
    avg = total / stage.total_credits
    return {
        "avg": str(avg),
        "contribution": str(avg * stage.weight_percent / Decimal("100")),
    }


def compute_cumulative_exact(values: Dict[int, Decimal], target_stage: int) -> dict:
    expected = set(range(1, target_stage + 1))
    if set(values.keys()) != expected:
        raise ValueError(f"Cumulative values must include stages 1..{target_stage}")
    total_weight = sum((STAGE_WEIGHTS[s] for s in expected), Decimal("0"))
    weighted_numerator = sum((d(values[s]) * STAGE_WEIGHTS[s] for s in expected), Decimal("0"))
    current_cumulative = weighted_numerator / total_weight
    final_contribution = weighted_numerator / Decimal("100")
    return {
        "current_cumulative": current_cumulative,
        "final_contribution": final_contribution,
        "completed_weight": total_weight,
    }


def compute_cumulative_range(
    exact_values: Dict[int, Decimal],
    target_stage: int,
    range_stage: int,
    min_value: Decimal,
    max_value: Decimal,
) -> dict:
    if range_stage < 1 or range_stage > target_stage:
        raise ValueError("range_stage out of bounds")
    needed_exact = set(range(1, target_stage + 1)) - {range_stage}
    if set(exact_values.keys()) != needed_exact:
        raise ValueError("Missing exact stage values")
    total_weight = sum((STAGE_WEIGHTS[s] for s in range(1, target_stage + 1)), Decimal("0"))
    base = sum((d(exact_values[s]) * STAGE_WEIGHTS[s] for s in needed_exact), Decimal("0"))
    low_num = base + d(min_value) * STAGE_WEIGHTS[range_stage]
    high_num = base + d(max_value) * STAGE_WEIGHTS[range_stage]
    return {
        "current_min": low_num / total_weight,
        "current_max": high_num / total_weight,
        "final_min": low_num / Decimal("100"),
        "final_max": high_num / Decimal("100"),
        "completed_weight": total_weight,
    }


# ---------------------------------------------------------------------------
# Keyboards / session helpers
# ---------------------------------------------------------------------------
MAIN_ROWS = [
    ["🧮 حساب المعدل", "📈 حساب التراكمي"],
    ["📝 إضافة/تغيير الاسم", "📚 عرض المواد"],
    ["ℹ️ المساعدة", "🔄 إعادة البداية"],
]


def maintenance_enabled() -> bool:
    return DB.get_setting("maintenance_enabled") == "1"


def admin_keyboard() -> ReplyKeyboardMarkup:
    toggle = "▶️ تشغيل البوت" if maintenance_enabled() else "⏸ إيقاف البوت"
    return ReplyKeyboardMarkup(
        [
            ["📊 الإحصائية الكاملة"],
            ["👥 قائمة المستخدمين الكاملة"],
            ["📁 ملفات آخر 24 ساعة"],
            ["🔎 فحص حالة المستخدمين"],
            ["📢 إرسال إعلان"],
            [toggle],
            ["🚫 حظر مستخدم", "✅ رفع حظر"],
            ["📄 قائمة المحظورين"],
            ["🔙 رجوع"],
        ],
        resize_keyboard=True,
    )


def main_keyboard_for(update: Update) -> ReplyKeyboardMarkup:
    rows = [row[:] for row in MAIN_ROWS]
    if update.effective_user and is_admin_user(update.effective_user.id):
        rows.append(["🛠 لوحة الأدمن"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def clear_calc(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in [
        "mode",
        "index",
        "answers",
        "selected_stage",
        "current_result",
        "current_mode",
        "cum_target_stage",
        "cum_values",
        "cum_needed",
        "cum_needed_index",
        "cum_range_stage",
        "cum_range_min",
        "cum_range_max",
    ]:
        context.user_data.pop(key, None)


def clear_admin_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ["broadcast_target", "broadcast_chat_id", "broadcast_message_id", "maintenance_action"]:
        context.user_data.pop(key, None)


def sync_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    DB.upsert_user(
        user.id,
        user.first_name or "",
        user.last_name or "",
        user.username or "",
        context.user_data.get("student_name", "") or "",
        context.user_data.get("selected_stage"),
    )
    row = DB.get_user(user.id)
    if row and not context.user_data.get("student_name") and row[4]:
        context.user_data["student_name"] = row[4]


def is_user_banned(user_id: Optional[int]) -> bool:
    if not user_id or is_admin_user(user_id):
        return False
    row = DB.get_ban_info(user_id)
    return bool(row and int(row[0] or 0) == 1)


def banned_message() -> str:
    return (
        "<b>🚫 الوصول إلى البوت موقوف لهذا الحساب.</b>\n\n"
        "إذا كنت تعتقد أن هذا حصل بالخطأ، تواصل مع إدارة البوت لإعادة التفعيل."
    )


async def deny_if_unavailable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    sync_user(update, context)
    if is_user_banned(user.id):
        if update.effective_message:
            await update.effective_message.reply_text(banned_message(), parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        return True
    if maintenance_enabled() and not is_admin_user(user.id):
        msg = DB.get_setting("maintenance_message") or "البوت متوقف مؤقتًا للصيانة."
        if update.effective_message:
            await update.effective_message.reply_text(
                f"<b>🔧 {escape(msg)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=ReplyKeyboardRemove(),
            )
        return True
    return False


# ---------------------------------------------------------------------------
# Reports / exports
# ---------------------------------------------------------------------------
HTML_NAVY = "#0D1F44"
HTML_BLUE = "#2F5DA8"
HTML_SOFT = "#F5F8FD"
HTML_ROW = "#EAF0F8"
HTML_LINE = "#D9E2F0"
HTML_TEXT = "#111827"
HTML_MUTED = "#5D6678"


def build_html_subject_rows(stage: StageConfig, answers: List[dict], mode: str) -> str:
    rows: List[str] = []
    for item in answers:
        credits = d(item["credits"])
        if mode == "grades":
            cmin = d(item["min_score"]) * credits / stage.total_credits * stage.weight_percent / Decimal("100")
            cmax = d(item["max_score"]) * credits / stage.total_credits * stage.weight_percent / Decimal("100")
            values = [
                item["subject_en"],
                fmt_credit(credits),
                item["grade_en"],
                f"{item['min_score']}-{item['max_score']}",
                f"{fmt(cmin)}% - {fmt(cmax)}%",
            ]
        else:
            contrib = d(item["score"]) * credits / stage.total_credits * stage.weight_percent / Decimal("100")
            values = [item["subject_en"], fmt_credit(credits), fmt(d(item["score"])), f"{fmt(contrib)}%"]
        rows.append("<tr>" + "".join(f"<td>{html_escape_text(v)}</td>" for v in values) + "</tr>")
    return "\n".join(rows)


def create_html_report(
    student_name: str,
    stage_no: int,
    answers: List[dict],
    result: dict,
    mode: str,
    telegram_id: int,
) -> Tuple[str, str, str]:
    stage = STAGES[stage_no]
    send_filename = f"{safe_filename(student_name)}_Stage{stage_no}.html"
    report_date = iraq_now().strftime("%Y-%m-%d %H:%M Iraq")
    logo_uri = logo_data_uri()
    logo_html = f'<img class="logo" src="{logo_uri}" alt="KMC Logo">' if logo_uri else ""

    if mode == "grades":
        result_cells = f"""
        <tr><th>Metric</th><th>Minimum</th><th>Middle</th><th>Maximum</th></tr>
        <tr><td>Stage average</td><td>{fmt(d(result['min_avg']))}%</td><td>{fmt(d(result['mid_avg']))}%</td><td>{fmt(d(result['max_avg']))}%</td></tr>
        <tr><td>Graduation contribution</td><td>{fmt(d(result['min_contribution']))}%</td><td>{fmt(d(result['mid_contribution']))}%</td><td>{fmt(d(result['max_contribution']))}%</td></tr>
        """
        subject_headers = "<tr><th>Subject</th><th>Cr</th><th>Grade</th><th>Range</th><th>Impact</th></tr>"
        note = "Grade categories produce an estimated range. For an exact result, use numeric scores."
    else:
        result_cells = f"""
        <tr><th>Metric</th><th>Result</th></tr>
        <tr><td>Stage average</td><td>{fmt(d(result['avg']))}%</td></tr>
        <tr><td>Graduation contribution</td><td>{fmt(d(result['contribution']))}%</td></tr>
        """
        subject_headers = "<tr><th>Subject</th><th>Cr</th><th>Score</th><th>Impact</th></tr>"
        note = "Numeric scores were used. Credits weight subjects inside the stage; the stage weight is applied afterward."

    rows_html = build_html_subject_rows(stage, answers, mode)
    student_name_html = html_escape_text(student_name)
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KMC Grade Report - {student_name_html}</title>
<style>
  :root {{ --navy:{HTML_NAVY}; --blue:{HTML_BLUE}; --soft:{HTML_SOFT}; --row:{HTML_ROW}; --line:{HTML_LINE}; --text:{HTML_TEXT}; --muted:{HTML_MUTED}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:#eef2f7; color:var(--text); font-family:Tahoma,Arial,"Noto Naskh Arabic","Noto Sans Arabic",sans-serif; font-weight:700; }}
  .page {{ width:210mm; min-height:297mm; margin:18px auto; background:white; border:2px solid var(--navy); border-radius:8px; padding:10mm 9mm 8mm; position:relative; overflow:hidden; }}
  .header {{ border:1.3px solid var(--line); border-radius:10px; padding:10px 14px 9px; display:grid; grid-template-columns:1fr 118px; align-items:center; gap:12px; border-bottom:5px solid var(--blue); }}
  h1 {{ margin:0; color:var(--navy); font-size:27px; line-height:1.05; }}
  .subtitle {{ margin-top:6px; color:var(--navy); font-size:14px; line-height:1.35; }}
  .logo {{ max-width:115px; max-height:78px; object-fit:contain; justify-self:end; }}
  .info {{ margin-top:10px; border:1.3px solid var(--line); border-radius:10px; overflow:hidden; display:grid; grid-template-columns:1fr 1fr; }}
  .info div {{ padding:9px 12px; border-bottom:1px solid var(--line); min-height:46px; }}
  .info div:nth-child(odd) {{ border-right:1px solid var(--line); }}
  .label {{ color:var(--muted); font-size:11.5px; display:block; margin-bottom:4px; }}
  .value {{ color:var(--navy); font-size:15px; line-height:1.25; }}
  .section-row {{ margin:12px 0 6px; display:flex; align-items:flex-end; justify-content:space-between; gap:10px; }}
  .section-title {{ margin:0; color:var(--navy); font-size:20px; }}
  .note {{ margin:0; color:var(--muted); font-size:10.5px; line-height:1.35; text-align:right; max-width:72%; }}
  table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
  th {{ background:var(--navy); color:white; padding:7px 6px; font-size:11px; text-align:center; line-height:1.1; }}
  td {{ padding:6px; border:1px solid white; font-size:10.5px; text-align:center; line-height:1.15; }}
  td:first-child, th:first-child {{ text-align:left; }}
  tbody tr:nth-child(even) td {{ background:var(--row); }}
  tbody tr:nth-child(odd) td {{ background:#fbfdff; }}
  .result-table td {{ font-size:13px; padding:8px 6px; }}
  .details-title {{ margin:12px 0 6px; color:var(--navy); font-size:16px; }}
  .footer {{ position:absolute; left:9mm; right:9mm; bottom:5.5mm; color:var(--muted); font-size:9.2px; text-align:center; border-top:1px solid var(--line); padding-top:6px; }}
  .print-button {{ position:fixed; right:18px; bottom:18px; background:var(--navy); color:white; padding:12px 16px; border-radius:999px; text-decoration:none; font:700 14px Arial; box-shadow:0 6px 22px rgba(0,0,0,.20); }}
  @media print {{ body {{ background:white; }} .page {{ margin:0; border-radius:0; box-shadow:none; }} .print-button {{ display:none; }} }}
  @page {{ size:A4; margin:0; }}
</style>
</head>
<body>
<a class="print-button" href="javascript:window.print()">Print / Save PDF</a>
<section class="page">
  <div class="header"><div><h1>{REPORT_TITLE}</h1><div class="subtitle">Stage {stage_no} Grade Report | {COLLEGE_NAME}<br>Developed by {DEVELOPER_NAME}</div></div>{logo_html}</div>
  <div class="info">
    <div><span class="label">Student Name</span><span class="value"><bdi dir="auto">{student_name_html}</bdi></span></div>
    <div><span class="label">Report Date</span><span class="value">{report_date}</span></div>
    <div><span class="label">Stage</span><span class="value">{html_escape_text(stage.en_name)}</span></div>
    <div><span class="label">Credits / Graduation Weight</span><span class="value">{fmt_credit(stage.total_credits)} Cr / {fmt_credit(stage.weight_percent)}%</span></div>
  </div>
  <div class="section-row"><h2 class="section-title">Final Result</h2><p class="note">{html_escape_text(note)}</p></div>
  <table class="result-table"><tbody>{result_cells.strip()}</tbody></table>
  <h2 class="details-title">Subject Details</h2>
  <table><thead>{subject_headers}</thead><tbody>{rows_html}</tbody></table>
  <div class="footer">Stage average = weighted by subject credits. Graduation contribution = stage average × stage weight. This is a calculator report, not an official transcript.</div>
</section>
</body>
</html>"""

    fd, path = tempfile.mkstemp(prefix=f"kmc_stage{stage_no}_", suffix=".html")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path, send_filename, html


def build_users_txt() -> str:
    rows = DB.all_users()
    fd, path = tempfile.mkstemp(prefix="kmc_users_", suffix=".txt")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(f"{BOT_TITLE} - Users\nGenerated: {now_str()}\nTotal users: {len(rows)}\n")
        f.write("=" * 90 + "\n\n")
        for i, row in enumerate(rows, start=1):
            tid, first, last, username, student_name, calculations, first_seen, last_seen, status, banned, stage = row
            f.write(f"{i}. ID: {tid}\n")
            f.write(f"   Telegram: {(first or '')} {(last or '')}\n")
            f.write(f"   Username: @{username if username else 'none'}\n")
            f.write(f"   Student: {student_name or 'none'}\n")
            f.write(f"   Stage: {stage or 'unknown'}\n")
            f.write(f"   Calculations: {calculations or 0}\n")
            f.write(f"   Status: {status or 'unknown'} | Banned: {banned or 0}\n")
            f.write(f"   First: {first_seen or ''} | Last: {last_seen or ''}\n\n")
    return path


def build_banned_users_txt() -> Optional[str]:
    rows = DB.banned_users()
    if not rows:
        return None
    fd, path = tempfile.mkstemp(prefix="kmc_banned_", suffix=".txt")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(f"{BOT_TITLE} - Banned Users\nGenerated: {now_str()}\nTotal: {len(rows)}\n")
        f.write("=" * 90 + "\n\n")
        for i, row in enumerate(rows, start=1):
            tid, first, last, username, student_name, reason, banned_at, status, last_seen = row
            f.write(f"{i}. ID: {tid}\n")
            f.write(f"   Name: {student_name or ((first or '') + ' ' + (last or '')).strip()}\n")
            f.write(f"   Username: @{username if username else 'none'}\n")
            f.write(f"   Reason: {reason or ''}\n")
            f.write(f"   Banned at: {banned_at or ''} | Status: {status or ''} | Last seen: {last_seen or ''}\n\n")
    return path


def build_reports_zip_24h() -> Optional[str]:
    rows = DB.recent_reports(24)
    if not rows:
        return None
    fd, zip_path = tempfile.mkstemp(prefix="kmc_reports_24h_", suffix=".zip")
    os.close(fd)
    summary_lines = [f"{BOT_TITLE} - Reports last 24h", f"Generated: {now_str()}", f"Count: {len(rows)}", ""]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, row in enumerate(rows, start=1):
            rid, tid, student_name, username, filename, path, stage, mode, summary, html_content, created_at = row
            ext = ".html"
            archive_name = f"{idx:03d}_stage{stage or 0}_{safe_filename(student_name or str(tid))}{ext}"
            content = html_content
            if not content and path and os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                except Exception:
                    content = None
            if content:
                zf.writestr(archive_name, content)
            summary_lines.append(
                f"{idx}. {student_name or ''} | @{username if username else 'none'} | ID {tid} | Stage {stage} | {mode} | {created_at} | {summary or ''}"
            )
        zf.writestr("reports_summary.txt", "\n".join(summary_lines))
    return zip_path


# ---------------------------------------------------------------------------
# Telegram handlers: public
# ---------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("calculate", "حساب معدل مرحلة"),
        BotCommand("cumulative", "حساب التراكمي"),
        BotCommand("list", "عرض مواد المراحل"),
        BotCommand("help", "شرح طريقة الحساب"),
        BotCommand("about", "عن البوت"),
        BotCommand("reset", "إعادة البداية"),
    ]
    await application.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    clear_calc(context)
    text = (
        f"<b>👋 أهلًا بك في {escape(BOT_TITLE)}</b>\n\n"
        "حاسبة درجات ومعدلات لطلاب كلية طب الكندي، من المرحلة الأولى إلى السادسة.\n\n"
        "• المرحلة الأولى محفوظة بنفس مواد وكردتات النسخة الأصلية.\n"
        "• المراحل الثانية إلى السادسة محسوبة حسب الكردتات المضافة للنظام.\n"
        "• يدعم حساب معدل المرحلة والتراكمي حتى المرحلة الحالية.\n\n"
        "اختر من القائمة بالأسفل."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    lines = [
        "<b>ℹ️ طريقة الحساب</b>",
        "",
        "<b>معدل المرحلة:</b>",
        "مجموع (درجة المادة × Credits المادة) ÷ مجموع Credits المرحلة.",
        "",
        "<b>وزن المراحل في التخرج:</b>",
        "الأولى 5% • الثانية 5% • الثالثة 5% • الرابعة 20% • الخامسة 25% • السادسة 40%.",
        "",
        "<b>الحساب الرقمي:</b> أدق خيار لأنك تدخل الدرجة نفسها.",
        "<b>حساب التقديرات:</b> تقريبي ويعطي مدى لأن كل تقدير يمثل مجموعة درجات.",
        "",
        "<b>التراكمي حتى مرحلة معينة:</b>",
        "يجمع معدلات السنوات بأوزانها، ثم يعيد موازنة السنوات المنجزة إلى 100% لعرض معدلك التراكمي الحالي، ويعرض أيضًا مقدار مساهمتك في معدل التخرج النهائي.",
    ]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (
        "<b>📌 عن البوت</b>\n\n"
        "• حساب معدلات المراحل 1-6 حسب Credits.\n"
        "• حساب التراكمي المرحلي ومساهمة كل مرحلة في معدل التخرج.\n"
        "• تقارير HTML باسم الطالب.\n"
        "• تخزين النتائج في قاعدة بيانات خارجية PostgreSQL عند ربط DATABASE_URL.\n\n"
        f"<b>Developer:</b> {escape(DEVELOPER_NAME)}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    clear_calc(context)
    clear_admin_flow(context)
    if update.effective_user:
        DB.clear_student_name(update.effective_user.id)
    context.user_data.pop("student_name", None)
    await update.effective_message.reply_text(
        "<b>تمت إعادة البداية وحذف الاسم المحفوظ من حسابك.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard_for(update),
    )
    return MAIN


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    await update.effective_message.reply_text(
        "<b>اكتب اسمك الثلاثي الذي تريد ظهوره داخل التقرير.</b>\n\nيمكن كتابة الاسم بالعربي أو الإنكليزي.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    name = (update.message.text or "").strip()
    if name.startswith("/"):
        await update.message.reply_text("<b>اكتب الاسم كنص، وليس أمرًا.</b>", parse_mode=ParseMode.HTML)
        return ASK_NAME
    if len(name) < 2 or len(name) > 80:
        await update.message.reply_text("<b>اكتب اسمًا واضحًا من 2 إلى 80 حرفًا.</b>", parse_mode=ParseMode.HTML)
        return ASK_NAME
    context.user_data["student_name"] = name
    if update.effective_user:
        DB.set_student_name(update.effective_user.id, name)
    await update.message.reply_text(
        f"<b>تم حفظ الاسم:</b> {escape(name)} ✅",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard_for(update),
    )
    return MAIN


async def begin_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    clear_calc(context)
    if not context.user_data.get("student_name"):
        await update.effective_message.reply_text(
            "<b>قبل الحساب، أضف اسم الطالب حتى يظهر داخل التقرير.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard_for(update),
        )
        return MAIN
    await update.effective_message.reply_text(
        "<b>🎓 اختر المرحلة التي تريد حساب معدلها:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=stage_keyboard(include_stage1=True),
    )
    return SELECT_STAGE


async def choose_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم إلغاء الحساب.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    stage_no = stage_from_button(text)
    if not stage_no:
        await update.message.reply_text("<b>اختر مرحلة من الأزرار فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=stage_keyboard())
        return SELECT_STAGE
    context.user_data["selected_stage"] = stage_no
    if update.effective_user:
        DB.set_current_stage(update.effective_user.id, stage_no)
    stage = STAGES[stage_no]
    await update.message.reply_text(
        f"<b>{escape(stage.ar_name)}</b>\n"
        f"Credits: <b>{fmt_credit(stage.total_credits)}</b>\n"
        f"وزن المرحلة في معدل التخرج: <b>{fmt_credit(stage.weight_percent)}%</b>\n\n"
        "<b>اختر طريقة الحساب:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=MODE_KEYBOARD,
    )
    return MODE


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم إلغاء الحساب.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    if text == "📊 حساب بالتقديرات":
        context.user_data["mode"] = "grades"
    elif text == "🔢 حساب بالدرجات الرقمية":
        context.user_data["mode"] = "scores"
    else:
        await update.message.reply_text("<b>اختر من الأزرار فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=MODE_KEYBOARD)
        return MODE
    context.user_data["index"] = 0
    context.user_data["answers"] = []
    return await ask_current_subject(update, context)


async def ask_current_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stage_no = int(context.user_data["selected_stage"])
    stage = STAGES[stage_no]
    idx = int(context.user_data["index"])
    subject = stage.subjects[idx]
    mode = context.user_data["mode"]
    text = (
        f"<b>{escape(stage.ar_name)} — المادة {idx + 1} من {len(stage.subjects)}</b>\n\n"
        f"<b>{escape(subject.en)}</b>\n"
        f"{escape(subject.ar)}\n"
        f"<b>Credits:</b> {fmt_credit(subject.credits)}\n\n"
    )
    if mode == "grades":
        text += "<b>اختر التقدير:</b>"
        markup = GRADE_KEYBOARD
    else:
        text += "<b>اكتب الدرجة الرقمية من 0 إلى 100.</b>"
        markup = ReplyKeyboardMarkup([["❌ إلغاء"]], resize_keyboard=True)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return COLLECT


async def collect_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم إلغاء الحساب.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN

    stage_no = int(context.user_data.get("selected_stage", 0))
    stage = STAGES.get(stage_no)
    if not stage:
        clear_calc(context)
        await update.message.reply_text("<b>انتهت الجلسة. ابدأ الحساب من جديد.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    idx = int(context.user_data.get("index", 0))
    if idx < 0 or idx >= len(stage.subjects):
        clear_calc(context)
        await update.message.reply_text("<b>صار خلل في ترتيب المواد. ابدأ من جديد.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    mode = context.user_data.get("mode")
    subject = stage.subjects[idx]

    if mode == "grades":
        if text not in GRADES:
            await update.message.reply_text("<b>اختر تقديرًا من الأزرار فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=GRADE_KEYBOARD)
            return COLLECT
        label_en, min_score, max_score = GRADES[text]
        context.user_data["answers"].append(
            {
                "subject_key": subject.key,
                "subject_en": subject.en,
                "subject_ar": subject.ar,
                "credits": str(subject.credits),
                "grade_ar": text,
                "grade_en": label_en,
                "min_score": str(min_score),
                "max_score": str(max_score),
            }
        )
    elif mode == "scores":
        score = parse_score(text)
        if score is None:
            await update.message.reply_text("<b>اكتب رقمًا صالحًا من 0 إلى 100.</b>", parse_mode=ParseMode.HTML)
            return COLLECT
        context.user_data["answers"].append(
            {
                "subject_key": subject.key,
                "subject_en": subject.en,
                "subject_ar": subject.ar,
                "credits": str(subject.credits),
                "score": str(score),
            }
        )
    else:
        clear_calc(context)
        await update.message.reply_text("<b>انتهت الجلسة. ابدأ من جديد.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN

    context.user_data["index"] = idx + 1
    if context.user_data["index"] >= len(stage.subjects):
        return await finish_calculation(update, context)
    return await ask_current_subject(update, context)


def build_summary_text(stage_no: int, result: dict, mode: str) -> str:
    stage = STAGES[stage_no]
    if mode == "grades":
        return (
            "<b>✅ تم حساب النتيجة التقريبية</b>\n\n"
            f"<b>{escape(stage.ar_name)}:</b> {fmt(d(result['min_avg']))}% - {fmt(d(result['max_avg']))}%\n"
            f"<b>المعدل الوسطي التقريبي:</b> {fmt(d(result['mid_avg']))}%\n"
            f"<b>مساهمة المرحلة في معدل التخرج:</b> {fmt(d(result['min_contribution']))}% - {fmt(d(result['max_contribution']))}%\n\n"
            "<b>مهم:</b> حساب التقديرات تقريبي. استخدم الدرجات الرقمية للحصول على نتيجة أدق."
        )
    return (
        "<b>✅ تم حساب النتيجة بالدرجات الرقمية</b>\n\n"
        f"<b>{escape(stage.ar_name)}:</b> {fmt(d(result['avg']))}%\n"
        f"<b>مساهمة المرحلة في معدل التخرج:</b> {fmt(d(result['contribution']))}%\n"
        f"<b>وزن المرحلة:</b> {fmt_credit(stage.weight_percent)}%"
    )


async def finish_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stage_no = int(context.user_data["selected_stage"])
    answers = list(context.user_data["answers"])
    mode = str(context.user_data["mode"])
    student_name = context.user_data.get("student_name", "student")
    result = calculate_stage_result(stage_no, answers, mode)
    user = update.effective_user
    if not user:
        clear_calc(context)
        return MAIN

    path, send_filename, html_content = create_html_report(student_name, stage_no, answers, result, mode, user.id)
    DB.increment_calculation(user.id)
    DB.save_stage_result(user.id, stage_no, mode, result, answers)
    summary_plain = f"Stage {stage_no} | {mode}"
    DB.record_report(
        user.id,
        student_name,
        user.username or "",
        send_filename,
        stage_no,
        mode,
        summary_plain,
        html_content,
        path,
    )

    summary = build_summary_text(stage_no, result, mode)
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML)
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=send_filename,
                caption="<b>تقريرك جاهز كملف HTML ✅</b>\nافتحه بالمتصفح، ويمكنك استخدام Print / Save PDF.",
                parse_mode=ParseMode.HTML,
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    context.user_data["current_result"] = result
    context.user_data["current_mode"] = mode
    if stage_no >= 2:
        await update.message.reply_text(
            f"<b>📈 تريد أحسب لك التراكمي من المرحلة الأولى إلى {escape(STAGES[stage_no].ar_name)}؟</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=CUMULATIVE_OFFER_KEYBOARD,
        )
        return ASK_CUMULATIVE

    clear_calc(context)
    await update.message.reply_text("<b>تمت العملية ✅</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def cumulative_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ لا، رجوع للقائمة":
        clear_calc(context)
        await update.message.reply_text("<b>تم ✅</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    if text != "✅ نعم، احسب التراكمي":
        await update.message.reply_text("<b>اختر نعم أو لا من الأزرار.</b>", parse_mode=ParseMode.HTML, reply_markup=CUMULATIVE_OFFER_KEYBOARD)
        return ASK_CUMULATIVE
    target = int(context.user_data["selected_stage"])
    current_result = context.user_data.get("current_result")
    current_mode = context.user_data.get("current_mode")
    return await prepare_cumulative(update, context, target, current_result, current_mode)


async def begin_cumulative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    clear_calc(context)
    await update.effective_message.reply_text(
        "<b>📈 اختر أعلى مرحلة تريد حساب التراكمي حتى نهايتها:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=stage_keyboard(include_stage1=False),
    )
    return CUMULATIVE_STAGE


async def choose_cumulative_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم الإلغاء.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    stage_no = stage_from_button(text)
    if not stage_no or stage_no < 2:
        await update.message.reply_text("<b>اختر مرحلة من الثانية إلى السادسة.</b>", parse_mode=ParseMode.HTML, reply_markup=stage_keyboard(include_stage1=False))
        return CUMULATIVE_STAGE
    return await prepare_cumulative(update, context, stage_no)


async def prepare_cumulative(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_stage: int,
    current_result: Optional[dict] = None,
    current_mode: Optional[str] = None,
) -> int:
    user = update.effective_user
    if not user:
        return MAIN
    values: Dict[int, str] = {}
    needed: List[int] = []
    range_stage: Optional[int] = None
    range_min: Optional[str] = None
    range_max: Optional[str] = None

    for stage_no in range(1, target_stage + 1):
        if stage_no == target_stage and current_result is not None:
            if current_mode == "scores":
                values[stage_no] = str(current_result["avg"])
            elif current_mode == "grades":
                range_stage = stage_no
                range_min = str(current_result["min_avg"])
                range_max = str(current_result["max_avg"])
            continue

        saved = DB.get_stage_result(user.id, stage_no)
        if saved and saved[0] == "scores" and saved[1] is not None:
            values[stage_no] = str(saved[1])
        else:
            needed.append(stage_no)

    context.user_data["cum_target_stage"] = target_stage
    context.user_data["cum_values"] = {str(k): v for k, v in values.items()}
    context.user_data["cum_needed"] = needed
    context.user_data["cum_needed_index"] = 0
    if range_stage is not None:
        context.user_data["cum_range_stage"] = range_stage
        context.user_data["cum_range_min"] = range_min
        context.user_data["cum_range_max"] = range_max

    if needed:
        used = sorted(values.keys())
        note = ""
        if used:
            note = "\n\n✅ سأستخدم النتائج الرقمية المحفوظة للمراحل: " + ", ".join(str(x) for x in used)
        stage_no = needed[0]
        await update.effective_message.reply_text(
            f"<b>أدخل المعدل السنوي الرسمي للمرحلة {stage_no}</b> من 0 إلى 100.{note}",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardMarkup([["❌ إلغاء"]], resize_keyboard=True),
        )
        return CUMULATIVE_INPUT

    return await finalize_cumulative(update, context)


async def collect_cumulative_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم إلغاء التراكمي.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    score = parse_score(text)
    if score is None:
        await update.message.reply_text("<b>اكتب معدلًا رقميًا صالحًا من 0 إلى 100.</b>", parse_mode=ParseMode.HTML)
        return CUMULATIVE_INPUT

    needed: List[int] = context.user_data.get("cum_needed", [])
    idx = int(context.user_data.get("cum_needed_index", 0))
    if idx >= len(needed):
        clear_calc(context)
        await update.message.reply_text("<b>انتهت الجلسة. ابدأ من جديد.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    stage_no = needed[idx]
    values = context.user_data.setdefault("cum_values", {})
    values[str(stage_no)] = str(score)
    idx += 1
    context.user_data["cum_needed_index"] = idx
    if idx < len(needed):
        next_stage = needed[idx]
        await update.message.reply_text(
            f"<b>الآن أدخل المعدل السنوي الرسمي للمرحلة {next_stage}</b> من 0 إلى 100.",
            parse_mode=ParseMode.HTML,
        )
        return CUMULATIVE_INPUT
    return await finalize_cumulative(update, context)


async def finalize_cumulative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = int(context.user_data["cum_target_stage"])
    values = {int(k): d(v) for k, v in context.user_data.get("cum_values", {}).items()}
    user = update.effective_user
    if not user:
        clear_calc(context)
        return MAIN

    if "cum_range_stage" in context.user_data:
        range_stage = int(context.user_data["cum_range_stage"])
        min_v = d(context.user_data["cum_range_min"])
        max_v = d(context.user_data["cum_range_max"])
        result = compute_cumulative_range(values, target, range_stage, min_v, max_v)
        text = (
            f"<b>📈 التراكمي حتى {escape(STAGES[target].ar_name)}</b>\n\n"
            f"<b>المعدل التراكمي الحالي التقريبي:</b> {fmt(result['current_min'])}% - {fmt(result['current_max'])}%\n"
            f"<b>المساهمة المحققة في معدل التخرج:</b> {fmt(result['final_min'])}% - {fmt(result['final_max'])}%\n"
            f"<b>الوزن المنجز:</b> {fmt_credit(result['completed_weight'])}%\n\n"
            "<b>ملاحظة:</b> النتيجة مدى تقريبي لأن المرحلة الحالية حُسبت بالتقديرات."
        )
        DB.record_cumulative(
            user.id,
            target,
            "range",
            result["current_min"],
            result["current_max"],
            result["final_min"],
            result["final_max"],
            {str(k): str(v) for k, v in values.items()},
        )
    else:
        result = compute_cumulative_exact(values, target)
        text = (
            f"<b>📈 التراكمي حتى {escape(STAGES[target].ar_name)}</b>\n\n"
            f"<b>المعدل التراكمي الحالي:</b> {fmt(result['current_cumulative'])}%\n"
            f"<b>المساهمة المحققة في معدل التخرج النهائي:</b> {fmt(result['final_contribution'])}% من 100\n"
            f"<b>الوزن المنجز:</b> {fmt_credit(result['completed_weight'])}%\n"
        )
        DB.record_cumulative(
            user.id,
            target,
            "exact",
            result["current_cumulative"],
            result["current_cumulative"],
            result["final_contribution"],
            result["final_contribution"],
            {str(k): str(v) for k, v in values.items()},
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    clear_calc(context)
    return MAIN


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    await update.effective_message.reply_text(
        "<b>📚 اختر المرحلة لعرض المواد والكردتات:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=stage_keyboard(),
    )
    return MATERIALS_STAGE


async def show_materials_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        await update.message.reply_text("<b>تم الإلغاء.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    stage_no = stage_from_button(text)
    if not stage_no:
        await update.message.reply_text("<b>اختر مرحلة من الأزرار.</b>", parse_mode=ParseMode.HTML, reply_markup=stage_keyboard())
        return MATERIALS_STAGE
    stage = STAGES[stage_no]
    lines = [
        f"<b>📚 {escape(stage.ar_name)}</b>",
        f"<b>إجمالي Credits:</b> {fmt_credit(stage.total_credits)}",
        f"<b>وزن المرحلة:</b> {fmt_credit(stage.weight_percent)}%",
        "",
    ]
    for i, subject in enumerate(stage.subjects, start=1):
        lines.append(f"<b>{i}.</b> {escape(subject.en)} — {escape(subject.ar)} <b>({fmt_credit(subject.credits)} cr)</b>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sync_user(update, context)
    user = update.effective_user
    if not user:
        return MAIN
    await update.effective_message.reply_text(
        f"<b>Telegram ID:</b>\n<code>{user.id}</code>\n\nضع هذا الرقم في Render باسم <b>ADMIN_IDS</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard_for(update),
    )
    return MAIN


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------
def require_admin(update: Update) -> bool:
    return bool(update.effective_user and is_admin_user(update.effective_user.id))


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sync_user(update, context)
    if not require_admin(update):
        await update.effective_message.reply_text("<b>هذا القسم خاص بمدير البوت.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    clear_admin_flow(context)
    status = "متوقف للصيانة ⏸" if maintenance_enabled() else "يعمل ✅"
    db_name = "PostgreSQL" if DB.backend == "postgres" else "SQLite"
    await update.effective_message.reply_text(
        f"<b>🛠 لوحة الأدمن</b>\n\nحالة البوت: <b>{status}</b>\nقاعدة البيانات: <b>{db_name}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )
    return MAIN


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    st = DB.stats()
    stage_lines = "\n".join(f"• المرحلة {s}: {st['by_stage'].get(s,0)}" for s in range(1, 7))
    text = (
        "<b>📊 الإحصائية الكاملة</b>\n\n"
        f"👥 المستخدمون: <b>{st['users']}</b>\n"
        f"🧮 مجموع عمليات الحساب: <b>{st['calculations']}</b>\n"
        f"📄 التقارير: <b>{st['reports']}</b>\n"
        f"💾 نتائج المراحل المحفوظة: <b>{st['stage_results']}</b>\n"
        f"🚫 المحظورون: <b>{st['banned']}</b>\n\n"
        "<b>حسب آخر مرحلة اختارها المستخدم:</b>\n"
        f"{stage_lines}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    return MAIN


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    path = build_users_txt()
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="kmc_users.txt", caption="<b>قائمة المستخدمين</b>", parse_mode=ParseMode.HTML)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return MAIN


async def admin_reports_24h(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    path = build_reports_zip_24h()
    if not path:
        await update.message.reply_text("<b>لا توجد تقارير خلال آخر 24 ساعة.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="kmc_reports_24h.zip", caption="<b>تقارير آخر 24 ساعة</b>", parse_mode=ParseMode.HTML)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return MAIN


async def run_status_check(bot, admin_chat_id: int) -> None:
    users = DB.all_users()
    reachable = blocked = failed = 0
    for row in users:
        tid = int(row[0])
        if is_admin_user(tid):
            continue
        try:
            await bot.send_chat_action(chat_id=tid, action=ChatAction.TYPING)
            DB.set_status(tid, "reachable")
            reachable += 1
        except Forbidden:
            DB.set_status(tid, "blocked", auto_ban=True)
            blocked += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await bot.send_message(
        admin_chat_id,
        f"✅ انتهى الفحص\n\nReachable: {reachable}\nBlocked: {blocked}\nFailed: {failed}",
    )


async def admin_check_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    await update.message.reply_text("<b>بدأ فحص المستخدمين بالخلفية…</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    context.application.create_task(run_status_check(context.bot, update.effective_chat.id))
    return MAIN


async def admin_ask_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    await update.message.reply_text(
        "<b>🚫 أرسل Telegram ID أو @username للمستخدم.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["❌ إلغاء"], ["🔙 رجوع"]], resize_keyboard=True),
    )
    return ASK_BAN


async def admin_save_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    ident = (update.message.text or "").strip()
    if ident in {"❌ إلغاء", "🔙 رجوع"}:
        await update.message.reply_text("<b>تم الإلغاء.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    row = DB.find_user(ident)
    if not row:
        await update.message.reply_text("<b>المستخدم غير موجود في قاعدة البيانات.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    tid, first, last, username, student_name, stage = row
    if is_admin_user(int(tid)):
        await update.message.reply_text("<b>لا يمكن حظر أدمن.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    DB.ban(int(tid), "manual_admin")
    await update.message.reply_text(f"<b>تم حظر المستخدم ✅</b>\nID: <code>{tid}</code>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    return MAIN


async def admin_ask_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    await update.message.reply_text(
        "<b>✅ أرسل Telegram ID أو @username لرفع الحظر.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["❌ إلغاء"], ["🔙 رجوع"]], resize_keyboard=True),
    )
    return ASK_UNBAN


async def admin_save_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    ident = (update.message.text or "").strip()
    if ident in {"❌ إلغاء", "🔙 رجوع"}:
        await update.message.reply_text("<b>تم الإلغاء.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    row = DB.find_user(ident)
    if not row:
        await update.message.reply_text("<b>المستخدم غير موجود.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    DB.unban(int(row[0]))
    await update.message.reply_text(f"<b>تم رفع الحظر ✅</b>\nID: <code>{row[0]}</code>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    return MAIN


async def admin_banned_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    path = build_banned_users_txt()
    if not path:
        await update.message.reply_text("<b>لا توجد حسابات محظورة.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="kmc_banned_users.txt", caption="<b>قائمة المحظورين</b>", parse_mode=ParseMode.HTML)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return MAIN


BROADCAST_TARGETS = {
    "👥 كل المستخدمين": "all",
    "1️⃣ المرحلة الأولى": "stage:1",
    "2️⃣ المرحلة الثانية": "stage:2",
    "3️⃣ المرحلة الثالثة": "stage:3",
    "4️⃣ المرحلة الرابعة": "stage:4",
    "5️⃣ المرحلة الخامسة": "stage:5",
    "6️⃣ المرحلة السادسة": "stage:6",
}


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    clear_admin_flow(context)
    keyboard = ReplyKeyboardMarkup(
        [
            ["👥 كل المستخدمين"],
            ["1️⃣ المرحلة الأولى", "2️⃣ المرحلة الثانية"],
            ["3️⃣ المرحلة الثالثة", "4️⃣ المرحلة الرابعة"],
            ["5️⃣ المرحلة الخامسة", "6️⃣ المرحلة السادسة"],
            ["❌ إلغاء"],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text("<b>📢 اختر جمهور الإعلان:</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return BROADCAST_TARGET


async def admin_broadcast_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_admin_flow(context)
        await update.message.reply_text("<b>تم إلغاء الإعلان.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    target = BROADCAST_TARGETS.get(text)
    if not target:
        await update.message.reply_text("<b>اختر الجمهور من الأزرار.</b>", parse_mode=ParseMode.HTML)
        return BROADCAST_TARGET
    recipients = DB.recipient_ids(target)
    context.user_data["broadcast_target"] = target
    await update.message.reply_text(
        f"<b>عدد المستلمين الحالي: {len(recipients)}</b>\n\n"
        "أرسل الآن الإعلان كما تريد: نص، صورة، فيديو، ملف… وسأعرضه لك قبل الإرسال.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return BROADCAST_CONTENT


async def admin_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    if not update.effective_message:
        return BROADCAST_CONTENT
    msg = update.effective_message
    context.user_data["broadcast_chat_id"] = msg.chat_id
    context.user_data["broadcast_message_id"] = msg.message_id
    await update.message.reply_text("<b>👁 المعاينة:</b>", parse_mode=ParseMode.HTML)
    try:
        await context.bot.copy_message(chat_id=msg.chat_id, from_chat_id=msg.chat_id, message_id=msg.message_id)
    except TelegramError as exc:
        logger.warning("Preview copy failed: %s", exc)
        await update.message.reply_text("<b>تعذر نسخ هذه الرسالة. أرسل نوعًا آخر.</b>", parse_mode=ParseMode.HTML)
        return BROADCAST_CONTENT
    await update.message.reply_text(
        "<b>هل تريد إرسال هذا الإعلان؟</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["✅ إرسال الآن"], ["❌ إلغاء"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return BROADCAST_CONFIRM


async def run_broadcast(bot, admin_chat_id: int, target: str, source_chat_id: int, source_message_id: int, broadcast_id: int) -> None:
    recipients = DB.recipient_ids(target)
    success = failed = blocked = 0
    for index, tid in enumerate(recipients, start=1):
        while True:
            try:
                await bot.copy_message(chat_id=tid, from_chat_id=source_chat_id, message_id=source_message_id)
                success += 1
                break
            except RetryAfter as exc:
                retry_after = getattr(exc, "retry_after", 1)
                wait_for = retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)
                await asyncio.sleep(max(wait_for, 1.0))
                continue
            except Forbidden:
                blocked += 1
                DB.set_status(tid, "blocked", auto_ban=True)
                break
            except (BadRequest, TelegramError) as exc:
                failed += 1
                logger.info("Broadcast failed to %s: %s", tid, exc)
                break
        if index % 50 == 0:
            DB.update_broadcast(broadcast_id, success, failed, blocked, "running", finished=False)
        await asyncio.sleep(0.05)
    DB.update_broadcast(broadcast_id, success, failed, blocked, "completed", finished=True)
    try:
        await bot.send_message(
            admin_chat_id,
            "📢 انتهى الإعلان\n\n"
            f"المستهدفون: {len(recipients)}\n"
            f"✅ نجح: {success}\n"
            f"🚫 حظروا البوت/غير متاح: {blocked}\n"
            f"⚠️ فشل: {failed}",
        )
    except TelegramError:
        pass


async def admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_admin_flow(context)
        await update.message.reply_text("<b>تم إلغاء الإعلان.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    if text != "✅ إرسال الآن":
        await update.message.reply_text("<b>اختر إرسال أو إلغاء.</b>", parse_mode=ParseMode.HTML)
        return BROADCAST_CONFIRM
    target = context.user_data.get("broadcast_target")
    source_chat_id = context.user_data.get("broadcast_chat_id")
    source_message_id = context.user_data.get("broadcast_message_id")
    if not target or not source_chat_id or not source_message_id:
        clear_admin_flow(context)
        await update.message.reply_text("<b>انتهت جلسة الإعلان. ابدأ من جديد.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    recipients = DB.recipient_ids(target)
    bid = DB.create_broadcast(update.effective_user.id, target, int(source_chat_id), int(source_message_id), len(recipients))
    context.application.create_task(
        run_broadcast(context.bot, update.effective_chat.id, target, int(source_chat_id), int(source_message_id), bid)
    )
    clear_admin_flow(context)
    await update.message.reply_text(
        f"<b>بدأ إرسال الإعلان بالخلفية إلى {len(recipients)} مستخدم.</b>\nسأرسل لك تقريرًا عند الانتهاء.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )
    return MAIN


async def admin_maintenance_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    currently_on = maintenance_enabled()
    action = "off" if currently_on else "on"
    context.user_data["maintenance_action"] = action
    if action == "on":
        prompt = "<b>هل تؤكد إيقاف استخدام البوت للطلاب ووضعه في وضع الصيانة؟</b>\nالأدمن يبقى قادرًا على الدخول."
    else:
        prompt = "<b>هل تؤكد إعادة تشغيل البوت للطلاب؟</b>"
    await update.message.reply_text(
        prompt,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup([["✅ تأكيد"], ["❌ إلغاء"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return MAINTENANCE_CONFIRM


async def admin_maintenance_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "❌ إلغاء":
        clear_admin_flow(context)
        await update.message.reply_text("<b>تم الإلغاء.</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
        return MAIN
    if text != "✅ تأكيد":
        await update.message.reply_text("<b>اختر تأكيد أو إلغاء.</b>", parse_mode=ParseMode.HTML)
        return MAINTENANCE_CONFIRM
    action = context.user_data.get("maintenance_action")
    if action == "on":
        DB.set_setting("maintenance_enabled", "1")
        msg = "<b>⏸ تم إيقاف البوت للطلاب ووضعه في وضع الصيانة.</b>"
    elif action == "off":
        DB.set_setting("maintenance_enabled", "0")
        msg = "<b>▶️ تم تشغيل البوت للطلاب.</b>"
    else:
        msg = "<b>انتهت الجلسة. حاول مرة أخرى.</b>"
    clear_admin_flow(context)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    return MAIN


# ---------------------------------------------------------------------------
# Telegram account-status update
# ---------------------------------------------------------------------------
def upsert_chat_member_user(member_update) -> None:
    chat = getattr(member_update, "chat", None)
    from_user = getattr(member_update, "from_user", None)
    telegram_id = getattr(chat, "id", None) or getattr(from_user, "id", None)
    if not telegram_id:
        return
    DB.upsert_user(
        int(telegram_id),
        getattr(from_user, "first_name", "") or getattr(chat, "first_name", "") or "",
        getattr(from_user, "last_name", "") or getattr(chat, "last_name", "") or "",
        getattr(from_user, "username", "") or getattr(chat, "username", "") or "",
    )


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    member_update = update.my_chat_member
    if not member_update:
        return
    chat = member_update.chat
    if getattr(chat, "type", "") != "private":
        return
    telegram_id = int(chat.id)
    if is_admin_user(telegram_id):
        return
    upsert_chat_member_user(member_update)
    new_status = getattr(member_update.new_chat_member, "status", "")
    if new_status in {"kicked", "left"}:
        DB.set_status(telegram_id, "blocked", auto_ban=True)
    elif new_status in {"member", "administrator"}:
        if not is_user_banned(telegram_id):
            DB.set_status(telegram_id, "reachable")
        else:
            DB.set_status(telegram_id, "returned_blocked")


# ---------------------------------------------------------------------------
# Main text router
# ---------------------------------------------------------------------------
async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await deny_if_unavailable(update, context):
        return MAIN
    text = (update.message.text or "").strip()
    if text == "🧮 حساب المعدل":
        return await begin_calculation(update, context)
    if text == "📈 حساب التراكمي":
        return await begin_cumulative(update, context)
    if text == "📝 إضافة/تغيير الاسم":
        return await ask_name(update, context)
    if text == "📚 عرض المواد":
        return await list_command(update, context)
    if text == "ℹ️ المساعدة":
        return await help_command(update, context)
    if text == "🔄 إعادة البداية":
        return await reset_command(update, context)
    if text == "🛠 لوحة الأدمن":
        return await admin_panel(update, context)
    if text == "📊 الإحصائية الكاملة":
        return await admin_stats(update, context)
    if text == "👥 قائمة المستخدمين الكاملة":
        return await admin_users(update, context)
    if text == "📁 ملفات آخر 24 ساعة":
        return await admin_reports_24h(update, context)
    if text == "🔎 فحص حالة المستخدمين":
        return await admin_check_status(update, context)
    if text == "📢 إرسال إعلان":
        return await admin_broadcast_start(update, context)
    if text in {"⏸ إيقاف البوت", "▶️ تشغيل البوت"}:
        return await admin_maintenance_request(update, context)
    if text == "🚫 حظر مستخدم":
        return await admin_ask_ban(update, context)
    if text == "✅ رفع حظر":
        return await admin_ask_unban(update, context)
    if text == "📄 قائمة المحظورين":
        return await admin_banned_list(update, context)
    if text == "🔙 رجوع":
        return await start(update, context)
    await update.message.reply_text("<b>اختر من أزرار الكيبورد.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("صار خطأ تقني مؤقت. أعد المحاولة من /start.")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    DB.init_schema()
    logger.info("Database backend: %s", DB.backend)
    start_health_server()

    application = Application.builder().token(token).post_init(post_init).build()
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(ChatMemberHandler(my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("calculate", begin_calculation),
            CommandHandler("cumulative", begin_cumulative),
            CommandHandler("rename", ask_name),
            CommandHandler("list", list_command),
        ],
        states={
            MAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)],
            SELECT_STAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_stage)],
            MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_mode)],
            COLLECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_answer)],
            MATERIALS_STAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_materials_stage)],
            ASK_CUMULATIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cumulative_offer)],
            CUMULATIVE_STAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_cumulative_stage)],
            CUMULATIVE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_cumulative_input)],
            ASK_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_ban)],
            ASK_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_unban)],
            BROADCAST_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_target)],
            BROADCAST_CONTENT: [MessageHandler(~filters.COMMAND, admin_broadcast_content)],
            BROADCAST_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_confirm)],
            MAINTENANCE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_maintenance_confirm)],
        },
        fallbacks=[
            CommandHandler("reset", reset_command),
            CommandHandler("help", help_command),
            CommandHandler("about", about_command),
            CommandHandler("list", list_command),
            CommandHandler("start", start),
            CommandHandler("calculate", begin_calculation),
            CommandHandler("cumulative", begin_cumulative),
            CommandHandler("myid", myid_command),
            CommandHandler("admin", admin_panel),
        ],
        allow_reentry=True,
    )
    application.add_handler(conv)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("reset", reset_command))

    logger.info("KMC Grade Calculator is running")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
