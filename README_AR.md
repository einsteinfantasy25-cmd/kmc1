# KMC Stage 1 Grade Calculator Bot

بوت تيليگرام لحساب معدل المرحلة الأولى لكلية طب الكندي - دفعة 99.

## يعتمد على

- 15 مادة
- مجموع 36 credits
- المرحلة الأولى = 5% من المعدل التراكمي النهائي

## الملفات

- `bot.py`: كود البوت
- `requirements.txt`: مكتبات بايثون المطلوبة
- `render.yaml`: ملف جاهز للنشر على Render كـ Background Worker
- `.env.example`: مثال للمتغير السري BOT_TOKEN

## التشغيل محليًا للتجربة

1. ثبتي Python 3.11 أو أحدث.
2. افتحي Terminal داخل مجلد المشروع.
3. اكتبي:

```bash
pip install -r requirements.txt
```

4. ضعي التوكن كمتغير بيئة:

```bash
export BOT_TOKEN="ضع_التوكن_هنا"
python bot.py
```

في Windows PowerShell:

```powershell
$env:BOT_TOKEN="ضع_التوكن_هنا"
python bot.py
```

## النشر 24 ساعة على Render

1. ارفعي هذه الملفات إلى GitHub repository جديد.
2. افتحي Render.
3. اختاري New ثم Blueprint أو Background Worker.
4. اربطي GitHub repository.
5. ضعي Environment Variable باسم:

```text
BOT_TOKEN
```

والقيمة هي توكن البوت من BotFather.

6. Start Command:

```bash
python bot.py
```

لا تضعي التوكن داخل الكود ولا تنشريه في الكروب.
