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

MAX_MESSAGE_LENGTH = 3900     # с запасом от лимита Telegram в 4096 символов на сообщение
DELAY_BETWEEN_MESSAGES = 2    # пауза (в секундах) между отправками, чтобы не упираться в лимиты Telegram


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


def send_to_telegram(text, max_retries=5):
    """Отправляет сообщение боту. Если Telegram временно ограничил частоту
    запросов (ошибка 429), ждёт нужное время и повторяет попытку, чтобы
    сообщение не терялось."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": False}

    for attempt in range(1, max_retries + 1):
        resp = requests.post(url, json=payload, timeout=20)
        if resp.ok:
            return True

        data = {}
        try:
            data = resp.json()
        except ValueError:
            pass

        if resp.status_code == 429:
            wait_seconds = data.get("parameters", {}).get("retry_after", 5) + 1
            print(f"    Превышен лимит запросов, жду {wait_seconds} сек. "
                  f"(попытка {attempt}/{max_retries})")
            time.sleep(wait_seconds)
            continue

        print("Ошибка отправки в Telegram:", resp.text)
        return False

    print("Не удалось отправить сообщение после нескольких попыток — пропускаю.")
    return False


def split_long_text(text, limit):
    """Если текст поста длиннее лимита Telegram — режем на несколько сообщений
    по границам строк, чтобы не обрывать текст на середине слова."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
    parts.append(text)
    return parts


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
            last_id = state.get(channel, 0)   # 0 = ещё ни разу не проверяли (читаем всю историю)
            max_id_seen = last_id
            found_count = 0

            try:
                # min_id=last_id — берём только сообщения новее уже проверенных
                # reverse=True — идём от старых к новым по порядку
                messages = client.iter_messages(channel, min_id=last_id, reverse=True)
                for msg in messages:
                    if msg.id > max_id_seen:
                        max_id_seen = msg.id

                    # raw_text — обычный читаемый текст поста, без служебных
                    # символов разметки markdown (**, [], и т.п.)
                    keyword = matches_keywords(msg.raw_text, keywords)
                    if keyword:
                        found_count += 1
                        link = f"https://t.me/{channel.lstrip('@')}/{msg.id}"
                        header = f"🔎 Найдено слово: {keyword}\n📢 Канал: {channel}\n\n"
                        body = msg.raw_text or ""
                        full_text = f"{header}{body}\n\n{link}"

                        for part in split_long_text(full_text, MAX_MESSAGE_LENGTH):
                            send_to_telegram(part)
                            time.sleep(DELAY_BETWEEN_MESSAGES)

            except Exception as e:
                print(f"Ошибка при обработке канала {channel}: {e}")
                continue

            state[channel] = max_id_seen
            print(f"  Готово. Найдено совпадений: {found_count}")

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
