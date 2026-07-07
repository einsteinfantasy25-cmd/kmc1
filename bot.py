import csv
import logging
import glob
import os
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from html import escape
from typing import Dict, List, Optional, Set, Tuple

from telegram import BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:
    arabic_reshaper = None
    get_display = None

# KMC B27 Stage 1 Grade Calculator Bot
# 15 subjects, 36 credits, Stage 1 contribution = 5% of final cumulative grade.

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

MAIN, ASK_NAME, MODE, COLLECT = range(4)
TOTAL_CREDITS = 36
STAGE_WEIGHT_PERCENT = 5
BOT_TITLE = "KMC B27 | Grade Calculator"
REPORT_TITLE = "KMC B27 GRADE CALCULATOR"
COLLEGE_NAME = "Al-Kindy College of Medicine"
BATCH_NAME = "Batch 27"
DEVELOPER_NAME = "Osama"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")
IRAQ_TZ = ZoneInfo("Asia/Baghdad") if ZoneInfo else timezone(timedelta(hours=3))

def iraq_now() -> datetime:
    return datetime.now(IRAQ_TZ).replace(tzinfo=None)

DATA_DIR = os.getenv("BOT_DATA_DIR", os.path.join(os.getcwd(), "bot_data"))
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)
DB_PATH = os.getenv("USERS_DB_PATH", os.path.join(DATA_DIR, "users.db"))


@dataclass(frozen=True)
class Subject:
    key: str
    en: str
    ar: str
    credits: int


SUBJECTS: List[Subject] = [
    Subject("anatomy", "Human Anatomy", "التشريح البشري", 4),
    Subject("medical_physics", "Medical Physics", "الفيزياء الطبية", 3),
    Subject("cell_gene", "Human Cell & Gene", "الخلية والموروثة الجينية", 3),
    Subject("foundation", "Foundation of Medicine", "أساسيات الطب", 2),
    Subject("human_rights", "Human Rights", "حقوق الإنسان", 2),
    Subject("med_term_1", "Medical Terminology 1", "المصطلحات الطبية 1", 1),
    Subject("arabic_1", "Arabic Language 1", "اللغة العربية 1", 1),
    Subject("hsd", "Human Structure & Development", "التركيب والنشوء البشري", 5),
    Subject("biochemistry", "Biochemistry", "الكيمياء الحياتية", 3),
    Subject("physiology", "Physiology", "الفسلجة", 3),
    Subject("micro_immunity", "Microbiology & Immunity", "الأحياء المجهرية والمناعة", 3),
    Subject("health_disease", "Concept of Health & Disease", "مفاهيم الصحة والمرض", 2),
    Subject("basic_computer", "Basic Computer", "أساسيات الحاسوب", 2),
    Subject("med_term_2", "Medical Terminology 2", "المصطلحات الطبية 2", 1),
    Subject("arabic_2", "Arabic Language 2", "اللغة العربية 2", 1),
]

GRADES: Dict[str, Tuple[str, int, int]] = {
    "امتياز": ("Excellent", 90, 100),
    "جيد جدًا": ("Very Good", 80, 89),
    "جيد جدا": ("Very Good", 80, 89),
    "جيد": ("Good", 70, 79),
    "متوسط": ("Fair", 60, 69),
    "مقبول": ("Pass", 50, 59),
    "راسب": ("Fail", 0, 49),
}

MAIN_ROWS = [
    ["🧮 حساب المعدل", "📝 إضافة/تغيير الاسم"],
    ["📚 عرض المواد", "ℹ️ المساعدة"],
    ["🔄 إعادة البداية"],
]

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 الإحصائية الكاملة"],
        ["👥 قائمة المستخدمين الكاملة"],
        ["📁 ملفات آخر 24 ساعة"],
        ["🔎 فحص حالة المستخدمين"],
        ["🔙 رجوع"],
    ],
    resize_keyboard=True,
)

MODE_KEYBOARD = ReplyKeyboardMarkup(
    [["📊 حساب بالتقديرات"], ["🔢 حساب بالدرجات الرقمية"], ["❌ إلغاء"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

GRADE_KEYBOARD = ReplyKeyboardMarkup(
    [["امتياز", "جيد جدًا"], ["جيد", "متوسط"], ["مقبول", "راسب"], ["❌ إلغاء"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# -------------------------- helpers --------------------------

def now_str() -> str:
    return iraq_now().strftime("%Y-%m-%d %H:%M:%S")


def get_admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: Set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_admin_user(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in get_admin_ids())


def main_keyboard_for(update: Update) -> ReplyKeyboardMarkup:
    rows = [row[:] for row in MAIN_ROWS]
    if update.effective_user and is_admin_user(update.effective_user.id):
        rows.append(["🛠 لوحة الأدمن"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def has_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text or ""))


def pdf_text(text: str) -> str:
    """Prepare Arabic / mixed text for ReportLab PDF."""
    text = str(text or "")
    if has_arabic(text) and arabic_reshaper and get_display:
        try:
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text


def safe_filename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_\-\u0600-\u06FF ]+", "", name or "").strip().replace(" ", "_")
    clean = clean[:50].strip("_")
    return clean if clean else "student"


def subject_line(subject: Subject) -> str:
    return f"{subject.en}\n{subject.ar}\nCredits: {subject.credits}"


def clear_calc(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ["mode", "index", "answers"]:
        context.user_data.pop(key, None)


# -------------------------- database --------------------------

def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                student_name TEXT,
                calculations INTEGER DEFAULT 0,
                first_seen TEXT,
                last_seen TEXT,
                status TEXT DEFAULT 'unknown',
                last_status_check TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                student_name TEXT,
                username TEXT,
                filename TEXT,
                path TEXT,
                mode TEXT,
                summary TEXT,
                created_at TEXT
            )
            """
        )
        # migration for older DBs
        for col, ddl in [
            ("status", "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'unknown'"),
            ("last_status_check", "ALTER TABLE users ADD COLUMN last_status_check TEXT"),
        ]:
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        con.commit()


def upsert_user(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> None:
    user = update.effective_user
    if not user:
        return
    init_db()
    student_name = ""
    if context is not None:
        student_name = context.user_data.get("student_name", "") or ""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO users (telegram_id, first_name, last_name, username, student_name, calculations, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, COALESCE((SELECT status FROM users WHERE telegram_id = ?), 'unknown'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                student_name=CASE WHEN excluded.student_name != '' THEN excluded.student_name ELSE users.student_name END,
                last_seen=excluded.last_seen
            """,
            (user.id, user.first_name or "", user.last_name or "", user.username or "", student_name, now_str(), now_str(), user.id),
        )
        con.commit()


def set_student_name_in_db(update: Update, student_name: str) -> None:
    if not update.effective_user:
        return
    init_db()
    user = update.effective_user
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO users (telegram_id, first_name, last_name, username, student_name, calculations, first_seen, last_seen, status)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, COALESCE((SELECT status FROM users WHERE telegram_id = ?), 'unknown'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                student_name=excluded.student_name,
                last_seen=excluded.last_seen
            """,
            (user.id, user.first_name or "", user.last_name or "", user.username or "", student_name, now_str(), now_str(), user.id),
        )
        con.commit()


def increment_calculation(update: Update) -> None:
    if not update.effective_user:
        return
    init_db()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE users SET calculations = COALESCE(calculations, 0) + 1, last_seen = ? WHERE telegram_id = ?",
            (now_str(), update.effective_user.id),
        )
        con.commit()


def update_user_status(telegram_id: int, status: str) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE users SET status = ?, last_status_check = ? WHERE telegram_id = ?",
            (status, now_str(), telegram_id),
        )
        con.commit()


def record_report(update: Update, student_name: str, path: str, send_filename: str, mode: str, result: dict) -> None:
    if not update.effective_user:
        return
    init_db()
    summary = ""
    if mode == "grades":
        summary = f"{result['min_avg']:.2f}-{result['max_avg']:.2f}% | impact {result['min_contribution']:.2f}-{result['max_contribution']:.2f}% of final"
    else:
        summary = f"{result['avg']:.2f}% | impact {result['contribution']:.2f}% of final"
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO reports (telegram_id, student_name, username, filename, path, mode, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                update.effective_user.id,
                student_name,
                update.effective_user.username or "",
                send_filename,
                path,
                mode,
                summary,
                now_str(),
            ),
        )
        con.commit()


def get_admin_stats() -> dict:
    init_db()
    since_24 = (iraq_now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    since_7 = (iraq_now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        return {
            "total_users": cur.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "named_users": cur.execute("SELECT COUNT(*) FROM users WHERE student_name IS NOT NULL AND student_name != ''").fetchone()[0],
            "active_24h": cur.execute("SELECT COUNT(*) FROM users WHERE datetime(last_seen) >= datetime(?)", (since_24,)).fetchone()[0],
            "active_7d": cur.execute("SELECT COUNT(*) FROM users WHERE datetime(last_seen) >= datetime(?)", (since_7,)).fetchone()[0],
            "total_calcs": cur.execute("SELECT COALESCE(SUM(calculations), 0) FROM users").fetchone()[0],
            "reports_total": cur.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
            "reports_24h": cur.execute("SELECT COUNT(*) FROM reports WHERE datetime(created_at) >= datetime(?)", (since_24,)).fetchone()[0],
            "reachable": cur.execute("SELECT COUNT(*) FROM users WHERE status = 'reachable'").fetchone()[0],
            "blocked": cur.execute("SELECT COUNT(*) FROM users WHERE status = 'blocked'").fetchone()[0],
            "unknown": cur.execute("SELECT COUNT(*) FROM users WHERE status IS NULL OR status = 'unknown'").fetchone()[0],
        }


def get_all_users() -> List[tuple]:
    init_db()
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            """
            SELECT telegram_id, first_name, last_name, username, student_name, calculations, first_seen, last_seen, status, last_status_check
            FROM users
            ORDER BY datetime(last_seen) DESC
            """
        ).fetchall()


def build_users_txt() -> str:
    rows = get_all_users()
    fd, path = tempfile.mkstemp(prefix="kmc_b27_users_full_", suffix=".txt")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("KMC B27 Grade Calculator - Full Users List\n")
        f.write(f"Generated at: {now_str()}\n")
        f.write(f"Total users: {len(rows)}\n")
        f.write("=" * 80 + "\n\n")
        for i, (tid, first, last, username, student_name, calculations, first_seen, last_seen, status, last_status_check) in enumerate(rows, start=1):
            tg_name = " ".join(x for x in [first, last] if x).strip() or "No Telegram name"
            uname = f"@{username}" if username else "No username"
            sname = student_name or "No student name"
            f.write(f"{i}. Student Name: {sname}\n")
            f.write(f"   Telegram Name: {tg_name}\n")
            f.write(f"   Username: {uname}\n")
            f.write(f"   Telegram ID: {tid}\n")
            f.write(f"   Calculations: {calculations}\n")
            f.write(f"   Status: {status or 'unknown'}\n")
            f.write(f"   First Seen: {first_seen}\n")
            f.write(f"   Last Seen: {last_seen}\n")
            f.write(f"   Last Status Check: {last_status_check or 'not checked'}\n")
            f.write("-" * 80 + "\n")
    return path


def get_recent_reports_24h() -> List[tuple]:
    init_db()
    since_24 = (iraq_now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            """
            SELECT id, telegram_id, student_name, username, filename, path, created_at, summary
            FROM reports
            WHERE datetime(created_at) >= datetime(?)
            ORDER BY datetime(created_at) DESC
            """,
            (since_24,),
        ).fetchall()


def build_reports_zip_24h() -> Optional[str]:
    reports = get_recent_reports_24h()
    existing = [r for r in reports if r[5] and os.path.exists(r[5])]
    if not existing:
        return None
    zip_path = os.path.join(tempfile.gettempdir(), f"kmc_b27_reports_last_24h_{iraq_now().strftime('%Y%m%d_%H%M')}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        summary_lines = [
            "KMC B27 Grade Calculator - Reports Last 24 Hours",
            f"Generated at: {now_str()}",
            f"Reports count: {len(existing)}",
            "",
        ]
        for idx, (_rid, tid, student_name, username, filename, path, created_at, summary) in enumerate(existing, start=1):
            safe_student = safe_filename(student_name or f"student_{tid}")
            archive_name = f"{idx:03d}_{created_at.replace(':', '-').replace(' ', '_')}_{safe_student}.pdf"
            zf.write(path, archive_name)
            summary_lines.append(f"{idx}. {student_name} | @{username if username else 'no_username'} | ID {tid} | {created_at} | {summary} | {archive_name}")
        zf.writestr("reports_summary.txt", "\n".join(summary_lines))
    return zip_path


# -------------------------- PDF --------------------------

def _font_candidates(patterns: List[str]) -> List[str]:
    found: List[str] = []
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            if os.path.exists(path) and path not in found:
                found.append(path)
    return found

# The student PDF is generated as a high-resolution A4 image and then saved as PDF.
# This avoids ReportLab Arabic rendering problems and guarantees that Arabic names
# display correctly as long as an Arabic-capable TrueType font exists on the server.
# Railway installs DejaVu fonts through nixpacks.toml.
from PIL import Image as PILImage, ImageDraw, ImageFont, features

A4_W, A4_H = 1240, 1754  # A4 portrait at ~150 DPI
MARGIN = 64
NAVY_HEX = "#0D1F44"
BLUE_HEX = "#2F5DA8"
SOFT_HEX = "#F5F7FC"
ROW_HEX = "#EAF0F8"
LINE_HEX = "#D8E0EE"
TEXT_HEX = "#101827"
MUTED_HEX = "#5C6475"
WHITE_HEX = "#FFFFFF"


def _font_paths() -> dict:
    """Find fonts without bundling font files inside the project."""
    regular_patterns = [
        os.path.join(os.path.dirname(__file__), "NotoNaskhArabic-Regular.ttf"),
        os.path.join(os.path.dirname(__file__), "NotoSansArabic-Regular.ttf"),
        os.path.join(os.path.dirname(__file__), "Amiri-Regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    ] + _font_candidates([
        "/nix/store/**/DejaVuSans.ttf",
        "/nix/store/**/NotoNaskhArabic-Regular.ttf",
        "/nix/store/**/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/**/DejaVuSans.ttf",
        "/usr/share/fonts/**/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/**/NotoSansArabic-Regular.ttf",
    ])
    bold_patterns = [
        os.path.join(os.path.dirname(__file__), "NotoNaskhArabic-Bold.ttf"),
        os.path.join(os.path.dirname(__file__), "NotoSansArabic-Bold.ttf"),
        os.path.join(os.path.dirname(__file__), "Amiri-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    ] + _font_candidates([
        "/nix/store/**/DejaVuSans-Bold.ttf",
        "/nix/store/**/NotoNaskhArabic-Bold.ttf",
        "/nix/store/**/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/**/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/**/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/**/NotoSansArabic-Bold.ttf",
    ])
    regular = next((x for x in regular_patterns if os.path.exists(x)), None)
    bold = next((x for x in bold_patterns if os.path.exists(x)), regular)
    return {"regular": regular, "bold": bold or regular}

_FONT_PATHS = _font_paths()
PIL_HAS_RAQM = bool(features.check("raqm"))


def font(size: int, bold: bool = False):
    path = _FONT_PATHS.get("bold" if bold else "regular") or _FONT_PATHS.get("regular")
    try:
        if path:
            try:
                return ImageFont.truetype(path, size=size, layout_engine=ImageFont.Layout.RAQM)
            except Exception:
                return ImageFont.truetype(path, size=size)
    except Exception:
        pass
    return ImageFont.load_default()


def shape_text(text: str) -> str:
    """Prepare text for the PDF image renderer.

    Important: Pillow on Railway normally supports RAQM, which means it can render
    Arabic directly. In that case we MUST NOT apply arabic_reshaper/get_display,
    because double shaping makes Arabic names look reversed or corrupted.
    The reshaper fallback is used only when RAQM is unavailable.
    """
    text = str(text or "")
    if has_arabic(text) and (not PIL_HAS_RAQM) and arabic_reshaper and get_display:
        try:
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text


def text_bbox(draw: ImageDraw.ImageDraw, text: str, fnt) -> tuple:
    try:
        return draw.textbbox((0, 0), text, font=fnt)
    except Exception:
        w, h = draw.textsize(text, font=fnt)
        return (0, 0, w, h)


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt) -> int:
    b = text_bbox(draw, text, fnt)
    return b[2] - b[0]


def text_height(draw: ImageDraw.ImageDraw, text: str, fnt) -> int:
    b = text_bbox(draw, text, fnt)
    return b[3] - b[1]


def draw_text(draw: ImageDraw.ImageDraw, xy, text: str, fnt, fill=TEXT_HEX, anchor=None, align="left"):
    shaped = shape_text(text)
    x, y = xy
    if align == "right":
        x -= text_width(draw, shaped, fnt)
    elif align == "center":
        x -= text_width(draw, shaped, fnt) // 2
    draw.text((x, y), shaped, font=fnt, fill=fill, anchor=anchor)


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, bold: bool = True, min_size: int = 18):
    current = size
    while current >= min_size:
        f = font(current, bold=bold)
        if text_width(draw, shape_text(text), f) <= max_width:
            return f
        current -= 1
    return font(min_size, bold=bold)


def wrap_words(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> List[str]:
    text = str(text or "")
    # Keep Arabic names on one line by shrinking them elsewhere; this wrapper is for English labels.
    words = text.split()
    if not words:
        return [""]
    lines = []
    cur = words[0]
    for word in words[1:]:
        trial = cur + " " + word
        if text_width(draw, trial, fnt) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def draw_box(draw: ImageDraw.ImageDraw, box, outline=NAVY_HEX, fill=WHITE_HEX, width=2, radius=10):
    try:
        draw.rounded_rectangle(box, radius=radius, outline=outline, fill=fill, width=width)
    except Exception:
        draw.rectangle(box, outline=outline, fill=fill, width=width)


def draw_label_value(draw: ImageDraw.ImageDraw, x: int, y: int, label_text: str, value_text: str, value_max_w: int):
    label_f = font(28, bold=True)
    value_f = fit_text(draw, value_text, value_max_w, 30, bold=True, min_size=18)
    draw_text(draw, (x, y), label_text, label_f, fill=NAVY_HEX)
    draw_text(draw, (x + 230, y), value_text, value_f, fill=TEXT_HEX)


def draw_kv_cell(draw: ImageDraw.ImageDraw, box, label_text: str, value_text: str):
    x0, y0, x1, y1 = box
    label_f = font(26, bold=True)
    # Reserve the left part for the label and the right part for the value.
    label_x = x0 + 22
    value_x0 = x0 + 220
    value_x1 = x1 - 20
    y = y0 + 18
    draw_text(draw, (label_x, y), label_text, label_f, fill=NAVY_HEX)
    max_w = max(90, value_x1 - value_x0)
    value_f = fit_text(draw, value_text, max_w, 28, bold=True, min_size=16)
    if has_arabic(str(value_text)):
        # Arabic names are right-aligned inside their value cell so word order appears natural.
        draw_text(draw, (value_x1, y), value_text, value_f, fill=TEXT_HEX, align="right")
    else:
        draw_text(draw, (value_x0, y), value_text, value_f, fill=TEXT_HEX)


def draw_table_cell(draw: ImageDraw.ImageDraw, box, text: str, fnt, fill=TEXT_HEX, align="left", bold=False):
    x0, y0, x1, y1 = box
    pad = 12
    max_w = (x1 - x0) - 2 * pad
    shaped = shape_text(text)
    # For ordinary text, shrink rather than overflow.
    local_font = fnt
    while text_width(draw, shaped, local_font) > max_w and getattr(local_font, 'size', 10) > 12:
        local_font = font(local_font.size - 1, bold=bold)
    h = text_height(draw, shaped, local_font)
    y = y0 + ((y1 - y0 - h) // 2) - 2
    if align == "center":
        x = x0 + (x1 - x0) // 2
        draw_text(draw, (x, y), text, local_font, fill=fill, align="center")
    elif align == "right":
        draw_text(draw, (x1 - pad, y), text, local_font, fill=fill, align="right")
    else:
        draw_text(draw, (x0 + pad, y), text, local_font, fill=fill)


def draw_clean_pdf_image(student_name: str, answers: List[dict], result: dict, mode: str) -> PILImage.Image:
    img = PILImage.new("RGB", (A4_W, A4_H), "white")
    draw = ImageDraw.Draw(img)

    # Background and border
    draw.rectangle((0, 0, A4_W, A4_H), fill="#FCFDFF")
    draw_box(draw, (36, 36, A4_W - 36, A4_H - 36), outline=NAVY_HEX, fill="#FCFDFF", width=3, radius=14)

    # Header
    header_x0, header_y0, header_x1, header_y1 = 64, 64, A4_W - 64, 260
    draw_box(draw, (header_x0, header_y0, header_x1, header_y1), outline=LINE_HEX, fill=WHITE_HEX, width=2, radius=12)
    draw.rectangle((header_x0, header_y1 - 7, header_x1, header_y1), fill=BLUE_HEX)

    title_f = fit_text(draw, REPORT_TITLE, 780, 50, bold=True, min_size=36)
    sub_f = font(26, bold=True)
    draw_text(draw, (92, 90), REPORT_TITLE, title_f, fill=NAVY_HEX)
    draw_text(draw, (92, 160), "Stage 1 Grade Report | Al-Kindy College of Medicine", sub_f, fill=NAVY_HEX)
    draw_text(draw, (92, 200), f"Batch 27 | Developed by {DEVELOPER_NAME}", sub_f, fill=NAVY_HEX)

    if os.path.exists(LOGO_PATH):
        try:
            logo = PILImage.open(LOGO_PATH).convert("RGBA")
            logo.thumbnail((230, 155), PILImage.LANCZOS)
            lx = header_x1 - 270
            ly = header_y0 + 22
            # White space behind the logo so colors never cover it.
            draw.rounded_rectangle((lx - 10, ly - 10, lx + 250, ly + 160), radius=12, fill="white")
            img.paste(logo, (lx, ly), logo)
        except Exception:
            pass

    # Student info: privacy friendly, no Telegram username/id/name.
    info_x0, info_y0, info_x1, info_y1 = 64, 290, A4_W - 64, 430
    draw_box(draw, (info_x0, info_y0, info_x1, info_y1), outline=LINE_HEX, fill=WHITE_HEX, width=2, radius=10)
    mid_x = info_x0 + (info_x1 - info_x0) // 2
    mid_y = info_y0 + (info_y1 - info_y0) // 2
    draw.line((mid_x, info_y0 + 14, mid_x, info_y1 - 14), fill=LINE_HEX, width=2)
    draw.line((info_x0 + 14, mid_y, info_x1 - 14, mid_y), fill=LINE_HEX, width=2)

    draw_kv_cell(draw, (info_x0, info_y0, mid_x, mid_y), "Student Name", student_name)
    draw_kv_cell(draw, (mid_x, info_y0, info_x1, mid_y), "Report Date", iraq_now().strftime("%Y-%m-%d %H:%M") + " Iraq")
    draw_kv_cell(draw, (info_x0, mid_y, mid_x, info_y1), "Stage", "First Year")
    draw_kv_cell(draw, (mid_x, mid_y, info_x1, info_y1), "College", "Al-Kindy Medicine")

    # Final result block
    result_x0, result_y0, result_x1, result_y1 = 64, 460, A4_W - 64, 655
    draw.rounded_rectangle((result_x0, result_y0, result_x1, result_y1), radius=12, fill=WHITE_HEX, outline=LINE_HEX, width=2)
    draw.rounded_rectangle((result_x0, result_y0, result_x1, result_y0 + 58), radius=12, fill=SOFT_HEX, outline=None)
    draw.rectangle((result_x0, result_y0 + 48, result_x1, result_y0 + 58), fill=SOFT_HEX)
    draw_text(draw, (92, 474), "Final Result", font(30, bold=True), fill=NAVY_HEX)
    if mode == "grades":
        note = "Grade categories give a range. Stage 1 impact is shown as a percent of final cumulative grade."
    else:
        note = "Numeric scores were used, so the result is exact based on entered values."
    draw_text(draw, (325, 482), note, fit_text(draw, note, 820, 22, bold=True, min_size=16), fill=MUTED_HEX)

    # Result table fills full width of the result box
    rt_x0, rt_y0 = result_x0, result_y0 + 58
    rt_x1, rt_y1 = result_x1, result_y1
    if mode == "grades":
        headers = ["Metric", "Minimum", "Middle", "Maximum"]
        rows = [
            ["Stage average", f"{result['min_avg']:.2f}%", f"{result['avg_avg']:.2f}%", f"{result['max_avg']:.2f}%"],
            ["Cumulative impact", f"{result['min_contribution']:.2f}%", f"{result['avg_contribution']:.2f}%", f"{result['max_contribution']:.2f}%"],
        ]
        widths = [430, 227, 227, 228]
    else:
        headers = ["Metric", "Value"]
        rows = [["Stage average", f"{result['avg']:.2f}%"], ["Cumulative impact", f"{result['contribution']:.2f}%"]]
        widths = [650, 462]
    row_h = (rt_y1 - rt_y0) // (len(rows) + 1)
    x = rt_x0
    for ci, w in enumerate(widths):
        draw.rectangle((x, rt_y0, x + w, rt_y0 + row_h), fill=NAVY_HEX, outline="white", width=2)
        draw_table_cell(draw, (x, rt_y0, x + w, rt_y0 + row_h), headers[ci], font(22, bold=True), fill="white", align="center", bold=True)
        x += w
    for ri, row in enumerate(rows):
        y0 = rt_y0 + row_h * (ri + 1)
        x = rt_x0
        for ci, w in enumerate(widths):
            fill = "#EEF3FB" if ri % 2 == 0 else "#FFFFFF"
            draw.rectangle((x, y0, x + w, y0 + row_h), fill=fill, outline="white", width=2)
            draw_table_cell(draw, (x, y0, x + w, y0 + row_h), row[ci], font(23, bold=True), align="center" if ci else "left", bold=True)
            x += w

    # Main subject table: true full page width and stretched vertically.
    table_x0, table_y0, table_x1, table_y1 = 64, 690, A4_W - 64, 1588
    draw_box(draw, (table_x0, table_y0, table_x1, table_y1), outline=LINE_HEX, fill=WHITE_HEX, width=2, radius=10)
    headers = ["Subject", "Cr", "Grade" if mode == "grades" else "Score", "Range" if mode == "grades" else "Impact", "Impact" if mode == "grades" else ""]
    if mode == "grades":
        col_widths = [445, 75, 185, 175, 232]  # total 1112 px = full table width
    else:
        col_widths = [585, 90, 190, 247]
        headers = ["Subject", "Cr", "Score", "Impact"]
    header_h = 52
    body_h = table_y1 - table_y0 - header_h
    row_h = body_h // len(answers)
    # Header row
    x = table_x0
    for ci, w in enumerate(col_widths):
        draw.rectangle((x, table_y0, x + w, table_y0 + header_h), fill=NAVY_HEX, outline="white", width=2)
        draw_table_cell(draw, (x, table_y0, x + w, table_y0 + header_h), headers[ci], font(23, bold=True), fill="white", align="center" if ci else "left", bold=True)
        x += w
    # Rows
    for ri, item in enumerate(answers):
        y0 = table_y0 + header_h + ri * row_h
        y1 = table_y0 + header_h + (ri + 1) * row_h if ri < len(answers) - 1 else table_y1
        row_fill = "#FFFFFF" if ri % 2 == 0 else ROW_HEX
        if mode == "grades":
            cmin = item["min_score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            cmax = item["max_score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            vals = [item["subject_en"], str(item["credits"]), item["grade_en"], f"{item['min_score']}-{item['max_score']}", f"{cmin:.2f}% - {cmax:.2f}%"]
        else:
            contrib = item["score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            vals = [item["subject_en"], str(item["credits"]), f"{item['score']:.2f}", f"{contrib:.2f}%"]
        x = table_x0
        for ci, w in enumerate(col_widths):
            draw.rectangle((x, y0, x + w, y1), fill=row_fill, outline="white", width=2)
            cell_font = font(21 if ci == 0 else 20, bold=True)
            draw_table_cell(draw, (x, y0, x + w, y1), vals[ci], cell_font, align="center" if ci else "left", bold=True)
            x += w

    # Footer note
    footer_f = font(16, bold=True)
    footer = "This report is automatically generated by KMC B27 Grade Calculator. It is not an official college transcript."
    draw_text(draw, (A4_W // 2, 1625), footer, footer_f, fill=MUTED_HEX, align="center")
    draw_text(draw, (A4_W // 2, 1662), f"Developed by {DEVELOPER_NAME} | Generated in Iraq time", footer_f, fill=NAVY_HEX, align="center")
    return img


def create_pdf_report(student_name: str, answers: List[dict], result: dict, mode: str, update: Update) -> Tuple[str, str]:
    user = update.effective_user
    telegram_id = str(user.id) if user else "unknown"
    send_filename = f"{safe_filename(student_name)}.pdf"
    storage_filename = f"{safe_filename(student_name)}_{iraq_now().strftime('%Y%m%d_%H%M%S')}_{telegram_id}.pdf"
    path = os.path.join(REPORTS_DIR, storage_filename)

    image = draw_clean_pdf_image(student_name, answers, result, mode)
    # Saving the rendered A4 image as PDF makes Arabic output reliable across PDF viewers.
    image.save(path, "PDF", resolution=150.0)
    return path, send_filename

# -------------------------- Telegram handlers --------------------------

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("calculate", "حساب المعدل"),
        BotCommand("rename", "إضافة أو تغيير الاسم"),
        BotCommand("list", "عرض المواد"),
        BotCommand("help", "شرح طريقة الحساب"),
        BotCommand("reset", "إعادة البداية"),
        BotCommand("myid", "معرفة رقم حسابك"),
        BotCommand("admin", "لوحة الأدمن"),
    ]
    await application.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    clear_calc(context)
    name = context.user_data.get("student_name", "غير مضاف")
    text = (
        f"<b>أهلًا بك في {escape(BOT_TITLE)} 👋</b>\n\n"
        f"<b>اسم الطالب الحالي:</b> {escape(name)}\n"
        "<b>اختار من لوحة الكيبورد بالأسفل.</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = (
        "<b>طريقة الحساب المختصرة:</b>\n\n"
        "<b>1) بالتقديرات</b>\n"
        "يعطيك أقل وأعلى معدل ممكن لأن التقدير يمثل مدى درجات.\n\n"
        "<b>2) بالدرجات الرقمية</b>\n"
        "يعطيك معدل أدق لأنك تدخل الدرجة نفسها.\n\n"
        "<b>القانون:</b>\n"
        "معدل المرحلة = مجموع (درجة المادة × الكردت) ÷ 36\n"
        "مساهمة المرحلة بالتراكمي النهائي = معدل المرحلة × 0.05\n"
        "وتظهر كنسبة من الدرجة النهائية الكلية، مثال: 4.25% وليس 4.25/5.\n\n"
        "<b>بعد إكمال الحساب، يرسل البوت تقرير PDF باسم الطالب.</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    lines = ["<b>مواد البوت الداخلة بالحساب:</b>", ""]
    for i, s in enumerate(SUBJECTS, start=1):
        lines.append(f"<b>{i}.</b> {escape(s.en)} - {escape(s.ar)} <b>({s.credits} cr)</b>")
    lines.append("")
    lines.append(f"<b>المجموع = {TOTAL_CREDITS} credits</b>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    clear_calc(context)
    context.user_data.pop("student_name", None)
    await update.effective_message.reply_text("<b>تمت إعادة البداية وحذف الاسم المؤقت.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    await update.effective_message.reply_text(
        "<b>اكتب اسمك الثلاثي الذي تريد ظهوره داخل تقرير الـ PDF.</b>\n\n"
        "يمكن كتابة الاسم بالعربي أو الإنكليزي.",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if name.startswith("/"):
        await update.message.reply_text("<b>اكتب الاسم كنص، وليس أمرًا يبدأ بعلامة /.</b>", parse_mode=ParseMode.HTML)
        return ASK_NAME
    if len(name.replace("@", "").strip()) < 2:
        await update.message.reply_text("<b>اكتب اسمًا واضحًا أكثر من حرف واحد.</b>", parse_mode=ParseMode.HTML)
        return ASK_NAME
    context.user_data["student_name"] = name
    set_student_name_in_db(update, name)
    await update.message.reply_text(f"<b>تم حفظ الاسم:</b> {escape(name)} ✅", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def begin_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    clear_calc(context)
    if not context.user_data.get("student_name"):
        await update.effective_message.reply_text(
            "<b>قبل الحساب، يجب إضافة اسم الطالب الثلاثي حتى يظهر داخل تقرير PDF.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_name(update, context)
    await update.effective_message.reply_text("<b>اختار طريقة الحساب:</b>", parse_mode=ParseMode.HTML, reply_markup=MODE_KEYBOARD)
    return MODE


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = update.message.text.strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم إلغاء الحساب.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    if text == "📊 حساب بالتقديرات":
        context.user_data["mode"] = "grades"
    elif text == "🔢 حساب بالدرجات الرقمية":
        context.user_data["mode"] = "scores"
    else:
        await update.message.reply_text("<b>اختار من أزرار الكيبورد فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=MODE_KEYBOARD)
        return MODE
    context.user_data["index"] = 0
    context.user_data["answers"] = []
    return await ask_current_subject(update, context)


async def ask_current_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["index"]
    subject = SUBJECTS[idx]
    mode = context.user_data["mode"]
    text = (
        f"<b>المادة {idx + 1} من {len(SUBJECTS)}</b>\n\n"
        f"<b>{escape(subject.en)}</b>\n"
        f"{escape(subject.ar)}\n"
        f"<b>Credits:</b> {subject.credits}\n\n"
    )
    if mode == "grades":
        text += "<b>اختار التقدير من لوحة الكيبورد:</b>"
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=GRADE_KEYBOARD)
    else:
        text += "<b>اكتب الدرجة الرقمية من 0 إلى 100.</b>"
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardMarkup([["❌ إلغاء"]], resize_keyboard=True))
    return COLLECT


async def collect_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = update.message.text.strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("<b>تم إلغاء الحساب.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN

    idx = context.user_data.get("index", 0)
    mode = context.user_data.get("mode")
    subject = SUBJECTS[idx]

    if mode == "grades":
        if text not in GRADES:
            await update.message.reply_text("<b>اختار تقديرًا من الأزرار فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=GRADE_KEYBOARD)
            return COLLECT
        label_en, min_score, max_score = GRADES[text]
        context.user_data["answers"].append(
            {
                "subject_en": subject.en,
                "subject_ar": subject.ar,
                "credits": subject.credits,
                "grade_ar": text,
                "grade_en": label_en,
                "min_score": min_score,
                "max_score": max_score,
            }
        )
    elif mode == "scores":
        raw = text.replace("%", "")
        try:
            score = float(raw)
        except ValueError:
            await update.message.reply_text("<b>اكتب رقمًا فقط من 0 إلى 100.</b>", parse_mode=ParseMode.HTML)
            return COLLECT
        if score < 0 or score > 100:
            await update.message.reply_text("<b>الدرجة يجب أن تكون بين 0 و 100.</b>", parse_mode=ParseMode.HTML)
            return COLLECT
        context.user_data["answers"].append(
            {
                "subject_en": subject.en,
                "subject_ar": subject.ar,
                "credits": subject.credits,
                "score": score,
            }
        )
    else:
        clear_calc(context)
        await update.message.reply_text("<b>صار خطأ بسيط. ابدأ من جديد.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN

    context.user_data["index"] = idx + 1
    if context.user_data["index"] >= len(SUBJECTS):
        return await finish_calculation(update, context)
    return await ask_current_subject(update, context)


def calculate_result(answers: List[dict], mode: str) -> dict:
    if mode == "grades":
        min_total = sum(item["min_score"] * item["credits"] for item in answers)
        max_total = sum(item["max_score"] * item["credits"] for item in answers)
        min_avg = min_total / TOTAL_CREDITS
        max_avg = max_total / TOTAL_CREDITS
        avg_avg = (min_avg + max_avg) / 2
        return {
            "min_avg": min_avg,
            "avg_avg": avg_avg,
            "max_avg": max_avg,
            "min_contribution": min_avg * STAGE_WEIGHT_PERCENT / 100,
            "avg_contribution": avg_avg * STAGE_WEIGHT_PERCENT / 100,
            "max_contribution": max_avg * STAGE_WEIGHT_PERCENT / 100,
        }
    total = sum(item["score"] * item["credits"] for item in answers)
    avg = total / TOTAL_CREDITS
    return {"avg": avg, "contribution": avg * STAGE_WEIGHT_PERCENT / 100}


def build_summary_text(result: dict, mode: str) -> str:
    if mode == "grades":
        return (
            "<b>✅ تم حساب نتيجتك التقريبية</b>\n\n"
            f"<b>معدل المرحلة الأولى:</b> {result['min_avg']:.2f}% - {result['max_avg']:.2f}%\n"
            f"<b>المعدل الوسطي التقريبي:</b> {result['avg_avg']:.2f}%\n"
            f"<b>مساهمة المرحلة الأولى في التراكمي النهائي:</b> {result['min_contribution']:.2f}% - {result['max_contribution']:.2f}%\n"
            "<b>ملاحظة:</b> هذه النسبة من الدرجة النهائية الكلية، وليست قسمة على 5.\n\n"
            "<b>تم إرسال تقرير PDF باسمك.</b>"
        )
    return (
        "<b>✅ تم حساب نتيجتك حسب الدرجات الرقمية</b>\n\n"
        f"<b>معدل المرحلة الأولى:</b> {result['avg']:.2f}%\n"
        f"<b>مساهمة المرحلة الأولى في التراكمي النهائي:</b> {result['contribution']:.2f}%\n"
        "<b>ملاحظة:</b> هذه النسبة من الدرجة النهائية الكلية، وليست قسمة على 5.\n\n"
        "<b>تم إرسال تقرير PDF باسمك.</b>"
    )


async def finish_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers = context.user_data["answers"]
    mode = context.user_data["mode"]
    student_name = context.user_data.get("student_name", "student")
    result = calculate_result(answers, mode)
    pdf_path, send_filename = create_pdf_report(student_name, answers, result, mode, update)
    increment_calculation(update)
    record_report(update, student_name, pdf_path, send_filename, mode, result)

    summary = build_summary_text(result, mode)
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    with open(pdf_path, "rb") as f:
        await update.message.reply_document(document=f, filename=send_filename, caption="<b>تقريرك بصيغة PDF ✅</b>", parse_mode=ParseMode.HTML)
    clear_calc(context)
    return MAIN


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    user = update.effective_user
    if not user:
        return MAIN
    text = (
        "<b>رقم حسابك في Telegram:</b>\n"
        f"<code>{user.id}</code>\n\n"
        "أضف هذا الرقم في Railway داخل Variables باسم <b>ADMIN_IDS</b>."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    if not get_admin_ids():
        await update.effective_message.reply_text(
            "<b>لوحة الأدمن غير مفعلة بعد.</b>\n"
            "اكتب /myid وخذ الرقم، ثم أضفه في Railway Variables باسم ADMIN_IDS.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard_for(update),
        )
        return MAIN
    if not update.effective_user or not is_admin_user(update.effective_user.id):
        await update.effective_message.reply_text("<b>هذا القسم خاص بمدير البوت فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    await update.effective_message.reply_text("<b>🛠 لوحة الأدمن الخاصة بك</b>", parse_mode=ParseMode.HTML, reply_markup=ADMIN_KEYBOARD)
    return MAIN


def require_admin(update: Update) -> bool:
    return bool(update.effective_user and is_admin_user(update.effective_user.id))


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("<b>هذا القسم خاص بمدير البوت فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    s = get_admin_stats()
    text = (
        "<b>📊 الإحصائية الكاملة للبوت</b>\n\n"
        f"<b>إجمالي المستخدمين:</b> {s['total_users']}\n"
        f"<b>أسماء طلاب محفوظة:</b> {s['named_users']}\n"
        f"<b>نشطون آخر 24 ساعة:</b> {s['active_24h']}\n"
        f"<b>نشطون آخر 7 أيام:</b> {s['active_7d']}\n"
        f"<b>عدد عمليات الحساب:</b> {s['total_calcs']}\n"
        f"<b>إجمالي ملفات PDF:</b> {s['reports_total']}\n"
        f"<b>ملفات PDF آخر 24 ساعة:</b> {s['reports_24h']}\n\n"
        "<b>حالة الوصول بعد آخر فحص:</b>\n"
        f"• قابلون للوصول: <b>{s['reachable']}</b>\n"
        f"• حاذفون/حاظرون البوت: <b>{s['blocked']}</b>\n"
        f"• غير مفحوصين: <b>{s['unknown']}</b>\n\n"
        "اضغط <b>🔎 فحص حالة المستخدمين</b> لتحديث حالة الوصول."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ADMIN_KEYBOARD)
    return MAIN


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("<b>هذا القسم خاص بمدير البوت فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    path = build_users_txt()
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename="kmc_b27_users_full.txt", caption="<b>قائمة المستخدمين الكاملة خاصة بالأدمن فقط.</b>", parse_mode=ParseMode.HTML)
    try:
        os.remove(path)
    except OSError:
        pass
    return MAIN


async def admin_reports_24h(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("<b>هذا القسم خاص بمدير البوت فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    zip_path = build_reports_zip_24h()
    if not zip_path:
        await update.message.reply_text("<b>لا توجد ملفات PDF محفوظة خلال آخر 24 ساعة.</b>", parse_mode=ParseMode.HTML, reply_markup=ADMIN_KEYBOARD)
        return MAIN
    with open(zip_path, "rb") as f:
        await update.message.reply_document(document=f, filename=os.path.basename(zip_path), caption="<b>ملفات PDF المرسلة خلال آخر 24 ساعة.</b>", parse_mode=ParseMode.HTML)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return MAIN


async def admin_check_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("<b>هذا القسم خاص بمدير البوت فقط.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
        return MAIN
    rows = get_all_users()
    await update.message.reply_text("<b>بدأ فحص حالة المستخدمين. انتظر قليلًا...</b>", parse_mode=ParseMode.HTML, reply_markup=ADMIN_KEYBOARD)
    reachable = 0
    blocked = 0
    failed = 0
    for row in rows:
        tid = row[0]
        try:
            await context.bot.send_chat_action(chat_id=tid, action=ChatAction.TYPING)
            update_user_status(tid, "reachable")
            reachable += 1
        except Forbidden:
            update_user_status(tid, "blocked")
            blocked += 1
        except TelegramError:
            update_user_status(tid, "unknown")
            failed += 1
    text = (
        "<b>انتهى فحص حالة المستخدمين ✅</b>\n\n"
        f"<b>قابلون للوصول:</b> {reachable}\n"
        f"<b>حاذفون/حاظرون البوت:</b> {blocked}\n"
        f"<b>غير معروف:</b> {failed}\n\n"
        "ملاحظة: تيليگرام لا يعطي حالة online مباشرة للبوتات؛ هذا الفحص يحدد قابلية الوصول فقط."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ADMIN_KEYBOARD)
    return MAIN


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = update.message.text.strip()
    if text == "🧮 حساب المعدل":
        return await begin_calculation(update, context)
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
    if text == "🔙 رجوع":
        return await start(update, context)
    await update.message.reply_text("<b>اختار من أزرار الكيبورد بالأسفل.</b>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    init_db()
    persistence = PicklePersistence(filepath=os.path.join(DATA_DIR, "bot_state.pickle"))
    application = Application.builder().token(token).persistence(persistence).post_init(post_init).build()

    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("admin", admin_panel))

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("calculate", begin_calculation), CommandHandler("rename", ask_name)],
        states={
            MAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name)],
            MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_mode)],
            COLLECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_answer)],
        },
        fallbacks=[
            CommandHandler("reset", reset_command),
            CommandHandler("help", help_command),
            CommandHandler("list", list_command),
            CommandHandler("start", start),
            CommandHandler("myid", myid_command),
            CommandHandler("admin", admin_panel),
        ],
        name="kmc_b27_final_conversation",
        persistent=True,
    )

    application.add_handler(conv)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("reset", reset_command))
    logger.info("KMC B27 Grade Calculator Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
