import asyncio
import logging
import os
import json
from datetime import datetime
from collections import defaultdict
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════
TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
STATS_FILE = "taxi_stats.json"
BOT_NAME = "TaxiCompare"

# ═══════════════════════════════════════════════════════════════
#  СОСТОЯНИЯ
# ═══════════════════════════════════════════════════════════════
(
    MAIN_MENU,
    GET_FROM,
    GET_TO,
    SHOW_LINKS,
    ENTER_PRICES,
    ENTER_YANDEX,
    ENTER_INDRIVE,
    ENTER_BOLT,
    ENTER_YANGO,
    ENTER_UBER,
) = range(10)

# ═══════════════════════════════════════════════════════════════
#  DEEPLINKS ДЛЯ ТАКСИ СЕРВИСОВ
# ═══════════════════════════════════════════════════════════════
def get_taxi_links(from_addr: str, to_addr: str) -> dict:
    """Генерируем deeplinks для каждого такси-сервиса"""
    from_enc = from_addr.replace(" ", "+")
    to_enc = to_addr.replace(" ", "+")

    return {
        "yandex": {
            "name": "Яндекс Такси",
            "emoji": "🚖",
            "color": "жёлтый",
            "url": f"https://3.redirect.appmetrica.yandex.com/route?start-lat=&start-lon=&end-lat=&end-lon=&ref=taxi_aggregator",
            "web": f"https://taxi.yandex.kz",
            "description": "Эконом / Комфорт / Бизнес"
        },
        "indrive": {
            "name": "inDrive",
            "emoji": "🚗",
            "color": "зелёный",
            "url": f"https://indrive.com/deeplink/order?from={from_enc}&to={to_enc}",
            "web": f"https://indrive.com",
            "description": "Торг с водителем"
        },
        "bolt": {
            "name": "Bolt",
            "emoji": "⚡",
            "color": "зелёный",
            "url": f"https://bolt.eu/deeplink/?action=route&pickup={from_enc}&destination={to_enc}",
            "web": f"https://bolt.eu",
            "description": "Экономичный вариант"
        },
        "yango": {
            "name": "Яндекс Go",
            "emoji": "🔵",
            "color": "синий",
            "url": f"https://go.yandex/route?from={from_enc}&to={to_enc}",
            "web": f"https://go.yandex",
            "description": "Быстрая подача"
        },
        "uber": {
            "name": "Uber",
            "emoji": "🖤",
            "color": "чёрный",
            "url": f"https://m.uber.com/ul/?action=setPickup&pickup=my_location&dropoff[formatted_address]={to_enc}",
            "web": f"https://uber.com",
            "description": "Международный сервис"
        },
    }

# ═══════════════════════════════════════════════════════════════
#  РАБОТА СО СТАТИСТИКОЙ
# ═══════════════════════════════════════════════════════════════
def load_stats() -> dict:
    if not os.path.exists(STATS_FILE):
        return {"comparisons": [], "total": 0, "cheapest_wins": defaultdict(int)}
    with open(STATS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        data["cheapest_wins"] = defaultdict(int, data.get("cheapest_wins", {}))
        return data

def save_stats(data: dict):
    data["cheapest_wins"] = dict(data["cheapest_wins"])
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_comparison(from_addr, to_addr, prices: dict):
    stats = load_stats()
    valid = {k: v for k, v in prices.items() if v and v > 0}
    if not valid:
        return
    cheapest = min(valid, key=valid.get)
    stats["comparisons"].append({
        "from": from_addr,
        "to": to_addr,
        "prices": valid,
        "cheapest": cheapest,
        "date": datetime.now().isoformat()
    })
    stats["total"] = stats.get("total", 0) + 1
    stats["cheapest_wins"][cheapest] = stats["cheapest_wins"].get(cheapest, 0) + 1
    # Храним только последние 1000 сравнений
    stats["comparisons"] = stats["comparisons"][-1000:]
    save_stats(stats)

# ═══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Сравнить цены", callback_data="compare")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="how")],
    ])

def cancel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
    ])

def after_links_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Ввести цены для сравнения", callback_data="enter_prices")],
        [InlineKeyboardButton("🔄 Новый поиск", callback_data="compare")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])

def skip_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Пропустить", callback_data="skip_price")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])

# ═══════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ РЕЗУЛЬТАТОВ
# ═══════════════════════════════════════════════════════════════
def format_comparison(prices: dict, from_addr: str, to_addr: str) -> str:
    services = {
        "yandex": ("🚖", "Яндекс Такси"),
        "indrive": ("🚗", "inDrive"),
        "bolt":    ("⚡", "Bolt"),
        "yango":   ("🔵", "Яндекс Go"),
        "uber":    ("🖤", "Uber"),
    }

    valid = {k: v for k, v in prices.items() if v and v > 0}
    if not valid:
        return "❌ Нет данных для сравнения"

    sorted_prices = sorted(valid.items(), key=lambda x: x[1])
    cheapest_key = sorted_prices[0][0]
    most_exp_key = sorted_prices[-1][0]

    lines = [
        f"📍 *{from_addr}* → *{to_addr}*\n",
        "━━━━━━━━━━━━━━━━━━━━",
        "💰 *Сравнение цен:*\n"
    ]

    for i, (key, price) in enumerate(sorted_prices):
        emoji, name = services.get(key, ("🚕", key))
        if i == 0:
            badge = " 🏆 *ДЕШЕВЛЕ*"
        elif key == most_exp_key and len(valid) > 1:
            badge = " 📈 дороже"
        else:
            badge = ""

        savings = ""
        if i > 0:
            diff = price - sorted_prices[0][1]
            savings = f" (+{diff:,.0f} тг)"

        lines.append(f"{emoji} {name}: *{price:,.0f} тг*{savings}{badge}")

    if len(valid) > 1:
        cheapest_price = sorted_prices[0][1]
        most_exp_price = sorted_prices[-1][1]
        saved = most_exp_price - cheapest_price
        _, cheapest_name = services.get(cheapest_key, ("🚕", cheapest_key))
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"✅ Экономия с {cheapest_name}: *{saved:,.0f} тг*")

    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    text = (
        f"🚕 *{BOT_NAME}* — сравни цены такси!\n\n"
        "Открывай приложения, смотри цены и вводи сюда.\n"
        "Бот покажет где дешевле! 💰\n\n"
        "Поддерживаемые сервисы:\n"
        "🚖 Яндекс Такси  |  🚗 inDrive\n"
        "⚡ Bolt  |  🔵 Яндекс Go"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
    else:
        query = update.callback_query
        try:
            await query.edit_message_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
    return MAIN_MENU

async def show_how(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    text = (
        "ℹ️ *Как это работает:*\n\n"
        "1️⃣ Нажмите *Сравнить цены*\n"
        "2️⃣ Введите откуда и куда\n"
        "3️⃣ Бот откроет ссылки на все такси\n"
        "4️⃣ Открывайте каждое приложение и смотрите цену\n"
        "5️⃣ Вводите цены в бот\n"
        "6️⃣ Бот покажет где дешевле! 🏆\n\n"
        "📊 Статистика показывает какой сервис\n"
        "чаще всего оказывается дешевле в вашем городе."
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="menu")]]),
        parse_mode="Markdown"
    )
    return MAIN_MENU

async def start_compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["prices"] = {}
    try:
        await query.edit_message_text(
            "📍 *Откуда едем?*\n\n"
            "Введите адрес или название места:\n"
            "_Пример: Алматы, ул. Абая 1_",
            reply_markup=cancel_kb(),
            parse_mode="Markdown"
        )
    except:
        await query.message.reply_text(
            "📍 *Откуда едем?*\n\n"
            "Введите адрес или название места:",
            reply_markup=cancel_kb(),
            parse_mode="Markdown"
        )
    return GET_FROM

async def get_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["from"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Откуда: *{context.user_data['from']}*\n\n"
        "🏁 *Куда едем?*\n\n"
        "_Пример: Алматы, ул. Достык 100_",
        reply_markup=cancel_kb(),
        parse_mode="Markdown"
    )
    return GET_TO

async def get_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["to"] = update.message.text.strip()
    from_addr = context.user_data["from"]
    to_addr = context.user_data["to"]

    links = get_taxi_links(from_addr, to_addr)
    context.user_data["links"] = links

    # Формируем сообщение со ссылками
    text_lines = [
        f"🗺 *{from_addr}* → *{to_addr}*\n",
        "Открывайте каждый сервис и запоминайте цену:\n"
    ]

    keyboard = []
    for key, info in links.items():
        text_lines.append(f"{info['emoji']} *{info['name']}* — {info['description']}")
        keyboard.append([InlineKeyboardButton(
            f"{info['emoji']} Открыть {info['name']}",
            url=info["web"]
        )])

    text_lines.append("\nПосмотрели цены? Нажмите кнопку ниже 👇")
    keyboard.append([InlineKeyboardButton("💰 Ввести цены", callback_data="enter_prices")])
    keyboard.append([InlineKeyboardButton("🔄 Другой маршрут", callback_data="compare")])

    await update.message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SHOW_LINKS

async def start_enter_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["prices"] = {}
    context.user_data["price_step"] = "yandex"

    try:
        await query.edit_message_text(
            "🚖 *Яндекс Такси*\n\n"
            "Сколько показывает цена?\n"
            "_Введите число в тенге, например: 1800_\n\n"
            "Если приложение не установлено — нажмите Пропустить",
            reply_markup=skip_kb(),
            parse_mode="Markdown"
        )
    except:
        await query.message.reply_text(
            "🚖 *Яндекс Такси*\n\n"
            "Сколько показывает цена?",
            reply_markup=skip_kb(),
            parse_mode="Markdown"
        )
    return ENTER_YANDEX

async def enter_yandex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(" ", "").replace(",", ""))
        context.user_data["prices"]["yandex"] = price
    except:
        await update.message.reply_text(
            "❌ Введите число, например: *1800*",
            reply_markup=skip_kb(), parse_mode="Markdown"
        )
        return ENTER_YANDEX

    await update.message.reply_text(
        "🚗 *inDrive*\n\n"
        "Какую цену предложил inDrive?\n"
        "_Введите число или нажмите Пропустить_",
        reply_markup=skip_kb(),
        parse_mode="Markdown"
    )
    return ENTER_INDRIVE

async def enter_indrive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(" ", "").replace(",", ""))
        context.user_data["prices"]["indrive"] = price
    except:
        await update.message.reply_text(
            "❌ Введите число, например: *1500*",
            reply_markup=skip_kb(), parse_mode="Markdown"
        )
        return ENTER_INDRIVE

    await update.message.reply_text(
        "⚡ *Bolt*\n\n"
        "Какую цену показал Bolt?\n"
        "_Введите число или нажмите Пропустить_",
        reply_markup=skip_kb(),
        parse_mode="Markdown"
    )
    return ENTER_BOLT

async def enter_bolt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(" ", "").replace(",", ""))
        context.user_data["prices"]["bolt"] = price
    except:
        await update.message.reply_text(
            "❌ Введите число, например: *1650*",
            reply_markup=skip_kb(), parse_mode="Markdown"
        )
        return ENTER_BOLT

    await update.message.reply_text(
        "🔵 *Яндекс Go*\n\n"
        "Какую цену показал Яндекс Go?\n"
        "_Введите число или нажмите Пропустить_",
        reply_markup=skip_kb(),
        parse_mode="Markdown"
    )
    return ENTER_YANGO

async def enter_yango(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(" ", "").replace(",", ""))
        context.user_data["prices"]["yango"] = price
    except:
        await update.message.reply_text(
            "❌ Введите число, например: *1700*",
            reply_markup=skip_kb(), parse_mode="Markdown"
        )
        return ENTER_YANGO

    await update.message.reply_text(
        "🖤 *Uber*\n\nКакую цену показал Uber?\n_Введите число или нажмите Пропустить_",
        reply_markup=skip_kb(),
        parse_mode="Markdown"
    )
    return ENTER_UBER

async def enter_uber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text.strip().replace(" ", "").replace(",", ""))
        context.user_data["prices"]["uber"] = price
    except:
        await update.message.reply_text(
            "❌ Введите число, например: *2000*",
            reply_markup=skip_kb(), parse_mode="Markdown"
        )
        return ENTER_UBER

    return await show_result(update, context)

async def skip_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    step = context.user_data.get("price_step", "yandex")
    steps = ["yandex", "indrive", "bolt", "yango", "uber"]
    next_steps = {
        "yandex": (ENTER_INDRIVE, "🚗", "inDrive"),
        "indrive": (ENTER_BOLT, "⚡", "Bolt"),
        "bolt": (ENTER_YANGO, "🔵", "Яндекс Go"),
        "yango": (ENTER_UBER, "🖤", "Uber"),
    }

    if step in next_steps:
        next_state, emoji, name = next_steps[step]
        context.user_data["price_step"] = steps[steps.index(step) + 1]
        try:
            await query.edit_message_text(
                f"{emoji} *{name}*\n\n"
                f"Какую цену показал {name}?\n"
                "_Введите число или нажмите Пропустить_",
                reply_markup=skip_kb(),
                parse_mode="Markdown"
            )
        except:
            await query.message.reply_text(
                f"{emoji} *{name}*\n\n"
                f"Какую цену показал {name}?",
                reply_markup=skip_kb(),
                parse_mode="Markdown"
            )
        return next_state
    else:
        # Последний шаг — показываем результат
        return await show_result_from_callback(update, context)

async def show_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    prices = context.user_data.get("prices", {})
    from_addr = context.user_data.get("from", "—")
    to_addr = context.user_data.get("to", "—")

    # Сохраняем статистику
    add_comparison(from_addr, to_addr, prices)

    result_text = format_comparison(prices, from_addr, to_addr)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Новый маршрут", callback_data="compare")],
        [InlineKeyboardButton("📊 Общая статистика", callback_data="stats")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])

    await update.message.reply_text(
        result_text, reply_markup=keyboard, parse_mode="Markdown"
    )
    return MAIN_MENU

async def show_result_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    prices = context.user_data.get("prices", {})
    from_addr = context.user_data.get("from", "—")
    to_addr = context.user_data.get("to", "—")

    add_comparison(from_addr, to_addr, prices)
    result_text = format_comparison(prices, from_addr, to_addr)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Новый маршрут", callback_data="compare")],
        [InlineKeyboardButton("📊 Общая статистика", callback_data="stats")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])

    try:
        await query.edit_message_text(result_text, reply_markup=keyboard, parse_mode="Markdown")
    except:
        await query.message.reply_text(result_text, reply_markup=keyboard, parse_mode="Markdown")
    return MAIN_MENU

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    stats = load_stats()
    total = stats.get("total", 0)
    wins = stats.get("cheapest_wins", {})

    services = {
        "yandex": ("🚖", "Яндекс Такси"),
        "indrive": ("🚗", "inDrive"),
        "bolt":    ("⚡", "Bolt"),
        "yango":   ("🔵", "Яндекс Go"),
        "uber":    ("🖤", "Uber"),
    }

    lines = [
        "📊 *Статистика TaxiCompare*\n",
        f"🔢 Всего сравнений: *{total}*\n",
        "🏆 *Кто чаще всего дешевле:*\n"
    ]

    if wins:
        sorted_wins = sorted(wins.items(), key=lambda x: x[1], reverse=True)
        for key, count in sorted_wins:
            emoji, name = services.get(key, ("🚕", key))
            pct = round(count / total * 100) if total > 0 else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"{emoji} {name}\n   {bar} {pct}% ({count} раз)\n")
    else:
        lines.append("_Пока нет данных. Сделайте первое сравнение!_")

    # Последние сравнения
    recent = stats.get("comparisons", [])[-3:]
    if recent:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("🕐 *Последние маршруты:*\n")
        for c in reversed(recent):
            cheapest = c.get("cheapest", "")
            emoji, name = services.get(cheapest, ("🚕", cheapest))
            lines.append(
                f"📍 {c.get('from', '?')} → {c.get('to', '?')}\n"
                f"   {emoji} Дешевле: {name}\n"
            )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Сравнить сейчас", callback_data="compare")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
        ]),
        parse_mode="Markdown"
    )
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        return await start(update, context)
    await update.message.reply_text("↩️ Отменено. /start — начать заново")
    return ConversationHandler.END

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await start(update, context)

# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════
async def health(request):
    return web.Response(text="OK")

async def start_web():
    app_web = web.Application()
    app_web.router.add_get("/", health)
    app_web.router.add_get("/health", health)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

async def main():
    await start_web()
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_compare, pattern="^compare$"),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(start_compare, pattern="^compare$"),
                CallbackQueryHandler(show_stats, pattern="^stats$"),
                CallbackQueryHandler(show_how, pattern="^how$"),
                CallbackQueryHandler(menu_handler, pattern="^menu$"),
            ],
            GET_FROM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_from),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            GET_TO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_to),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            SHOW_LINKS: [
                CallbackQueryHandler(start_enter_prices, pattern="^enter_prices$"),
                CallbackQueryHandler(start_compare, pattern="^compare$"),
                CallbackQueryHandler(menu_handler, pattern="^menu$"),
            ],
            ENTER_YANDEX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_yandex),
                CallbackQueryHandler(skip_price, pattern="^skip_price$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            ENTER_INDRIVE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_indrive),
                CallbackQueryHandler(skip_price, pattern="^skip_price$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            ENTER_BOLT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_bolt),
                CallbackQueryHandler(skip_price, pattern="^skip_price$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            ENTER_YANGO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_yango),
                CallbackQueryHandler(skip_price, pattern="^skip_price$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            ENTER_UBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_uber),
                CallbackQueryHandler(skip_price, pattern="^skip_price$"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CallbackQueryHandler(menu_handler, pattern="^menu$"),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stats", show_stats_cmd))

    print(f"🚕 {BOT_NAME} запущен!")
    print(f"   Сравниваем: Яндекс | inDrive | Bolt | Яндекс Go | Uber")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

async def show_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats"""
    stats = load_stats()
    total = stats.get("total", 0)
    await update.message.reply_text(
        f"📊 Всего сравнений: *{total}*\n"
        f"Используйте меню для подробной статистики.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Подробная статистика", callback_data="stats")]
        ])
    )

if __name__ == "__main__":
    asyncio.run(main())
