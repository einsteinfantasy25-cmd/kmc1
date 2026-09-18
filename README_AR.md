# KMC Grade Calculator — النسخة النهائية

هذه النسخة توسّع البوت إلى المراحل 1–6 مع الحفاظ على **المرحلة الأولى كما كانت في البوت الأصلي**: 15 مادة، 36 Credits، ووزن 5%.

## أهم الميزات
- Stage 1 محفوظة كما هي من النسخة الأصلية.
- Stage 2 = 41 Credits، Stage 3 = 43، Stage 4 = 47، Stage 5 = 51، Stage 6 = 48.
- أوزان التخرج: 5%، 5%، 5%، 20%، 25%، 40%.
- حساب بالتقديرات (تقريبي كنطاق) أو بالدرجات الرقمية.
- حساب تراكمي حتى المرحلة الحالية، مع استخدام النتائج الرقمية المحفوظة تلقائيًا عند توفرها.
- PostgreSQL خارجي عبر `DATABASE_URL`، مع SQLite كخيار احتياطي محلي.
- لوحة أدمن: إحصائيات، مستخدمون، تقارير، فحص حالة، حظر/رفع حظر، إعلانات للجميع أو حسب المرحلة، ووضع صيانة ON/OFF.
- تقارير HTML محفوظة داخل قاعدة البيانات أيضًا، لذلك لا تعتمد على قرص Render المؤقت.
- Health endpoint على `/health` مناسب لـ Render Web Service.

## متغيرات Render المطلوبة
1. `BOT_TOKEN` — توكن جديد وآمن للبوت.
2. `ADMIN_IDS` — Telegram ID للأدمن. يمكن وضع أكثر من ID بفاصلة.
3. `DATABASE_URL` — رابط PostgreSQL الخارجي. إن تركته فارغًا سيستخدم SQLite محليًا، وهو غير مناسب كحفظ دائم على Render Free.
4. `PYTHON_VERSION=3.12.11` — موجود في `render.yaml`.

## إعداد Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Health Check Path: `/health`

## PostgreSQL
لا تحتاج إنشاء الجداول يدويًا. أول تشغيل ينشئ/يحدّث الجداول تلقائيًا:
- `users`
- `reports`
- `stage_results`
- `settings`
- `broadcasts`
- `cumulative_history`

## تنبيه أمني
إذا ظهر BOT_TOKEN في أي صورة أو Log سابقًا، استخدم BotFather لإلغائه وإصدار توكن جديد قبل النشر.
