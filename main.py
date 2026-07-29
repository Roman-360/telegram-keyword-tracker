import os
import json
import time
import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# --- Секретные данные читаются из переменных окружения (GitHub Secrets) ---
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELETHON_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

CONFIG_FILE = "config.json"   # список каналов и ключевых слов (редактируете вручную)
STATE_FILE = "state.json"     # "память" — до какого сообщения уже проверено (создаётся сама)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def matches_keywords(text, keywords):
    """Возвращает найденное ключевое слово или None, если совпадений нет."""
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}
    resp = requests.post(url, json=payload, timeout=20)
    if not resp.ok:
        print("Ошибка отправки в Telegram:", resp.text)


def main():
    config = load_json(CONFIG_FILE, {"channels": [], "keywords": []})
    state = load_json(STATE_FILE, {})

    channels = config.get("channels", [])
    keywords = config.get("keywords", [])

    if not channels or not keywords:
        print("Список каналов или ключевых слов пуст — нечего проверять. "
              "Заполните config.json.")
        return

    with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        for channel in channels:
            print(f"Проверяю канал: {channel}")
            last_id = state.get(channel, 0)          # 0 = ещё ни разу не проверяли (значит, читаем всю историю)
            max_id_seen = last_id
            found_count = 0

            try:
                # min_id=last_id — берём только сообщения новее того, что уже проверяли
                # reverse=True — идём от старых к новым, чтобы номера сообщений росли по порядку
                messages = client.iter_messages(channel, min_id=last_id, reverse=True)
                for msg in messages:
                    if msg.id > max_id_seen:
                        max_id_seen = msg.id

                    keyword = matches_keywords(msg.text, keywords)
                    if keyword:
                        found_count += 1
                        link = f"https://t.me/{channel.lstrip('@')}/{msg.id}"
                        preview = (msg.text or "")[:300]
                        text = (
                            f"🔎 Найдено слово: {keyword}\n"
                            f"📢 Канал: {channel}\n"
                            f"{preview}\n\n"
                            f"{link}"
                        )
                        send_to_telegram(text)
                        time.sleep(1)  # небольшая пауза, чтобы не перегружать Telegram API

            except Exception as e:
                print(f"Ошибка при обработке канала {channel}: {e}")
                continue

            state[channel] = max_id_seen
            print(f"  Готово. Найдено совпадений: {found_count}")

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
