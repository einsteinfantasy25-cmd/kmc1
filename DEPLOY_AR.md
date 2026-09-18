# خطوات الاستبدال على Render

1. ارفع ملفات المشروع كاملة إلى GitHub بدل النسخة القديمة.
2. في Render > Environment ضع:
   - `BOT_TOKEN` = التوكن الجديد.
   - `ADMIN_IDS` = Telegram ID الخاص بك.
   - `DATABASE_URL` = رابط PostgreSQL الخارجي.
   - `PYTHON_VERSION` = `3.12.11` (اختياري لأن `.python-version` و`render.yaml` يحتويانه أيضًا).
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Health Check Path: `/health`
6. نفّذ Clear build cache & deploy مرة واحدة عند استبدال النسخة القديمة.
7. بعد ظهور Live اختبر `/start`، حساب مرحلة ثانية، التراكمي، `/admin`، وضع الصيانة، ثم إعلان تجريبي لحسابك/مجموعة صغيرة قبل الإعلان العام.

## قاعدة البيانات الخارجية

البوت يقرأ `DATABASE_URL` تلقائيًا. إذا كان الرابط موجودًا يستخدم PostgreSQL وينشئ الجداول بنفسه. إذا لم يكن موجودًا يرجع إلى SQLite، وهذا غير مناسب للحفظ الدائم على Render Free.

## مهم

لا تشغّل نسختين من البوت بنفس Telegram token في الوقت نفسه إذا كانتا تستخدمان polling.
