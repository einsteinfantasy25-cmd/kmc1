import csv
import logging
import glob
import os
import re
import sqlite3
import tempfile
import zipfile
import base64
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
    "ضعيف": ("Weak", 0, 49),
    "راسب": ("Weak", 0, 49),  # alias only, not shown to students
}

MAIN_ROWS = [
    ["🧮 حساب المعدل", "📝 إضافة/تغيير الاسم"],
    ["📚 عرض المواد", "📌 عن البوت"],
    ["ℹ️ المساعدة", "🔄 إعادة البداية"],
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
    [["امتياز", "جيد جدًا"], ["جيد", "متوسط"], ["مقبول", "ضعيف"], ["❌ إلغاء"]],
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
            archive_name = f"{idx:03d}_{created_at.replace(':', '-').replace(' ', '_')}_{safe_student}{os.path.splitext(path)[1] or '.html'}"
            zf.write(path, archive_name)
            summary_lines.append(f"{idx}. {student_name} | @{username if username else 'no_username'} | ID {tid} | {created_at} | {summary} | {archive_name}")
        zf.writestr("reports_summary.txt", "\n".join(summary_lines))
    return zip_path


# -------------------------- HTML REPORT --------------------------
# Alternative reporting method: send a clean HTML report file instead of generating a PDF/image.
# Browsers render Arabic names correctly, so this avoids font/rendering problems on Railway.

HTML_NAVY = "#0D1F44"
HTML_BLUE = "#2F5DA8"
HTML_SOFT = "#F5F8FD"
HTML_ROW = "#EAF0F8"
HTML_LINE = "#D9E2F0"
HTML_TEXT = "#111827"
HTML_MUTED = "#5D6678"


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


def build_html_subject_rows(answers: List[dict], mode: str) -> str:
    rows = []
    for item in answers:
        if mode == "grades":
            cmin = item["min_score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            cmax = item["max_score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            values = [item["subject_en"], str(item["credits"]), item["grade_en"], f"{item['min_score']}-{item['max_score']}", f"{cmin:.2f}% - {cmax:.2f}%"]
        else:
            contrib = item["score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            values = [item["subject_en"], str(item["credits"]), f"{item['score']:.2f}", f"{contrib:.2f}%"]
        rows.append("<tr>" + "".join(f"<td>{html_escape_text(v)}</td>" for v in values) + "</tr>")
    return "\n".join(rows)


def create_html_report(student_name: str, answers: List[dict], result: dict, mode: str, update: Update) -> Tuple[str, str]:
    user = update.effective_user
    telegram_id = str(user.id) if user else "unknown"
    send_filename = f"{safe_filename(student_name)}.html"
    storage_filename = f"{safe_filename(student_name)}_{iraq_now().strftime('%Y%m%d_%H%M%S')}_{telegram_id}.html"
    path = os.path.join(REPORTS_DIR, storage_filename)

    report_date = iraq_now().strftime("%Y-%m-%d %H:%M Iraq")
    logo_uri = logo_data_uri()
    logo_html = f'<img class="logo" src="{logo_uri}" alt="Batch 27 Logo">' if logo_uri else ""

    if mode == "grades":
        result_cells = f"""
        <tr><th>Metric</th><th>Minimum</th><th>Middle</th><th>Maximum</th></tr>
        <tr><td>Stage average</td><td>{result['min_avg']:.2f}%</td><td>{result['avg_avg']:.2f}%</td><td>{result['max_avg']:.2f}%</td></tr>
        <tr><td>Cumulative impact</td><td>{result['min_contribution']:.2f}%</td><td>{result['avg_contribution']:.2f}%</td><td>{result['max_contribution']:.2f}%</td></tr>
        """
        subject_headers = "<tr><th>Subject</th><th>Cr</th><th>Grade</th><th>Range</th><th>Impact</th></tr>"
        note = "Grade categories give a range. Stage 1 impact is shown as percent of the final cumulative grade."
    else:
        result_cells = f"""
        <tr><th>Metric</th><th>Result</th></tr>
        <tr><td>Stage average</td><td>{result['avg']:.2f}%</td></tr>
        <tr><td>Cumulative impact</td><td>{result['contribution']:.2f}%</td></tr>
        """
        subject_headers = "<tr><th>Subject</th><th>Cr</th><th>Score</th><th>Impact</th></tr>"
        note = "Numeric scores were used. Stage 1 impact is shown as percent of the final cumulative grade."

    rows_html = build_html_subject_rows(answers, mode)
    student_name_html = html_escape_text(student_name)
    result_cells = result_cells.strip()

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KMC B27 Grade Report - {student_name_html}</title>
<style>
  :root {{ --navy:{HTML_NAVY}; --blue:{HTML_BLUE}; --soft:{HTML_SOFT}; --row:{HTML_ROW}; --line:{HTML_LINE}; --text:{HTML_TEXT}; --muted:{HTML_MUTED}; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#eef2f7; color:var(--text); font-family: Tahoma, Arial, "Noto Naskh Arabic", "Noto Sans Arabic", sans-serif; font-weight:700; }}
  .page {{ width:210mm; min-height:297mm; margin:18px auto; background:white; border:2px solid var(--navy); border-radius:8px; padding:10mm 9mm 8mm; position:relative; overflow:hidden; }}
  .header {{ border:1.3px solid var(--line); border-radius:10px; padding:10px 14px 9px; display:grid; grid-template-columns:1fr 118px; align-items:center; gap:12px; border-bottom:5px solid var(--blue); }}
  h1 {{ margin:0; color:var(--navy); font-size:28px; letter-spacing:.3px; line-height:1.05; }}
  .subtitle {{ margin-top:6px; color:var(--navy); font-size:14.5px; line-height:1.35; }}
  .logo {{ max-width:115px; max-height:78px; object-fit:contain; justify-self:end; }}
  .info {{ margin-top:10px; border:1.3px solid var(--line); border-radius:10px; overflow:hidden; display:grid; grid-template-columns:1fr 1fr; }}
  .info div {{ padding:9px 12px; border-bottom:1px solid var(--line); min-height:46px; }}
  .info div:nth-child(odd) {{ border-right:1px solid var(--line); }}
  .label {{ color:var(--muted); font-size:11.5px; display:block; margin-bottom:4px; }}
  .value {{ color:var(--navy); font-size:15px; line-height:1.25; }}
  .student-name {{ font-size:17px; direction:auto; unicode-bidi:plaintext; }}
  .section-row {{ margin:12px 0 6px; display:flex; align-items:flex-end; justify-content:space-between; gap:10px; }}
  .section-title {{ margin:0; color:var(--navy); font-size:20px; }}
  .note {{ margin:0; color:var(--muted); font-size:10.5px; line-height:1.35; text-align:right; max-width:72%; }}
  table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
  th {{ background:var(--navy); color:white; padding:7px 6px; font-size:11px; text-align:center; line-height:1.1; }}
  td {{ padding:6px 6px; border:1px solid white; font-size:11px; text-align:center; line-height:1.15; }}
  td:first-child, th:first-child {{ text-align:left; }}
  tbody tr:nth-child(even) td {{ background:var(--row); }}
  tbody tr:nth-child(odd) td {{ background:#fbfdff; }}
  .result-table td {{ font-size:13px; padding:8px 6px; }}
  .result-table td:first-child {{ color:var(--navy); }}
  .details-title {{ margin:12px 0 6px; color:var(--navy); font-size:16px; }}
  .details-table th:nth-child(1) {{ width:39%; }} .details-table th:nth-child(2) {{ width:7%; }} .details-table th:nth-child(3) {{ width:17%; }} .details-table th:nth-child(4) {{ width:17%; }} .details-table th:nth-child(5) {{ width:20%; }}
  .details-table td {{ font-size:10.2px; padding:6px 5px; }}
  .footer {{ position:absolute; left:9mm; right:9mm; bottom:5.5mm; color:var(--muted); font-size:9.5px; text-align:center; border-top:1px solid var(--line); padding-top:6px; }}
  .print-button {{ position:fixed; right:18px; bottom:18px; background:var(--navy); color:white; padding:12px 16px; border-radius:999px; text-decoration:none; font:700 14px Arial; box-shadow:0 6px 22px rgba(0,0,0,.20); }}
  @media print {{ body {{ background:white; }} .page {{ margin:0; border-radius:0; box-shadow:none; }} .print-button {{ display:none; }} }}
  @page {{ size:A4; margin:0; }}
</style>
</head>
<body>
<a class="print-button" href="javascript:window.print()">Print / Save PDF</a>
<section class="page">
  <div class="header"><div><h1>KMC B27 GRADE CALCULATOR</h1><div class="subtitle">Stage 1 Grade Report | Al-Kindy College of Medicine<br>Batch 27 | Developed by Osama</div></div>{logo_html}</div>
  <div class="info">
    <div><span class="label">Student Name</span><span class="value student-name"><bdi dir="auto">{student_name_html}</bdi></span></div>
    <div><span class="label">Report Date</span><span class="value">{report_date}</span></div>
    <div><span class="label">Stage</span><span class="value">First Year</span></div>
    <div><span class="label">College</span><span class="value">Al-Kindy Medicine</span></div>
  </div>
  <div class="section-row"><h2 class="section-title">Final Result</h2><p class="note">{html_escape_text(note)}</p></div>
  <table class="result-table"><tbody>{result_cells}</tbody></table>
  <h2 class="details-title">Subject Details</h2>
  <table class="details-table"><thead>{subject_headers}</thead><tbody>{rows_html}</tbody></table>
  <div class="footer">Cumulative impact is a percentage of the final cumulative grade, not a division by 5. This report is automatically generated and is not an official college transcript.<br>Developed by Osama | Iraq time</div>
</section>
</body>
</html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path, send_filename

# -------------------------- Telegram handlers --------------------------
# -------------------------- Telegram handlers --------------------------

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("calculate", "حساب المعدل"),
        BotCommand("rename", "إضافة أو تغيير الاسم"),
        BotCommand("list", "عرض المواد"),
        BotCommand("help", "شرح طريقة الحساب"),
        BotCommand("about", "عن البوت"),
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
        "<b>بعد إكمال الحساب، يرسل البوت تقرير HTML باسم الطالب.</b>"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = (
        "<b>📌 عن البوت</b>\n\n"
        "هذا البوت يحسب <b>معدل المرحلة الأولى</b> لطلاب <b>كلية طب الكندي - Batch 27</b> حسب الكردتات.\n\n"
        "<b>ماذا يفعل؟</b>\n"
        "• يحسب المعدل بالتقديرات أو بالدرجات الرقمية.\n"
        "• يوضح تأثير المرحلة الأولى في التراكمي النهائي كنسبة مئوية.\n"
        "• يرسل تقريرًا مرتبًا باسم الطالب بدون إظهار معلومات Telegram داخل التقرير.\n\n"
        "<b>Developer:</b> Osama"
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
            "<b>قبل الحساب، يجب إضافة اسم الطالب الثلاثي حتى يظهر داخل تقرير HTML.</b>",
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
            "<b>تم إرسال تقرير HTML باسمك.</b>"
        )
    return (
        "<b>✅ تم حساب نتيجتك حسب الدرجات الرقمية</b>\n\n"
        f"<b>معدل المرحلة الأولى:</b> {result['avg']:.2f}%\n"
        f"<b>مساهمة المرحلة الأولى في التراكمي النهائي:</b> {result['contribution']:.2f}%\n"
        "<b>ملاحظة:</b> هذه النسبة من الدرجة النهائية الكلية، وليست قسمة على 5.\n\n"
        "<b>تم إرسال تقرير HTML باسمك.</b>"
    )


async def finish_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers = context.user_data["answers"]
    mode = context.user_data["mode"]
    student_name = context.user_data.get("student_name", "student")
    result = calculate_result(answers, mode)
    report_path, send_filename = create_html_report(student_name, answers, result, mode, update)
    increment_calculation(update)
    record_report(update, student_name, report_path, send_filename, mode, result)

    summary = build_summary_text(result, mode)
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    with open(report_path, "rb") as f:
        await update.message.reply_document(document=f, filename=send_filename, caption="<b>تقريرك جاهز كملف HTML ✅</b>\nافتح الملف من الهاتف. إذا أردته PDF اضغط Print / Save PDF داخل الملف.", parse_mode=ParseMode.HTML)
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
        f"<b>إجمالي التقارير:</b> {s['reports_total']}\n"
        f"<b>تقارير آخر 24 ساعة:</b> {s['reports_24h']}\n\n"
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
        await update.message.reply_text("<b>لا توجد تقارير محفوظة خلال آخر 24 ساعة.</b>", parse_mode=ParseMode.HTML, reply_markup=ADMIN_KEYBOARD)
        return MAIN
    with open(zip_path, "rb") as f:
        await update.message.reply_document(document=f, filename=os.path.basename(zip_path), caption="<b>التقارير المرسلة خلال آخر 24 ساعة.</b>", parse_mode=ParseMode.HTML)
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
    if text == "📌 عن البوت":
        return await about_command(update, context)
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
    application.add_handler(CommandHandler("about", about_command))

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
            CommandHandler("about", about_command),
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
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("reset", reset_command))
    logger.info("KMC B27 Grade Calculator Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
