import os
import io
import json
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBold, MessageEntityItalic, MessageEntityUnderline,
    MessageEntityStrike, MessageEntityCode, MessageEntityPre,
    MessageEntityTextUrl, MessageEntitySpoiler, MessageEntityBlockquote,
)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELETHON_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")

USERS_FILE = "users.json"
LEGACY_CONFIG_FILE = "config.json"
LEGACY_STATE_FILE = "state.json"

MAX_MESSAGE_LENGTH = 3900
MAX_CAPTION_LENGTH = 1024
DELAY_BETWEEN_MESSAGES = 2
DEFAULT_TZ = "Europe/Moscow"

ENTITY_TYPE_MAP = {
    MessageEntityBold: "bold",
    MessageEntityItalic: "italic",
    MessageEntityUnderline: "underline",
    MessageEntityStrike: "strikethrough",
    MessageEntityCode: "code",
    MessageEntityPre: "pre",
    MessageEntityTextUrl: "text_link",
    MessageEntitySpoiler: "spoiler",
    MessageEntityBlockquote: "blockquote",
}


# ==================== Загрузка / сохранение / миграция ====================

def migrate_legacy_if_needed():
    if not (os.path.exists(LEGACY_CONFIG_FILE) and os.path.exists(LEGACY_STATE_FILE) and OWNER_CHAT_ID):
        return {}
    with open(LEGACY_CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    with open(LEGACY_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    channels = {}
    for ch in config.get("channels", []):
        channels[ch] = {"last_id": state.get(ch, 0)}

    print("Выполняю перенос старых настроек (config.json/state.json) в users.json")
    return {
        OWNER_CHAT_ID: {
            "channels": channels,
            "keywords": {kw: {} for kw in config.get("keywords", [])},
            "destination": {"type": "private", "chat_id": OWNER_CHAT_ID},
            "delivery_time": "19:00",
            "timezone": DEFAULT_TZ,
        }
    }


def route_key(destination, delivery_time, tz_name):
    return f"{destination.get('type')}:{destination.get('chat_id')}|{delivery_time}|{tz_name}"


def normalize_user(user):
    """Приводит запись пользователя к актуальному формату, независимо от того,
    в каком из прошлых форматов она была сохранена."""
    user.setdefault("destination", {"type": "private", "chat_id": "0"})
    user.setdefault("delivery_time", "19:00")
    user.setdefault("timezone", DEFAULT_TZ)

    # Слова: старый формат — список строк; новый — объект с настройками
    kws = user.get("keywords", {})
    if isinstance(kws, list):
        user["keywords"] = {kw: {} for kw in kws}
    for kw, settings in user["keywords"].items():
        settings.setdefault("excluded_channels", [])
        settings.setdefault("only_channels", [])
        settings.setdefault("overrides", {})

    global_route_key = route_key(user["destination"], user["delivery_time"], user["timezone"])
    old_last_delivered = user.pop("last_delivered_date", None)

    for channel, chstate in user.get("channels", {}).items():
        chstate.setdefault("baseline_id", 0)
        chstate.setdefault("pending_init", False)
        chstate.setdefault("immediate_requested", False)
        chstate.setdefault("route_state", {})
        if "last_id" in chstate:
            old_last_id = chstate.pop("last_id")
            chstate["route_state"].setdefault(
                global_route_key, {"last_id": old_last_id, "last_delivered_date": old_last_delivered}
            )

    return user


def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f).get("users", {})
    else:
        raw = migrate_legacy_if_needed()

    return {chat_id: normalize_user(user) for chat_id, user in raw.items()}


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)


# ==================== Вспомогательные функции ====================

def matches_keywords(text, keyword_names):
    if not text:
        return None
    lowered = text.lower()
    for kw in keyword_names:
        if kw.lower() in lowered:
            return kw
    return None


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def convert_entities(entities, offset_shift):
    result = []
    if not entities:
        return result
    for ent in entities:
        bot_type = ENTITY_TYPE_MAP.get(type(ent))
        if not bot_type:
            continue
        item = {"type": bot_type, "offset": ent.offset + offset_shift, "length": ent.length}
        if bot_type == "text_link":
            item["url"] = ent.url
        result.append(item)
    return result


def split_long_text(text, limit):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
    parts.append(text)
    return parts


def is_route_due(delivery_time, tz_name, now_utc, last_delivered_date):
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)

    local_now = now_utc.astimezone(tz)
    today_str = local_now.date().isoformat()

    if last_delivered_date == today_str:
        return False

    try:
        th, tm = map(int, delivery_time.split(":"))
    except Exception:
        th, tm = 19, 0

    target_minutes = th * 60 + tm
    now_minutes = local_now.hour * 60 + local_now.minute
    diff = (now_minutes - target_minutes) % (24 * 60)
    return 0 <= diff < 15


def keyword_applies_to_channel(kw_settings, channel):
    only = kw_settings.get("only_channels") or []
    if only:
        return channel in only
    excluded = kw_settings.get("excluded_channels") or []
    return channel not in excluded


def effective_route(user, channel, kw_settings):
    override = (kw_settings.get("overrides") or {}).get(channel)
    if override:
        return override["destination"], override["delivery_time"], override.get("timezone", user["timezone"])
    return user["destination"], user["delivery_time"], user["timezone"]


def build_channel_routes(user, channel):
    """Группирует ключевые слова, применимые к каналу, по эффективному
    маршруту (место + время + пояс) — чтобы не сканировать канал отдельно
    под каждое слово, если у них одинаковые настройки."""
    routes = {}
    for kw, kw_settings in user.get("keywords", {}).items():
        if not keyword_applies_to_channel(kw_settings, channel):
            continue
        dest, dtime, tz = effective_route(user, channel, kw_settings)
        rk = route_key(dest, dtime, tz)
        routes.setdefault(rk, {"destination": dest, "delivery_time": dtime, "timezone": tz, "keywords": []})
        routes[rk]["keywords"].append(kw)
    return routes


# ==================== Отправка в Telegram ====================

def telegram_api_call(method, data, files=None, max_retries=5):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(1, max_retries + 1):
        if files:
            for f in files.values():
                if hasattr(f[1], "seek"):
                    f[1].seek(0)
            resp = requests.post(url, data=data, files=files, timeout=60)
        else:
            resp = requests.post(url, json=data, timeout=20)

        if resp.ok:
            return True

        payload = {}
        try:
            payload = resp.json()
        except ValueError:
            pass

        if resp.status_code == 429:
            wait_seconds = payload.get("parameters", {}).get("retry_after", 5) + 1
            print(f"    Превышен лимит запросов, жду {wait_seconds} сек. (попытка {attempt}/{max_retries})")
            time.sleep(wait_seconds)
            continue

        print(f"Ошибка Telegram API ({method}):", resp.text)
        return False

    print("Не удалось выполнить запрос после нескольких попыток — пропускаю.")
    return False


def send_text(chat_id, text, entities=None):
    data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    if entities:
        data["entities"] = entities
    return telegram_api_call("sendMessage", data)


def send_media(chat_id, client, msg, caption, entities=None):
    buffer = io.BytesIO()
    client.download_media(msg, file=buffer)
    buffer.seek(0)

    if msg.photo:
        method, field = "sendPhoto", "photo"
    elif msg.video:
        method, field = "sendVideo", "video"
    else:
        method, field = "sendDocument", "document"

    files = {field: ("file", buffer)}
    data = {"chat_id": chat_id, "caption": caption[:MAX_CAPTION_LENGTH]}
    if entities:
        data["caption_entities"] = json.dumps(entities)
    return telegram_api_call(method, data, files=files)


def deliver_post(client, chat_id, channel, msg, keyword):
    link = f"https://t.me/{channel.lstrip('@')}/{msg.id}"
    header = f"🔎 Найдено слово: {keyword}\n📢 Канал: {channel}\n\n"
    body = msg.raw_text or ""
    full_text = f"{header}{body}\n\n{link}"

    has_media = bool(msg.photo or msg.video or msg.document)
    fits_as_caption = len(full_text) <= MAX_CAPTION_LENGTH

    if has_media and fits_as_caption:
        entities = convert_entities(msg.entities, utf16_len(header))
        send_media(chat_id, client, msg, full_text, entities=entities)
        return

    if has_media:
        short_caption = f"🔎 Найдено слово: {keyword}\n📢 Канал: {channel}"
        send_media(chat_id, client, msg, short_caption)
        time.sleep(DELAY_BETWEEN_MESSAGES)

    parts = split_long_text(full_text, MAX_MESSAGE_LENGTH)
    if len(parts) == 1:
        entities = convert_entities(msg.entities, utf16_len(header))
        send_text(chat_id, parts[0], entities=entities)
        time.sleep(DELAY_BETWEEN_MESSAGES)
    else:
        for part in parts:
            send_text(chat_id, part)
            time.sleep(DELAY_BETWEEN_MESSAGES)


# ==================== Обработка одного маршрута ====================

def process_route(client, channel, route_state_dict, rk, route, baseline_id, force, now_utc):
    """Сканирует канал по конкретному маршруту (место+время+свои слова) и
    доставляет находки. force=True — игнорирует проверку времени (для
    внеочередных проверок «Переслать сейчас»)."""
    rstate = route_state_dict.setdefault(rk, {"last_id": baseline_id, "last_delivered_date": None})

    if not force and not is_route_due(route["delivery_time"], route["timezone"], now_utc, rstate.get("last_delivered_date")):
        return False  # не наступило время для этого маршрута

    min_id = rstate.get("last_id", baseline_id)
    max_id_seen = min_id

    try:
        messages = list(client.iter_messages(channel, min_id=min_id, reverse=True))
    except Exception as e:
        print(f"    Ошибка при сканировании {channel}: {e}")
        return False

    for msg in messages:
        if msg.id > max_id_seen:
            max_id_seen = msg.id
        keyword = matches_keywords(msg.raw_text, route["keywords"])
        if keyword:
            deliver_post(client, route["destination"]["chat_id"], channel, msg, keyword)

    rstate["last_id"] = max_id_seen
    if not force:
        local_today = now_utc.astimezone(ZoneInfo(route["timezone"])).date().isoformat()
        rstate["last_delivered_date"] = local_today

    return True


# ==================== Немедленные проверки ("Переслать сейчас") ====================

def process_immediate_requests(client, users, now_utc):
    any_done = False
    for chat_id, user in users.items():
        for channel, chstate in user.get("channels", {}).items():
            if not chstate.get("immediate_requested"):
                continue
            print(f"Внеочередная проверка: {channel} для пользователя {chat_id}")
            routes = build_channel_routes(user, channel)
            for rk, route in routes.items():
                process_route(
                    client, channel, chstate["route_state"], rk, route,
                    chstate.get("baseline_id", 0), force=True, now_utc=now_utc
                )
            chstate["immediate_requested"] = False
            any_done = True
    return any_done


# ==================== Инициализация каналов "только новое" ====================

def initialize_pending_channels(client, users):
    any_done = False
    for chat_id, user in users.items():
        for channel, chstate in user.get("channels", {}).items():
            if chstate.get("pending_init"):
                latest_id = 0
                try:
                    for msg in client.iter_messages(channel, limit=1):
                        latest_id = msg.id
                except Exception as e:
                    print(f"Не удалось инициализировать {channel}: {e}")
                chstate["baseline_id"] = latest_id
                chstate["pending_init"] = False
                any_done = True
                print(f"Канал {channel} для {chat_id}: начинаем с сообщения {latest_id}")
    return any_done


# ==================== Плановая доставка ====================

def any_route_due_preview(users, now_utc):
    """Быстрая проверка без подключения к Telegram: есть ли вообще смысл
    сейчас что-то делать."""
    for user in users.values():
        for channel, chstate in user.get("channels", {}).items():
            if chstate.get("pending_init") or chstate.get("immediate_requested"):
                return True
            routes = build_channel_routes(user, channel)
            for rk, route in routes.items():
                rstate = chstate.get("route_state", {}).get(rk, {})
                if is_route_due(route["delivery_time"], route["timezone"], now_utc, rstate.get("last_delivered_date")):
                    return True
    return False


def process_scheduled(client, users, now_utc):
    for chat_id, user in users.items():
        for channel, chstate in user.get("channels", {}).items():
            routes = build_channel_routes(user, channel)
            for rk, route in routes.items():
                done = process_route(
                    client, channel, chstate["route_state"], rk, route,
                    chstate.get("baseline_id", 0), force=False, now_utc=now_utc
                )
                if done:
                    print(f"  {channel} / маршрут {rk}: доставлено пользователю {chat_id}")


# ==================== Точка входа ====================

def main():
    users = load_users()
    if not users:
        print("Пока нет ни одного пользователя.")
        return

    now_utc = datetime.now(timezone.utc)

    needs_connection = any_route_due_preview(users, now_utc) or any(
        chstate.get("pending_init") or chstate.get("immediate_requested")
        for user in users.values()
        for chstate in user.get("channels", {}).values()
    )

    if not needs_connection:
        print("Сейчас нечего делать — выходим без подключения к Telegram.")
        save_users(users)
        return

    with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        initialize_pending_channels(client, users)
        process_immediate_requests(client, users, now_utc)
        process_scheduled(client, users, now_utc)

    save_users(users)


if __name__ == "__main__":
    main()
