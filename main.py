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

# --- Секретные данные читаются из переменных окружения (GitHub Secrets) ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELETHON_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")  # нужен только для одноразового переноса старых данных

USERS_FILE = "users.json"
LEGACY_CONFIG_FILE = "config.json"
LEGACY_STATE_FILE = "state.json"

MAX_MESSAGE_LENGTH = 3900
MAX_CAPTION_LENGTH = 1024
DELAY_BETWEEN_MESSAGES = 2

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


# ==================== Загрузка / сохранение пользователей ====================

def migrate_legacy_if_needed():
    """Одноразовый перенос данных из старого формата (config.json + state.json)
    в новый users.json, чтобы не терять прогресс и не слать историю заново."""
    if not (os.path.exists(LEGACY_CONFIG_FILE) and os.path.exists(LEGACY_STATE_FILE) and OWNER_CHAT_ID):
        return {}

    with open(LEGACY_CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
    with open(LEGACY_STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    channels = {}
    for ch in config.get("channels", []):
        channels[ch] = {"last_id": state.get(ch, 0), "pending_init": False}

    print("Выполняю перенос старых настроек (config.json/state.json) в users.json")
    return {
        OWNER_CHAT_ID: {
            "channels": channels,
            "keywords": config.get("keywords", []),
            "destination": {"type": "private", "chat_id": OWNER_CHAT_ID},
            "delivery_time": "19:00",
            "timezone": "Europe/Moscow",
            "last_delivered_date": None,
        }
    }


def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("users", {})
    return migrate_legacy_if_needed()


def save_users(users):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)


# ==================== Вспомогательные функции ====================

def matches_keywords(text, keywords):
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords:
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


def is_due(user, now_utc):
    """Наступил ли у пользователя его час доставки и не доставляли ли мы ему
    уже находки сегодня."""
    try:
        tz = ZoneInfo(user.get("timezone", "Europe/Moscow"))
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    local_now = now_utc.astimezone(tz)
    today_str = local_now.date().isoformat()

    if user.get("last_delivered_date") == today_str:
        return False

    delivery_hour = user.get("delivery_time", "19:00")
    return local_now.strftime("%H:00") == delivery_hour


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


# ==================== Инициализация каналов "только новое" ====================

def initialize_pending_channels(client, users):
    """Для каналов, добавленных в режиме "только новое", один раз запоминаем
    текущую последнюю точку в канале — ничего из истории не отправляем."""
    for chat_id, user in users.items():
        for channel, chstate in user.get("channels", {}).items():
            if chstate.get("pending_init"):
                latest_id = 0
                try:
                    for msg in client.iter_messages(channel, limit=1):
                        latest_id = msg.id
                except Exception as e:
                    print(f"Не удалось инициализировать {channel}: {e}")
                chstate["last_id"] = latest_id
                chstate["pending_init"] = False
                print(f"Канал {channel} для пользователя {chat_id}: начинаем отслеживать с сообщения {latest_id}")


# ==================== Основная логика ====================

def main():
    users = load_users()
    if not users:
        print("Пока нет ни одного пользователя.")
        return

    now_utc = datetime.now(timezone.utc)

    with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        initialize_pending_channels(client, users)

        due_users = {cid: u for cid, u in users.items() if is_due(u, now_utc)}

        if not due_users:
            print("Сейчас никому не пора получать находки — выходим.")
            save_users(users)
            return

        print(f"Сейчас пора получать находки: {len(due_users)} пользователь(ей)")

        # channel -> {chat_id: last_id}, только для тех, кому сейчас пора
        channel_map = {}
        for chat_id, user in due_users.items():
            for channel, chstate in user.get("channels", {}).items():
                channel_map.setdefault(channel, {})[chat_id] = chstate["last_id"]

        for channel, per_user_last_id in channel_map.items():
            print(f"Проверяю канал: {channel}")
            min_id = min(per_user_last_id.values())
            max_id_seen = min_id

            try:
                messages = list(client.iter_messages(channel, min_id=min_id, reverse=True))
            except Exception as e:
                print(f"  Ошибка при обработке канала {channel}: {e}")
                continue

            for msg in messages:
                if msg.id > max_id_seen:
                    max_id_seen = msg.id

                for chat_id, last_id in per_user_last_id.items():
                    if msg.id <= last_id:
                        continue
                    user = due_users[chat_id]
                    keyword = matches_keywords(msg.raw_text, user.get("keywords", []))
                    if keyword:
                        destination = user.get("destination", {"type": "private", "chat_id": chat_id})
                        deliver_post(client, destination["chat_id"], channel, msg, keyword)

            for chat_id in per_user_last_id:
                users[chat_id]["channels"][channel]["last_id"] = max_id_seen
            print(f"  Готово.")

        for chat_id, user in due_users.items():
            tz = ZoneInfo(user.get("timezone", "Europe/Moscow"))
            local_today = now_utc.astimezone(tz).date().isoformat()
            users[chat_id]["last_delivered_date"] = local_today
            print(f"  Пользователь {chat_id}: доставка выполнена на {local_today}")

    save_users(users)


if __name__ == "__main__":
    main()
