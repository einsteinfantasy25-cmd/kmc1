import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# KMC Stage 1 Grade Calculator Bot
# Al-Kindy College of Medicine - Year 1
# Calculation model:
# Stage average = sum(score * credits) / 36
# Stage 1 contribution to final cumulative grade = stage_average * 0.05

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MODE, COLLECT = range(2)
TOTAL_CREDITS = 36
STAGE_WEIGHT_PERCENT = 5


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
    "excellent": ("امتياز", 90, 100),
    "very_good": ("جيد جدًا", 80, 89),
    "good": ("جيد", 70, 79),
    "fair": ("متوسط", 60, 69),
    "pass": ("مقبول", 50, 59),
    "fail": ("راسب", 0, 49),
}


def subject_title(subject: Subject) -> str:
    return f"{subject.en}\n{subject.ar}\nCredits: {subject.credits}"


def clear_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ["mode", "index", "answers"]:
        context.user_data.pop(key, None)


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("أحسب بالتقديرات", callback_data="mode:grades")],
            [InlineKeyboardButton("أحسب بالدرجات الرقمية", callback_data="mode:scores")],
        ]
    )


def grade_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("امتياز", callback_data="grade:excellent"), InlineKeyboardButton("جيد جدًا", callback_data="grade:very_good")],
        [InlineKeyboardButton("جيد", callback_data="grade:good"), InlineKeyboardButton("متوسط", callback_data="grade:fair")],
        [InlineKeyboardButton("مقبول", callback_data="grade:pass"), InlineKeyboardButton("راسب", callback_data="grade:fail")],
        [InlineKeyboardButton("إلغاء الحساب", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(rows)


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "بدء استخدام البوت"),
        BotCommand("calculate", "حساب معدل المرحلة الأولى"),
        BotCommand("list", "عرض المواد والكردتات"),
        BotCommand("help", "شرح طريقة الحساب"),
        BotCommand("about", "معلومات عن البوت"),
        BotCommand("reset", "إعادة الحساب من البداية"),
    ]
    await application.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_session(context)
    text = (
        "أهلًا بك في بوت حساب معدل المرحلة الأولى\n"
        "كلية طب الكندي - دفعة 99\n\n"
        "يعتمد الحساب على 15 مادة و 36 credits.\n"
        "نسبة المرحلة الأولى من التراكمي النهائي = 5%.\n\n"
        "اختَر طريقة الحساب:"
    )
    await update.message.reply_text(text, reply_markup=mode_keyboard())
    return MODE


async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await start(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "طريقة الحساب:\n\n"
        "1) إذا اخترت التقديرات، البوت يحسب أقل وأعلى معدل ممكن، لأن جيد جدًا مثلًا تعني من 80 إلى 89.\n"
        "2) إذا اخترت الدرجات الرقمية، البوت يعطي نتيجة دقيقة حسب الدرجات التي تدخلها.\n\n"
        "القانون:\n"
        "معدل المرحلة = مجموع (درجة المادة × كردت المادة) ÷ 36\n"
        "مساهمة المرحلة بالتراكمي = معدل المرحلة × 0.05\n\n"
        "الأوامر:\n"
        "/calculate - بدء الحساب\n"
        "/list - عرض المواد\n"
        "/reset - إلغاء وإعادة من البداية"
    )
    await update.message.reply_text(text)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "KMC Stage 1 Grade Calculator\n"
        "يعتمد على 15 مادة من مرحلة أولى و 36 credits.\n"
        "لا يخزن البوت درجاتك بعد انتهاء المحادثة، والنتيجة للتقدير والمساعدة فقط."
    )
    await update.message.reply_text(text)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["مواد المرحلة الأولى الداخلة في هذا البوت:", ""]
    for i, s in enumerate(SUBJECTS, start=1):
        lines.append(f"{i}. {s.en} - {s.ar} ({s.credits} cr)")
    lines.append("")
    lines.append(f"المجموع = {TOTAL_CREDITS} credits")
    await update.message.reply_text("\n".join(lines))


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_session(context)
    await update.message.reply_text("تمت إعادة الحساب. اكتب /calculate للبدء من جديد.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        clear_session(context)
        await query.edit_message_text("تم إلغاء الحساب. اكتب /calculate للبدء من جديد.")
        return ConversationHandler.END

    if query.data not in ["mode:grades", "mode:scores"]:
        await query.edit_message_text("اختيار غير صحيح. اكتب /calculate وابدأ من جديد.")
        return ConversationHandler.END

    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode
    context.user_data["index"] = 0
    context.user_data["answers"] = []

    if mode == "grades":
        await ask_grade(query, context)
    else:
        await ask_score(query, context)
    return COLLECT


async def ask_grade(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    idx = context.user_data["index"]
    subject = SUBJECTS[idx]
    text = (
        f"المادة {idx + 1} من {len(SUBJECTS)}\n\n"
        f"{subject_title(subject)}\n\n"
        "اختر التقدير:"
    )
    await query.edit_message_text(text, reply_markup=grade_keyboard())


async def ask_score(query_or_update, context: ContextTypes.DEFAULT_TYPE) -> None:
    idx = context.user_data["index"]
    subject = SUBJECTS[idx]
    text = (
        f"المادة {idx + 1} من {len(SUBJECTS)}\n\n"
        f"{subject_title(subject)}\n\n"
        "اكتب الدرجة الرقمية من 0 إلى 100، مثال: 86"
    )

    if hasattr(query_or_update, "edit_message_text"):
        await query_or_update.edit_message_text(text)
    else:
        await query_or_update.message.reply_text(text)


async def collect_grade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        clear_session(context)
        await query.edit_message_text("تم إلغاء الحساب. اكتب /calculate للبدء من جديد.")
        return ConversationHandler.END

    if not query.data.startswith("grade:"):
        await query.edit_message_text("اختيار غير صحيح. اكتب /calculate وابدأ من جديد.")
        return ConversationHandler.END

    grade_key = query.data.split(":", 1)[1]
    if grade_key not in GRADES:
        await query.edit_message_text("تقدير غير معروف. اكتب /calculate وابدأ من جديد.")
        return ConversationHandler.END

    idx = context.user_data["index"]
    subject = SUBJECTS[idx]
    label, min_score, max_score = GRADES[grade_key]
    context.user_data["answers"].append(
        {
            "subject": subject.key,
            "subject_en": subject.en,
            "subject_ar": subject.ar,
            "credits": subject.credits,
            "grade_label": label,
            "min_score": min_score,
            "max_score": max_score,
        }
    )
    context.user_data["index"] = idx + 1

    if context.user_data["index"] >= len(SUBJECTS):
        result = build_grade_result(context.user_data["answers"])
        clear_session(context)
        await query.edit_message_text(result, parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    await ask_grade(query, context)
    return COLLECT


async def collect_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().replace("%", "")
    try:
        score = float(text)
    except ValueError:
        await update.message.reply_text("اكتب رقم فقط من 0 إلى 100، مثال: 86")
        return COLLECT

    if score < 0 or score > 100:
        await update.message.reply_text("الدرجة لازم تكون بين 0 و 100.")
        return COLLECT

    idx = context.user_data["index"]
    subject = SUBJECTS[idx]
    context.user_data["answers"].append(
        {
            "subject": subject.key,
            "subject_en": subject.en,
            "subject_ar": subject.ar,
            "credits": subject.credits,
            "score": score,
        }
    )
    context.user_data["index"] = idx + 1

    if context.user_data["index"] >= len(SUBJECTS):
        result = build_score_result(context.user_data["answers"])
        clear_session(context)
        await update.message.reply_text(result, parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    await ask_score(update, context)
    return COLLECT


def build_grade_result(answers: List[dict]) -> str:
    min_total = sum(item["min_score"] * item["credits"] for item in answers)
    max_total = sum(item["max_score"] * item["credits"] for item in answers)
    min_avg = min_total / TOTAL_CREDITS
    max_avg = max_total / TOTAL_CREDITS
    min_contribution = min_avg * STAGE_WEIGHT_PERCENT / 100
    max_contribution = max_avg * STAGE_WEIGHT_PERCENT / 100

    lines = [
        "<b>نتيجة حسابك التقريبية</b>",
        "",
        f"معدل المرحلة الأولى المتوقع: <b>{min_avg:.2f}% - {max_avg:.2f}%</b>",
        f"مساهمتك بالتراكمي النهائي: <b>{min_contribution:.2f} - {max_contribution:.2f}</b> من أصل 5",
        "",
        "<b>التفاصيل:</b>",
    ]
    for item in answers:
        lines.append(f"- {item['subject_en']} ({item['credits']} cr): {item['grade_label']} = {item['min_score']}-{item['max_score']}")
    lines.extend(
        [
            "",
            "ملاحظة: النتيجة تقريبية لأنك أدخلت تقديرات وليس درجات رقمية.",
            "لنتيجة أدق، أعد الحساب واختر الدرجات الرقمية.",
        ]
    )
    return "\n".join(lines)


def build_score_result(answers: List[dict]) -> str:
    total = sum(item["score"] * item["credits"] for item in answers)
    avg = total / TOTAL_CREDITS
    contribution = avg * STAGE_WEIGHT_PERCENT / 100

    lines = [
        "<b>نتيجة حسابك الدقيقة حسب الدرجات المدخلة</b>",
        "",
        f"معدل المرحلة الأولى: <b>{avg:.2f}%</b>",
        f"مساهمتك بالتراكمي النهائي: <b>{contribution:.2f}</b> من أصل 5",
        "",
        "<b>التفاصيل:</b>",
    ]
    for item in answers:
        lines.append(f"- {item['subject_en']} ({item['credits']} cr): {item['score']:.2f}")
    lines.append("")
    lines.append("القانون: مجموع (درجة المادة × الكردت) ÷ 36")
    return "\n".join(lines)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    persistence = PicklePersistence(filepath="bot_state.pickle")
    application = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("calculate", calculate)],
        states={
            MODE: [CallbackQueryHandler(choose_mode)],
            COLLECT: [
                CallbackQueryHandler(collect_grade, pattern="^(grade:|cancel$)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_score),
            ],
        },
        fallbacks=[
            CommandHandler("reset", reset_command),
            CommandHandler("cancel", reset_command),
            CommandHandler("start", start),
            CommandHandler("calculate", calculate),
        ],
        name="kmc_stage1_conversation",
        persistent=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("reset", reset_command))

    logger.info("KMC Stage 1 Grade Calculator Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
