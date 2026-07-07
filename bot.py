import logging
import os
import re
import sqlite3
import csv
import tempfile
from html import escape
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple, Set, Optional

from telegram import BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:
    arabic_reshaper = None
    get_display = None

# KMC Stage 1 Grade Calculator Bot
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
DB_PATH = os.getenv("USERS_DB_PATH", os.path.join(os.getcwd(), "users.db"))


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

MAIN_ROWS = [["🧮 حساب المعدل", "📝 إضافة/تغيير الاسم"], ["📚 عرض المواد", "ℹ️ المساعدة"], ["🔄 إعادة البداية"]]

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [["📊 إحصائيات", "👥 قائمة المستخدمين"], ["📤 تصدير CSV"], ["🔙 رجوع"]],
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


def init_db() -> None:
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
                last_seen TEXT
            )
            """
        )
        con.commit()


def upsert_user(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> None:
    user = update.effective_user
    if not user:
        return
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    student_name = ""
    if context is not None:
        student_name = context.user_data.get("student_name", "") or ""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO users (telegram_id, first_name, last_name, username, student_name, calculations, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                student_name=CASE WHEN excluded.student_name != '' THEN excluded.student_name ELSE users.student_name END,
                last_seen=excluded.last_seen
            """,
            (user.id, user.first_name or "", user.last_name or "", user.username or "", student_name, now, now),
        )
        con.commit()


def set_student_name_in_db(update: Update, student_name: str) -> None:
    if not update.effective_user:
        return
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO users (telegram_id, first_name, last_name, username, student_name, calculations, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                student_name=excluded.student_name,
                last_seen=excluded.last_seen
            """,
            (update.effective_user.id, update.effective_user.first_name or "", update.effective_user.last_name or "", update.effective_user.username or "", student_name, now, now),
        )
        con.commit()


def increment_calculation(update: Update) -> None:
    if not update.effective_user:
        return
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE users SET calculations = COALESCE(calculations, 0) + 1, last_seen = ? WHERE telegram_id = ?",
            (now, update.effective_user.id),
        )
        con.commit()


def get_admin_stats() -> dict:
    init_db()
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        total_users = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        named_users = cur.execute("SELECT COUNT(*) FROM users WHERE student_name IS NOT NULL AND student_name != ''").fetchone()[0]
        total_calcs = cur.execute("SELECT COALESCE(SUM(calculations), 0) FROM users").fetchone()[0]
        return {"total_users": total_users, "named_users": named_users, "total_calcs": total_calcs}


def get_recent_users(limit: int = 50) -> List[tuple]:
    init_db()
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            """
            SELECT telegram_id, first_name, last_name, username, student_name, calculations, last_seen
            FROM users
            ORDER BY datetime(last_seen) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def export_users_csv() -> str:
    init_db()
    fd, path = tempfile.mkstemp(prefix="kmc_b27_users_", suffix=".csv")
    os.close(fd)
    with sqlite3.connect(DB_PATH) as con, open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["telegram_id", "first_name", "last_name", "username", "student_name", "calculations", "first_seen", "last_seen"])
        for row in con.execute(
            """
            SELECT telegram_id, first_name, last_name, username, student_name, calculations, first_seen, last_seen
            FROM users
            ORDER BY datetime(last_seen) DESC
            """
        ):
            writer.writerow(row)
    return path


def require_admin(update: Update) -> bool:
    return bool(update.effective_user and is_admin_user(update.effective_user.id))


def register_fonts() -> Tuple[str, str]:
    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]

    regular_font = "Helvetica"
    bold_font = "Helvetica-Bold"

    for path in regular_candidates:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("BotFont", path))
            regular_font = "BotFont"
            break

    for path in bold_candidates:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("BotFontBold", path))
            bold_font = "BotFontBold"
            break

    return regular_font, bold_font

PDF_FONT, PDF_FONT_BOLD = register_fonts()


def ar(text: str) -> str:
    if not text:
        return ""
    if arabic_reshaper and get_display:
        try:
            return get_display(arabic_reshaper.reshape(text))
        except Exception:
            return text
    return text


def clear_calc(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ["mode", "index", "answers"]:
        context.user_data.pop(key, None)


def safe_filename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_\-\u0600-\u06FF ]+", "", name).strip().replace(" ", "_")
    return clean[:40] if clean else "student"


def subject_line(subject: Subject) -> str:
    return f"{subject.en}\n{subject.ar}\nCredits: {subject.credits}"


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
        f"أهلًا بك في {BOT_TITLE} 👋\n\n"
        f"اسم الطالب الحالي: {name}\n"
        "اختار من لوحة الكيبورد بالأسفل."
    )
    await update.effective_message.reply_text(text, reply_markup=main_keyboard_for(update))
    return MAIN


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = (
        "طريقة الحساب:\n"
        "• بالتقديرات: يعطي أقل وأعلى معدل ممكن.\n"
        "• بالدرجات الرقمية: يعطي معدل دقيق.\n\n"
        "القانون:\n"
        "معدل المرحلة = مجموع (درجة المادة × الكردت) ÷ 36\n"
        "مساهمة المرحلة بالتراكمي = معدل المرحلة × 0.05\n\n"
        "بعد اكتمال الحساب يرسل البوت تقرير PDF باسم الطالب."
    )
    await update.effective_message.reply_text(text, reply_markup=main_keyboard_for(update))
    return MAIN


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    lines = ["مواد البوت الداخلة بالحساب:", ""]
    for i, s in enumerate(SUBJECTS, start=1):
        lines.append(f"{i}. {s.en} - {s.ar} ({s.credits} cr)")
    lines.append("")
    lines.append(f"المجموع = {TOTAL_CREDITS} credits")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_keyboard_for(update))
    return MAIN


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    clear_calc(context)
    context.user_data.pop("student_name", None)
    await update.effective_message.reply_text("تمت إعادة البداية وحذف الاسم المؤقت.", reply_markup=main_keyboard_for(update))
    return MAIN


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    await update.effective_message.reply_text(
        "اكتب اسم الطالب أو اليوزرنيم الذي تريد يظهر باسم ملف الـ PDF.\nمثال: Osama أو @osama200",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def save_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if name.startswith("/"):
        await update.message.reply_text("اكتب الاسم كنص، مو أمر يبدأ بعلامة /.")
        return ASK_NAME
    # يقبل العربي، الإنكليزي، المسافات، @ والرموز البسيطة.
    if len(name.replace("@", "").strip()) < 2:
        await update.message.reply_text("اكتب اسم واضح أكثر من حرف واحد. يقبل عربي أو إنكليزي.")
        return ASK_NAME
    context.user_data["student_name"] = name
    set_student_name_in_db(update, name)
    await update.message.reply_text(f"تم حفظ الاسم: {name} ✅", reply_markup=main_keyboard_for(update))
    return MAIN


async def begin_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    clear_calc(context)
    if not context.user_data.get("student_name"):
        await update.effective_message.reply_text(
            "قبل الحساب لازم نضيف اسم الطالب حتى يكون اسم ملف التقرير PDF باسمك.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_name(update, context)
    await update.effective_message.reply_text("اختار طريقة الحساب:", reply_markup=MODE_KEYBOARD)
    return MODE


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = update.message.text.strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("تم إلغاء الحساب.", reply_markup=main_keyboard_for(update))
        return MAIN
    if text == "📊 حساب بالتقديرات":
        context.user_data["mode"] = "grades"
    elif text == "🔢 حساب بالدرجات الرقمية":
        context.user_data["mode"] = "scores"
    else:
        await update.message.reply_text("اختار من أزرار الكيبورد فقط.", reply_markup=MODE_KEYBOARD)
        return MODE
    context.user_data["index"] = 0
    context.user_data["answers"] = []
    return await ask_current_subject(update, context)


async def ask_current_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idx = context.user_data["index"]
    subject = SUBJECTS[idx]
    mode = context.user_data["mode"]
    text = f"المادة {idx + 1} من {len(SUBJECTS)}\n\n{subject_line(subject)}\n\n"
    if mode == "grades":
        text += "اختار التقدير من لوحة الكيبورد:"
        await update.effective_message.reply_text(text, reply_markup=GRADE_KEYBOARD)
    else:
        text += "اكتب الدرجة الرقمية من 0 إلى 100، مثال: 86"
        await update.effective_message.reply_text(text, reply_markup=ReplyKeyboardMarkup([["❌ إلغاء"]], resize_keyboard=True))
    return COLLECT


async def collect_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    text = update.message.text.strip()
    if text == "❌ إلغاء":
        clear_calc(context)
        await update.message.reply_text("تم إلغاء الحساب.", reply_markup=main_keyboard_for(update))
        return MAIN

    idx = context.user_data.get("index", 0)
    mode = context.user_data.get("mode")
    subject = SUBJECTS[idx]

    if mode == "grades":
        if text not in GRADES:
            await update.message.reply_text("اختار تقدير من الأزرار فقط.", reply_markup=GRADE_KEYBOARD)
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
            await update.message.reply_text("اكتب رقم فقط من 0 إلى 100، مثال: 86")
            return COLLECT
        if score < 0 or score > 100:
            await update.message.reply_text("الدرجة لازم تكون بين 0 و 100.")
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
        await update.message.reply_text("صار خطأ بسيط. ابدأ من جديد.", reply_markup=main_keyboard_for(update))
        return MAIN

    context.user_data["index"] = idx + 1
    if context.user_data["index"] >= len(SUBJECTS):
        return await finish_calculation(update, context)
    return await ask_current_subject(update, context)


async def finish_calculation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers = context.user_data["answers"]
    mode = context.user_data["mode"]
    student_name = context.user_data.get("student_name", "student")
    result = calculate_result(answers, mode)
    pdf_path = create_pdf_report(student_name, answers, result, mode)
    increment_calculation(update)

    summary = build_summary_text(result, mode)
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    with open(pdf_path, "rb") as f:
        await update.message.reply_document(document=f, filename=os.path.basename(pdf_path), caption="هذا تقريرك بصيغة PDF ✅")
    try:
        os.remove(pdf_path)
    except OSError:
        pass
    clear_calc(context)
    return MAIN


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
            "<b>تم حساب نتيجتك التقريبية ✅</b>\n\n"
            f"معدل المرحلة الأولى: <b>{result['min_avg']:.2f}% - {result['max_avg']:.2f}%</b>\n"
            f"المعدل الوسطي التقريبي: <b>{result['avg_avg']:.2f}%</b>\n"
            f"مساهمتك بالتراكمي: <b>{result['min_contribution']:.2f} - {result['max_contribution']:.2f}</b> من أصل 5\n\n"
            "تم إرسال تقرير PDF باسمك."
        )
    return (
        "<b>تم حساب نتيجتك حسب الدرجات الرقمية ✅</b>\n\n"
        f"معدل المرحلة الأولى: <b>{result['avg']:.2f}%</b>\n"
        f"مساهمتك بالتراكمي: <b>{result['contribution']:.2f}</b> من أصل 5\n\n"
        "تم إرسال تقرير PDF باسمك."
    )


def create_pdf_report(student_name: str, answers: List[dict], result: dict, mode: str) -> str:
    filename = f"{safe_filename(student_name)}.pdf"
    student_name_pdf = ar(student_name)
    path = os.path.join("/tmp", filename)
    doc = SimpleDocTemplate(
        path,
        pagesize=A4,
        rightMargin=1.2 * cm,
        leftMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleBot",
        parent=styles["Title"],
        fontName=PDF_FONT_BOLD,
        fontSize=19,
        leading=22,
        textColor=colors.HexColor("#102348"),
        alignment=0,
    )
    subtitle_style = ParagraphStyle(
        "SubtitleBot",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#102348"),
    )
    normal = ParagraphStyle(
        "NormalBot",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=9.5,
        leading=12.5,
        textColor=colors.HexColor("#111111"),
    )
    small = ParagraphStyle(
        "SmallBot",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor("#333333"),
    )

    navy = colors.HexColor("#102348")
    light = colors.HexColor("#eef2f8")
    mid = colors.HexColor("#dce3ef")

    story = []

    # Header with Batch 27 identity logo
    title_block = [
        Paragraph(REPORT_TITLE, title_style),
        Paragraph("Stage 1 Grade Report - Al-Kindy College of Medicine", subtitle_style),
        Paragraph(f"{BATCH_NAME} | Developed by {DEVELOPER_NAME}", subtitle_style),
    ]
    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=3.2 * cm, height=2.15 * cm, kind="proportional")
        header = Table([[title_block, logo]], colWidths=[12.7 * cm, 3.5 * cm], hAlign="LEFT")
    else:
        header = Table([[title_block]], colWidths=[16.2 * cm], hAlign="LEFT")
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, -1), 1.5, navy),
    ]))
    story.append(header)
    story.append(Spacer(1, 0.25 * cm))

    info_data = [
        ["Name", student_name_pdf, "Stage", "First Year"],
        ["Batch", BATCH_NAME, "Date", datetime.now().strftime("%Y-%m-%d %H:%M")],
        ["College", COLLEGE_NAME, "Calculation", "Credits-based"],
    ]
    info_table = Table(info_data, colWidths=[2.2 * cm, 5.6 * cm, 2.3 * cm, 5.7 * cm], hAlign="LEFT")
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), PDF_FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, -1), light),
        ("TEXTCOLOR", (0, 0), (0, -1), navy),
        ("TEXTCOLOR", (2, 0), (2, -1), navy),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.white),
        ("BOX", (0, 0), (-1, -1), 0.8, navy),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.35 * cm))

    if mode == "grades":
        story.append(Paragraph("Result: Approximate range based on grade categories", normal))
        res_data = [
            ["Minimum", "Average", "Maximum"],
            [f"{result['min_avg']:.2f}%", f"{result['avg_avg']:.2f}%", f"{result['max_avg']:.2f}%"],
            [f"{result['min_contribution']:.2f} / 5", f"{result['avg_contribution']:.2f} / 5", f"{result['max_contribution']:.2f} / 5"],
        ]
    else:
        story.append(Paragraph("Result: Exact calculation based on numeric scores", normal))
        res_data = [["Stage Average", "Cumulative Contribution"], [f"{result['avg']:.2f}%", f"{result['contribution']:.2f} / 5"]]

    result_table = Table(res_data, hAlign="LEFT")
    result_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), PDF_FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("BACKGROUND", (0, 1), (-1, -1), light),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(result_table)
    story.append(Spacer(1, 0.3 * cm))

    if mode == "grades":
        data = [["Subject", "Cr", "Grade", "Min-Max", "Contribution Range"]]
        for item in answers:
            cmin = item["min_score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            cmax = item["max_score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            data.append([item["subject_en"], str(item["credits"]), item["grade_en"], f"{item['min_score']}-{item['max_score']}", f"{cmin:.4f}-{cmax:.4f}"])
    else:
        data = [["Subject", "Cr", "Score", "Weighted Contribution"]]
        for item in answers:
            contrib = item["score"] * item["credits"] / TOTAL_CREDITS * STAGE_WEIGHT_PERCENT / 100
            data.append([item["subject_en"], str(item["credits"]), f"{item['score']:.2f}", f"{contrib:.4f}"])

    table = Table(
        data,
        repeatRows=1,
        colWidths=[6.5 * cm, 1.1 * cm, 2.1 * cm, 2.5 * cm, 3.5 * cm] if mode == "grades" else [7.2 * cm, 1.2 * cm, 2.5 * cm, 4.0 * cm],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), PDF_FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 7.6),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [light, mid]),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 4.6),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.25 * cm))
    story.append(Paragraph("Note: Grade-category calculation is approximate. Numeric scores give the most accurate result.", small))
    story.append(Paragraph(f"Generated automatically by {BOT_TITLE}. Developed by {DEVELOPER_NAME}.", small))
    doc.build(story)
    return path


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    user = update.effective_user
    if not user:
        return MAIN
    text = (
        "رقم حسابك في Telegram هو:\n"
        f"<code>{user.id}</code>\n\n"
        "حتى تظهر لك لوحة الأدمن فقط، أضف هذا الرقم في Railway داخل Variables باسم ADMIN_IDS."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard_for(update))
    return MAIN


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_user(update, context)
    if not get_admin_ids():
        await update.effective_message.reply_text(
            "لوحة الأدمن غير مفعلة بعد.\n"
            "اكتب /myid وخذ الرقم، ثم أضفه في Railway Variables باسم ADMIN_IDS.",
            reply_markup=main_keyboard_for(update),
        )
        return MAIN
    if not require_admin(update):
        await update.effective_message.reply_text("هذا القسم خاص بمدير البوت فقط.", reply_markup=main_keyboard_for(update))
        return MAIN
    await update.effective_message.reply_text("لوحة الأدمن الخاصة بك:", reply_markup=ADMIN_KEYBOARD)
    return MAIN


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("هذا القسم خاص بمدير البوت فقط.", reply_markup=main_keyboard_for(update))
        return MAIN
    stats = get_admin_stats()
    text = (
        "📊 إحصائيات البوت\n\n"
        f"عدد المستخدمين: {stats['total_users']}\n"
        f"عدد الأسماء المحفوظة: {stats['named_users']}\n"
        f"عدد عمليات الحساب: {stats['total_calcs']}"
    )
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return MAIN


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("هذا القسم خاص بمدير البوت فقط.", reply_markup=main_keyboard_for(update))
        return MAIN
    rows = get_recent_users(limit=50)
    if not rows:
        await update.message.reply_text("لا يوجد مستخدمون بعد.", reply_markup=ADMIN_KEYBOARD)
        return MAIN
    lines = ["👥 آخر 50 مستخدم:", ""]
    for i, (tid, first, last, username, student_name, calculations, last_seen) in enumerate(rows, start=1):
        full = " ".join(x for x in [first, last] if x).strip() or "بدون اسم تيليگرام"
        uname = f"@{username}" if username else "بدون يوزر"
        sname = student_name or "لم يضف اسم"
        lines.append(f"{i}. {sname} | {full} | {uname} | ID: {tid} | حسابات: {calculations}")
    text = "\n".join(lines)
    # Telegram message limit safety
    if len(text) > 3900:
        text = text[:3900] + "\n...\nللقائمة الكاملة استخدم زر تصدير CSV."
    await update.message.reply_text(text, reply_markup=ADMIN_KEYBOARD)
    return MAIN


async def admin_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not require_admin(update):
        await update.message.reply_text("هذا القسم خاص بمدير البوت فقط.", reply_markup=main_keyboard_for(update))
        return MAIN
    path = export_users_csv()
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename="kmc_b27_users.csv", caption="ملف المستخدمين CSV")
    try:
        os.remove(path)
    except OSError:
        pass
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
    if text == "📊 إحصائيات":
        return await admin_stats(update, context)
    if text == "👥 قائمة المستخدمين":
        return await admin_users(update, context)
    if text == "📤 تصدير CSV":
        return await admin_export(update, context)
    if text == "🔙 رجوع":
        return await start(update, context)
    await update.message.reply_text("اختار من أزرار الكيبورد بالأسفل.", reply_markup=main_keyboard_for(update))
    return MAIN


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    persistence = PicklePersistence(filepath="bot_state.pickle")
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
        fallbacks=[CommandHandler("reset", reset_command), CommandHandler("help", help_command), CommandHandler("list", list_command), CommandHandler("start", start), CommandHandler("myid", myid_command), CommandHandler("admin", admin_panel)],
        name="kmc_stage1_pdf_conversation",
        persistent=True,
    )

    application.add_handler(conv)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("reset", reset_command))
    logger.info("KMC Grade Calculator Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
