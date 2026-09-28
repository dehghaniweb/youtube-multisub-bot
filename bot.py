async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = clean_url(update.message.text)

    if not url:
        await update.message.reply_text(
            "❌ لینک YouTube معتبر پیدا نشد.\n\n"
            "لطفاً لینک ویدئو را ارسال کن."
        )
        return

    context.user_data["url"] = url

    await update.message.reply_text(
        "⏳ در حال بررسی زیرنویس‌های این ویدئو..."
    )

    try:
        loop = asyncio.get_running_loop()

        def get_subtitles():
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "writesubtitles": False,
                "writeautomaticsub": False,
                "remote_components": ["ejs:github"],
                "extractor_args": {
                    "youtube": {
                        "player_client": [
                            "android_vr",
                            "web_embedded",
                        ]
                    }
                },
            }

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            subtitles = info.get("subtitles") or {}
            automatic = info.get("automatic_captions") or {}

            return subtitles, automatic

        subtitles, automatic = await loop.run_in_executor(
            None,
            get_subtitles
        )

        available = []

        # زیرنویس‌های اصلی ویدئو
        for code, tracks in subtitles.items():
            if tracks:
                available.append((code, "اصلی"))

        # زیرنویس‌های خودکار YouTube
        for code, tracks in automatic.items():
            if tracks and code not in subtitles:
                available.append((code, "خودکار"))

        if not available:
            await update.message.reply_text(
                "⚠️ برای این ویدئو زیرنویسی پیدا نشد."
            )
            return

        # مرتب‌سازی بر اساس کد زبان
        available.sort(key=lambda x: x[0])

        lines = ["🎬 زیرنویس‌های موجود برای این ویدئو:\n"]

        for code, kind in available:
            lines.append(
                f"• {code} — {kind}"
            )

        lines.append(
            "\n🌍 حالا زبان موردنظر را انتخاب کن:"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "🇬🇧 English",
                    callback_data="lang_en"
                ),
                InlineKeyboardButton(
                    "🇮🇷 فارسی",
                    callback_data="lang_fa"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🇳🇱 Nederlands",
                    callback_data="lang_nl"
                ),
                InlineKeyboardButton(
                    "🇩🇪 Deutsch",
                    callback_data="lang_de"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🇸🇦 العربية",
                    callback_data="lang_ar"
                ),
                InlineKeyboardButton(
                    "🇹🇷 Türkçe",
                    callback_data="lang_tr"
                ),
            ],
        ]

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:

        print(f"Subtitle check error: {e}")

        await update.message.reply_text(
            "⚠️ هنگام بررسی زیرنویس‌های ویدئو خطایی رخ داد.\n\n"
            "لطفاً دوباره امتحان کن."
        )
