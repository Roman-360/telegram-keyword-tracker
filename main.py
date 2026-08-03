import os
import io
import json
import time
import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageEntityBold, MessageEntityItalic, MessageEntityUnderline,
    MessageEntityStrike, MessageEntityCode, MessageEntityPre,
    MessageEntityTextUrl, MessageEntitySpoiler, MessageEntityBlockquote,
    MessageMediaWebPage,
)

# --- Секретные данные читаются из переменных окружения (GitHub Secrets) ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELETHON_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

MAX_MESSAGE_LENGTH = 3900     # с запасом от лимита Telegram в 4096 символов на текстовое сообщение
MAX_CAPTION_LENGTH = 1024     # лимит Telegram на подпись к фото/видео/файлу
DELAY_BETWEEN_MESSAGES = 2    # пауза (в секундах) между отправками

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


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def matches_keywords(text, keywords):
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def utf16_len(s):
    """Telegram считает позиции символов в единицах UTF-16 (эмодзи и т.п.
    занимают по 2 единицы) — обычная длина строки Python этого не учитывает."""
    return len(s.encode("utf-16-le")) // 2


def convert_entities(entities, offset_shift):
    """Переводит форматирование оригинального поста (жирный текст, ссылки)
    в формат Telegram Bot API, сдвигая позиции на длину нашей "шапки"."""
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


def telegram_api_call(method, data, files=None, max_retries=5):
    """Общая функция обращения к Telegram Bot API с повтором попытки,
    если Telegram временно ограничил частоту запросов (ошибка 429)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    for attempt in range(1, max_retries + 1):
        if files:
            for key in files:
                fileobj = files[key][1]
                if hasattr(fileobj, "seek"):
                    fileobj.seek(0)
            resp = requests.post(url, data=data, files=files, timeout=60)
        else:
            resp = requests.post(url, json=data, timeout=20)

        if resp.ok:
            return True

        resp_data = {}
        try:
            resp_data = resp.json()
        except ValueError:
            pass

        if resp.status_code == 429:
            wait_seconds = resp_data.get("parameters", {}).get("retry_after", 5) + 1
            print(f"    Превышен лимит запросов, жду {wait_seconds} сек. "
                  f"(попытка {attempt}/{max_retries})")
            time.sleep(wait_seconds)
            continue

        print(f"Ошибка запроса к Telegram ({method}):", resp.text)
        return False

    print("Не удалось выполнить запрос после нескольких попыток — пропускаю.")
    return False


def send_text(text, entities=None):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}
    if entities:
        payload["entities"] = entities
    return telegram_api_call("sendMessage", payload)


def send_media(client, msg, caption, entities=None):
    """Скачивает фото/видео/файл из поста и отправляет его боту с подписью."""
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
    data = {"chat_id": CHAT_ID, "caption": caption[:MAX_CAPTION_LENGTH]}
    if entities:
        data["caption_entities"] = json.dumps(entities)
    return telegram_api_call(method, data, files=files)


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


def send_found_post(client, channel, msg, keyword):
    link = f"https://t.me/{channel.lstrip('@')}/{msg.id}"
    header = f"🔎 Найдено слово: {keyword}\n📢 Канал: {channel}\n\n"
    body = msg.raw_text or ""
    full_text = f"{header}{body}\n\n{link}"

    has_media = bool(msg.photo or msg.video or msg.document)
    fits_as_caption = len(full_text) <= MAX_CAPTION_LENGTH

    if has_media and fits_as_caption:
        # Короткий пост — фото и текст одним сообщением, с форматированием и ссылками
        entities = convert_entities(msg.entities, utf16_len(header))
        send_media(client, msg, full_text, entities=entities)
        return

    if has_media:
        # Длинный пост — фото с коротким заголовком, текст отдельным сообщением
        short_caption = f"🔎 Найдено слово: {keyword}\n📢 Канал: {channel}"
        send_media(client, msg, short_caption)
        time.sleep(DELAY_BETWEEN_MESSAGES)

    parts = split_long_text(full_text, MAX_MESSAGE_LENGTH)
    if len(parts) == 1:
        entities = convert_entities(msg.entities, utf16_len(header))
        send_text(parts[0], entities=entities)
        time.sleep(DELAY_BETWEEN_MESSAGES)
    else:
        for part in parts:
            send_text(part)
            time.sleep(DELAY_BETWEEN_MESSAGES)


def main():
    config = load_json(CONFIG_FILE, {"channels": [], "keywords": []})
    state = load_json(STATE_FILE, {})

    channels = config.get("channels", [])
    keywords = config.get("keywords", [])

    if not channels or not keywords:
        print("Список каналов или ключевых слов пуст — нечего проверять. Заполните config.json.")
        return

    with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        for channel in channels:
            print(f"Проверяю канал: {channel}")
            last_id = state.get(channel, 0)
            max_id_seen = last_id
            found_count = 0

            try:
                messages = client.iter_messages(channel, min_id=last_id, reverse=True)
                for msg in messages:
                    if msg.id > max_id_seen:
                        max_id_seen = msg.id

                    keyword = matches_keywords(msg.raw_text, keywords)
                    if keyword:
                        found_count += 1
                        send_found_post(client, channel, msg, keyword)

            except Exception as e:
                print(f"Ошибка при обработке канала {channel}: {e}")
                continue

            state[channel] = max_id_seen
            print(f"  Готово. Найдено совпадений: {found_count}")

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
