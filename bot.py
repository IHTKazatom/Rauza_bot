import asyncio
import logging
import os
import json
import re
from datetime import datetime
from aiohttp import web, ClientSession, ClientTimeout
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════
TOKEN = os.getenv("BOT_TOKEN", "")
USERS_FILE = "subscribers.json"
PRODUCTS_FILE = "products_cache.json"
CHECK_INTERVAL = 3 * 60 * 60  # каждые 3 часа

BASE_URL = "https://rauza-ade.kz"
DISCOUNT_URL = f"{BASE_URL}/catalog/all?discounts=true"

CATEGORIES = {
    "all": "🛒 Все товары со скидкой",
    "lekarstvennye-sredstva": "💊 Лекарства",
    "vitaminy": "🍊 Витамины",
    "bad": "🌿 БАДы",
    "mama-i-malysh": "👶 Мама и малыш",
    "lechebnaya-kosmetika": "💄 Косметика",
    "gigiena": "🧴 Гигиена",
}

# ═══════════════════════════════════════════════════════════════
#  РАБОТА С ДАННЫМИ
# ═══════════════════════════════════════════════════════════════
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_users(): return load_json(USERS_FILE, {})
def save_users(u): save_json(USERS_FILE, u)
def load_cache(): return load_json(PRODUCTS_FILE, {"products": [], "updated": ""})
def save_cache(d): save_json(PRODUCTS_FILE, d)

def get_user(uid):
    return load_users().get(str(uid))

def save_user(uid, data):
    users = load_users()
    users[str(uid)] = data
    save_users(users)

# ═══════════════════════════════════════════════════════════════
#  ПАРСЕР САЙТА РАУЗА
# ═══════════════════════════════════════════════════════════════
async def fetch_discounts(category="all") -> list:
    """Парсим товары со скидкой с сайта rauza-ade.kz"""
    if category == "all":
        url = DISCOUNT_URL
    else:
        url = f"{BASE_URL}/catalog/{category}?discounts=true"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    products = []
    try:
        timeout = ClientTimeout(total=30)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"Status {resp.status} for {url}")
                    return []
                html = await resp.text()

        # Ищем товары в HTML
        # Паттерн для названия и ссылки
        links = re.findall(r'href="(/products/\d+)"[^>]*>(.*?)</a>', html, re.DOTALL)
        # Паттерн для скидки
        discounts = re.findall(r'-(\d+\.?\d*)%', html)
        # Паттерн для цен
        prices_old = re.findall(r'class="[^"]*old[^"]*"[^>]*>\s*([\d\s,]+)\s*[₸₽]', html)
        prices_new = re.findall(r'class="[^"]*new[^"]*"[^>]*>\s*([\d\s,]+)\s*[₸₽]', html)

        # Простой парсинг блоков товаров
        product_blocks = re.findall(
            r'href="(/products/(\d+))"[^>]*>\s*<[^>]+>\s*([^<]+)</[^>]+>.*?(-\d+\.?\d*%)',
            html, re.DOTALL
        )

        seen_ids = set()
        for block in product_blocks:
            link, prod_id, name, discount = block
            if prod_id in seen_ids:
                continue
            seen_ids.add(prod_id)
            name = re.sub(r'\s+', ' ', name).strip()
            if len(name) < 3:
                continue
            products.append({
                "id": prod_id,
                "name": name,
                "discount": discount.strip(),
                "url": f"{BASE_URL}{link}",
            })

        # Если блочный парсинг не дал результатов — простой парсинг
        if not products:
            all_links = re.findall(r'href="(/products/(\d+))"[^>]*>([^<]{5,80})</a>', html)
            disc_list = re.findall(r'-(\d+)\.?\d*%', html)
            for i, (link, prod_id, name) in enumerate(all_links[:50]):
                name = name.strip()
                if not name or prod_id in seen_ids:
                    continue
                seen_ids.add(prod_id)
                discount = f"-{disc_list[i]}%" if i < len(disc_list) else "скидка"
                products.append({
                    "id": prod_id,
                    "name": name,
                    "discount": discount,
                    "url": f"{BASE_URL}{link}",
                })

        logger.info(f"Найдено товаров: {len(products)} на странице {url}")
        return products

    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return []

# ═══════════════════════════════════════════════════════════════
#  СОСТОЯНИЯ
# ═══════════════════════════════════════════════════════════════
MAIN_MENU, CATEGORY_SELECT = range(2)

# ═══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════
def main_kb(is_subscribed: bool):
    rows = [
        [InlineKeyboardButton("🔥 Скидки прямо сейчас", callback_data="show_all")],
        [InlineKeyboardButton("📂 По категории", callback_data="by_category")],
    ]
    if is_subscribed:
        rows.append([InlineKeyboardButton("🔕 Отписаться от уведомлений", callback_data="unsubscribe")])
    else:
        rows.append([InlineKeyboardButton("🔔 Подписаться на уведомления", callback_data="subscribe")])
    rows.append([InlineKeyboardButton("ℹ️ О боте", callback_data="about")])
    return InlineKeyboardMarkup(rows)

def category_kb():
    rows = []
    for key, name in CATEGORIES.items():
        rows.append([InlineKeyboardButton(name, callback_data=f"cat_{key}")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)

def back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="show_all")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])

# ═══════════════════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ ТОВАРОВ
# ═══════════════════════════════════════════════════════════════
def format_products(products: list, category_name: str = "Все скидки") -> list[str]:
    """Возвращает список сообщений (Telegram лимит 4096 символов)"""
    if not products:
        return [f"😔 По категории *{category_name}* скидок сейчас нет.\n\nПопробуйте позже или выберите другую категорию."]

    messages = []
    current = [f"🏷 *{category_name}* — {len(products)} товаров\n_Обновлено: {datetime.now().strftime('%d.%m %H:%M')}_\n\n"]

    for i, p in enumerate(products, 1):
        line = f"{i}. [{p['name']}]({p['url']}) — *{p['discount']}*\n"
        if sum(len(m) for m in current) + len(line) > 3800:
            messages.append("".join(current))
            current = [f"📋 *Продолжение* ({i}/{len(products)})\n\n"]
        current.append(line)

    if current:
        messages.append("".join(current))

    return messages

# ═══════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user = get_user(user_id)
    is_sub = user.get("subscribed", False) if user else False

    text = (
        "💊 *Бот скидок аптеки Рауза-АДЕ*\n\n"
        "Следит за товарами с подходящим сроком годности "
        "и сообщает о скидках 20-50%!\n\n"
        "🔔 Подпишитесь — бот сам напишет когда появятся новые скидки."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_kb(is_sub), parse_mode="Markdown")
    else:
        query = update.callback_query
        try:
            await query.edit_message_text(text, reply_markup=main_kb(is_sub), parse_mode="Markdown")
        except:
            await query.message.reply_text(text, reply_markup=main_kb(is_sub), parse_mode="Markdown")
    return MAIN_MENU

async def show_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Загружаю скидки с сайта Рауза-АДЕ...", parse_mode="Markdown")

    products = await fetch_discounts("all")

    # Сохраняем в кэш
    save_cache({"products": products, "updated": datetime.now().isoformat()})

    messages = format_products(products, "Все скидки Рауза-АДЕ")
    for i, msg in enumerate(messages):
        kb = back_kb() if i == len(messages) - 1 else None
        try:
            if i == 0:
                await query.edit_message_text(msg, reply_markup=kb, parse_mode="Markdown",
                                              disable_web_page_preview=True)
            else:
                await query.message.reply_text(msg, reply_markup=kb, parse_mode="Markdown",
                                               disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")

    return MAIN_MENU

async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    cat_key = query.data.replace("cat_", "")
    cat_name = CATEGORIES.get(cat_key, cat_key)

    await query.edit_message_text(f"⏳ Загружаю *{cat_name}*...", parse_mode="Markdown")

    products = await fetch_discounts(cat_key)
    messages = format_products(products, cat_name)

    for i, msg in enumerate(messages):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 Другая категория", callback_data="by_category")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]) if i == len(messages) - 1 else None
        try:
            if i == 0:
                await query.edit_message_text(msg, reply_markup=kb, parse_mode="Markdown",
                                              disable_web_page_preview=True)
            else:
                await query.message.reply_text(msg, reply_markup=kb, parse_mode="Markdown",
                                               disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Ошибка: {e}")

    return MAIN_MENU

async def by_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text(
            "📂 *Выберите категорию:*",
            reply_markup=category_kb(),
            parse_mode="Markdown"
        )
    except:
        pass
    return CATEGORY_SELECT

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    name = update.effective_user.first_name or "Пользователь"
    save_user(user_id, {"name": name, "subscribed": True, "since": datetime.now().isoformat()})
    try:
        await query.edit_message_text(
            "✅ *Вы подписаны на уведомления!*\n\n"
            "Бот проверяет сайт каждые 3 часа и пришлёт сообщение "
            "когда появятся новые скидки.\n\n"
            "🏠 Главное меню:",
            reply_markup=main_kb(True),
            parse_mode="Markdown"
        )
    except:
        pass
    return MAIN_MENU

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    user = get_user(user_id) or {}
    user["subscribed"] = False
    save_user(user_id, user)
    try:
        await query.edit_message_text(
            "🔕 *Вы отписались от уведомлений.*\n\n"
            "Вы всегда можете подписаться снова.",
            reply_markup=main_kb(False),
            parse_mode="Markdown"
        )
    except:
        pass
    return MAIN_MENU

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    users = load_users()
    subs = sum(1 for u in users.values() if u.get("subscribed"))
    cache = load_cache()
    updated = cache.get("updated", "")[:16].replace("T", " ") if cache.get("updated") else "ещё не проверялось"
    try:
        await query.edit_message_text(
            "ℹ️ *О боте*\n\n"
            f"Бот следит за скидками на сайте [rauza-ade.kz]({BASE_URL}) "
            "и уведомляет подписчиков о новых акциях.\n\n"
            f"🔔 Подписчиков: {subs}\n"
            f"🕐 Последняя проверка: {updated}\n"
            f"⏱ Проверка каждые 3 часа\n\n"
            "Скидки связаны с подходящим сроком годности товаров — "
            "они полностью безопасны для использования!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Открыть сайт", url=BASE_URL)],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ]),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except:
        pass
    return MAIN_MENU

# ═══════════════════════════════════════════════════════════════
#  АВТОМАТИЧЕСКАЯ ПРОВЕРКА И УВЕДОМЛЕНИЯ
# ═══════════════════════════════════════════════════════════════
async def check_and_notify(app):
    """Проверяем новые скидки и уведомляем подписчиков"""
    logger.info("Автопроверка скидок Рауза-АДЕ...")
    try:
        products = await fetch_discounts("all")
        if not products:
            logger.info("Товаров со скидкой не найдено")
            return

        old_cache = load_cache()
        old_ids = {p["id"] for p in old_cache.get("products", [])}
        new_products = [p for p in products if p["id"] not in old_ids]

        save_cache({"products": products, "updated": datetime.now().isoformat()})

        if not new_products and old_ids:
            logger.info(f"Новых скидок нет. Всего: {len(products)}")
            return

        # Уведомляем подписчиков
        users = load_users()
        subscribers = [uid for uid, u in users.items() if u.get("subscribed")]
        if not subscribers:
            return

        notify_products = new_products if new_products else products[:10]
        label = f"🆕 *{len(new_products)} новых скидок* в Рауза-АДЕ!" if new_products else f"🔥 *Актуальные скидки* в Рауза-АДЕ"

        lines = [f"{label}\n_{datetime.now().strftime('%d.%m.%Y %H:%M')}_\n\n"]
        for i, p in enumerate(notify_products[:15], 1):
            lines.append(f"{i}. [{p['name']}]({p['url']}) — *{p['discount']}*\n")
        if len(products) > 15:
            lines.append(f"\n_...и ещё {len(products) - 15} товаров на сайте_")

        text = "".join(lines)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Все скидки", url=DISCOUNT_URL)],
        ])

        sent = 0
        for uid in subscribers:
            try:
                await app.bot.send_message(
                    chat_id=int(uid),
                    text=text,
                    reply_markup=kb,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                sent += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"Не удалось уведомить {uid}: {e}")

        logger.info(f"Уведомлено {sent} подписчиков о {len(notify_products)} товарах")

    except Exception as e:
        logger.error(f"Ошибка автопроверки: {e}")

async def scheduler(app):
    """Планировщик — запускает проверку каждые 3 часа"""
    await asyncio.sleep(10)  # небольшая задержка при старте
    while True:
        await check_and_notify(app)
        await asyncio.sleep(CHECK_INTERVAL)

# ═══════════════════════════════════════════════════════════════
#  HTTP СЕРВЕР (нужен для Fly.io)
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

# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════
async def main():
    await start_web()
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(show_all, pattern="^show_all$"),
                CallbackQueryHandler(by_category, pattern="^by_category$"),
                CallbackQueryHandler(subscribe, pattern="^subscribe$"),
                CallbackQueryHandler(unsubscribe, pattern="^unsubscribe$"),
                CallbackQueryHandler(about, pattern="^about$"),
                CallbackQueryHandler(start, pattern="^main_menu$"),
            ],
            CATEGORY_SELECT: [
                CallbackQueryHandler(show_category, pattern="^cat_"),
                CallbackQueryHandler(start, pattern="^main_menu$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv)

    # Запускаем планировщик параллельно
    asyncio.create_task(scheduler(app))

    print("💊 Бот скидок Рауза-АДЕ запущен!")
    print(f"   Проверка каждые {CHECK_INTERVAL // 3600} часа")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
