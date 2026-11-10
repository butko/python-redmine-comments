#!/usr/bin/env python3
# Версия: v2.6
"""
Собирает ссылки на видео и Shorts с канала Rutube и сохраняет их в файл
в виде таблицы Textile (формат Redmine).
Ссылки очищаются от параметров плейлиста (остаётся только базовый URL).
Дата публикации и заголовок запрашиваются через официальный API Rutube.

Формат файла:
  1) таблица, подготовленная к отправке (pending);
  2) разделитель "--- SENT ---";
  3) ранее отправленные комментарии (sent).

Новые ссылки добавляются в pending. Ссылки, уже находящиеся в pending или sent,
повторно не добавляются. После успешной отправки redmine_commenter.py
перемещает pending под разделитель.

Логика сбора:
  - Скрипт явно открывает вкладки /videos/ и /shorts/.
  - Прокрутка через window.scrollTo.
  - Шаблон ссылки: строгий "/[0-9a-f]{32}", чтобы отсечь пагинацию и служебные страницы.
  - Остановка по любому условию:
    - достигнут лимит max_items новых записей;
    - исчерпан лимит max_duration (общий на прогон, секунды);
    - 15 прокруток подряд не дали новых ссылок;
    - количество ссылок в DOM стабилизировалось;
    - достигнут max_scrolls.
"""

import argparse
import configparser
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- Константы по умолчанию ---
DEFAULT_BROWSER_BINARY = "/usr/bin/chromium"
DEFAULT_TIMEOUT = 20
DEFAULT_SCROLL_PAUSE = 2
DEFAULT_MAX_SCROLLS = 200
DEFAULT_SORT_BY_DATE = True
DEFAULT_MAX_DURATION = 0
SENT_MARKER = "--- SENT ---"
DRIVER_NAMES = ("chromedriver", "yandexdriver")
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_LOG_NAME = "rutube_parser.log"
API_URL_TEMPLATE = "https://rutube.ru/api/video/{video_id}/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def load_config(config_path: str) -> configparser.ConfigParser:
    """Загружает и валидирует конфиг."""
    config = configparser.ConfigParser()
    try:
        read_files = config.read(config_path, encoding="utf-8")
        if not read_files:
            logging.error("Файл конфига не найден: %s", config_path)
            sys.exit(1)
    except configparser.Error as exc:
        logging.error("Ошибка разбора конфига: %s", exc)
        sys.exit(1)

    if "rutube" not in config:
        logging.error("В конфиге отсутствует секция [rutube]")
        sys.exit(1)

    if not config.get("rutube", "channel_url", fallback="").strip():
        logging.error("В секции [rutube] не задан 'channel_url'")
        sys.exit(1)

    if not config.get("rutube", "output_file", fallback="").strip():
        logging.error("В секции [rutube] не задан 'output_file'")
        sys.exit(1)

    return config


def resolve_driver_path(config: configparser.ConfigParser):
    """Ищет драйвер так же, как redmine_commenter."""
    for name in DRIVER_NAMES:
        candidate = SCRIPT_DIR / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            logging.info("Использую драйвер из папки скрипта: %s", candidate)
            return str(candidate)
    from_config = config.get("browser", "driver_path", fallback="").strip()
    if from_config:
        logging.info("Использую драйвер из конфига: %s", from_config)
        return from_config
    logging.info("Драйвер не найден — используется Selenium Manager.")
    return None


def build_driver(config: configparser.ConfigParser) -> webdriver.Chrome:
    """Создаёт headless-браузер для парсинга."""
    driver_path = resolve_driver_path(config)
    service = Service(executable_path=driver_path)
    options = Options()
    options.binary_location = config.get(
        "browser", "browser_binary", fallback=DEFAULT_BROWSER_BINARY
    ).strip()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")
    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(DEFAULT_TIMEOUT)
        return driver
    except WebDriverException as exc:
        logging.error("Не удалось создать WebDriver: %s", exc)
        sys.exit(1)


def normalize_channel_url(url: str) -> str:
    """Приводит URL канала к единому виду (без завершающего слэша)."""
    return url.strip().rstrip("/")


def get_channel_slug(driver: webdriver.Chrome, channel_url: str) -> str:
    """Возвращает слаг канала (часть после /u/) или его числовой ID."""
    logging.info("Открываю канал: %s", channel_url)
    try:
        driver.get(channel_url)
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except (TimeoutException, WebDriverException) as exc:
        logging.error("Не удалось открыть канал: %s", exc)
        sys.exit(1)

    current = driver.current_url.rstrip("/")
    logging.info("Текущий URL после перехода: %s", current)

    match = re.search(r"/channel/(\d+)/?", current)
    if match:
        channel_id = match.group(1)
        logging.info("ID канала: %s", channel_id)
        return channel_id

    match = re.search(r"/u/([^/]+)/?", current)
    if match:
        slug = match.group(1)
        logging.info("Слаг канала: %s", slug)
        return slug

    logging.error("Не удалось распознать ни ID, ни слаг канала из URL: %s", current)
    sys.exit(1)


def collect_from_page(
    driver: webdriver.Chrome,
    url: str,
    link_pattern: re.Pattern,
    scroll_pause: int,
    max_scrolls: int,
    max_items: int,
    deadline,
    collected: list,
    seen: set,
) -> int:
    """Прокручивает страницу и собирает уникальные ссылки."""
    logging.info("Открываю страницу: %s", url)
    try:
        driver.get(url)
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except (TimeoutException, WebDriverException) as exc:
        logging.error("Не удалось открыть %s: %s", url, exc)
        return 0

    added_count = 0
    last_dom_count = 0
    stable_rounds = 0
    no_progress_rounds = 0

    for i in range(1, max_scrolls + 1):
        if deadline is not None and time.time() > deadline:
            logging.warning(
                "Достигнут общий лимит времени (max_duration) — останавливаю сбор на %s.",
                url,
            )
            break

        anchors = driver.find_elements(By.TAG_NAME, "a")
        new_this_round = 0
        for a in anchors:
            href = a.get_attribute("href") or ""
            match = link_pattern.search(href)
            if not match:
                continue

            kind = match.group(1)
            video_id = match.group(2)

            if video_id in seen:
                continue

            seen.add(video_id)
            clean_url = f"https://rutube.ru/{kind}/{video_id}/"
            collected.append((video_id, clean_url))
            added_count += 1
            new_this_round += 1

            if max_items > 0 and len(collected) >= max_items:
                logging.info(
                    "Достигнут лимит max_items=%d (прокрутка %d). Останавливаюсь.",
                    max_items, i,
                )
                return added_count

        if new_this_round == 0:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(scroll_pause)

        dom_video_links = driver.find_elements(
            By.CSS_SELECTOR, "a[href*='/video/'], a[href*='/shorts/']"
        )
        new_dom_count = len(dom_video_links)

        if new_dom_count == last_dom_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_dom_count = new_dom_count

        logging.info(
            "Прокрутка %d: всего %d ссылок (добавлено %d), ссылок на видео в DOM %d",
            i, len(collected), new_this_round, new_dom_count,
        )

        if max_items > 0 and len(collected) >= max_items:
            logging.info(
                "Достигнут лимит max_items=%d (после прокрутки %d). Останавливаюсь.",
                max_items, i,
            )
            break

        if no_progress_rounds >= 15:
            logging.info("15 прокруток подряд без новых ссылок — останавливаю сбор.")
            break

        if stable_rounds >= 3:
            logging.info("Количество элементов в DOM стабилизировалось — завершаю прокрутку.")
            break

    return added_count


def fetch_publication_data(video_id: str) -> tuple:
    """Запрашивает заголовок и дату публикации через API Rutube."""
    if not video_id:
        return "", 0
    url = API_URL_TEMPLATE.format(video_id=video_id)

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://rutube.ru/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = str(data.get("title", "")).strip()

            raw_ts = data.get("publication_ts") or data.get("created_ts") or 0

            ts = 0
            if isinstance(raw_ts, (int, float)):
                ts = int(raw_ts)
            elif isinstance(raw_ts, str):
                try:
                    dt = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                    ts = int(dt.timestamp())
                except ValueError:
                    try:
                        ts = int(raw_ts)
                    except ValueError:
                        logging.warning("Не удалось распознать дату '%s' для видео %s", raw_ts, video_id)

            return title, ts
    except urllib.error.HTTPError as e:
        logging.warning("API HTTP %s для видео %s", e.code, video_id)
        return "", 0
    except urllib.error.URLError as e:
        logging.warning("API URL Error для видео %s: %s", video_id, e.reason)
        return "", 0
    except json.JSONDecodeError:
        logging.warning("API вернул не JSON для видео %s", video_id)
        return "", 0
    except Exception as e:
        logging.warning("API Неизвестная ошибка для видео %s: %s", video_id, e)
        return "", 0


def parse_date_to_ts(date_str: str) -> int:
    """Преобразует дату из таблицы (%d.%m.%Y) в timestamp."""
    if not date_str:
        return 0
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return int(dt.timestamp())
    except ValueError:
        return 0


def _parse_table_lines(lines: list) -> list:
    """Парсит строки таблицы Textile. Пропускает шапку и некорректные строки."""
    records = []
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("|"):
            continue
        if line.startswith("|_."):
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        date_str = parts[1].strip()
        title = parts[2].strip()
        url = parts[3].strip()
        if not url:
            continue
        m = re.search(r"/(video|shorts)/([0-9a-f]{32})/?", url)
        video_id = m.group(2) if m else ""
        records.append({
            "ts": parse_date_to_ts(date_str),
            "date_str": date_str,
            "title": title,
            "url": url,
            "video_id": video_id,
        })
    return records


def read_existing_table(output_path: Path) -> tuple:
    """Читает существующий файл: возвращает (pending, sent, known_ids)."""
    if not output_path.is_file():
        return [], [], set()

    try:
        lines = output_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logging.error("Не удалось прочитать существующий файл %s: %s", output_path, exc)
        sys.exit(1)

    pending_lines = []
    sent_lines = []
    in_sent = False
    for line in lines:
        if line.strip() == SENT_MARKER:
            in_sent = True
            continue
        if in_sent:
            sent_lines.append(line)
        else:
            pending_lines.append(line)

    pending_records = _parse_table_lines(pending_lines)
    sent_records = _parse_table_lines(sent_lines)

    known_ids = set()
    for r in pending_records + sent_records:
        if r["video_id"]:
            known_ids.add(r["video_id"])

    non_empty = [l for l in lines if l.strip() and l.strip() != SENT_MARKER]
    if non_empty and not pending_records and not sent_records:
        logging.error(
            "Файл %s существует и не пуст, но не содержит распознаваемых записей таблицы. "
            "Переименуйте или удалите его, чтобы начать заново.",
            output_path,
        )
        sys.exit(1)

    logging.info("Прочитано %d ожидающих и %d отправленных записей из %s",
                 len(pending_records), len(sent_records), output_path)
    return pending_records, sent_records, known_ids


def build_table_data(collected: list, sort_by_date: bool) -> list:
    """Обогащает список новых ссылок заголовками и датами. Сортирует, если нужно."""
    logging.info("Запрашиваю данные для %d новых ссылок...", len(collected))
    processed = []

    for idx, (video_id, clean_url) in enumerate(collected, 1):
        title, ts = fetch_publication_data(video_id)

        if ts > 0:
            date_str = datetime.fromtimestamp(ts).strftime('%d.%m.%Y')
        else:
            date_str = ""

        if not title:
            title = "Без названия"

        safe_title = title.replace("|", "&#124;").replace("\n", " ").replace("\r", " ")

        processed.append({
            "ts": ts,
            "date_str": date_str,
            "title": safe_title,
            "url": clean_url,
            "video_id": video_id,
        })

        if idx % 50 == 0:
            logging.info("  обработано %d/%d", idx, len(collected))

        time.sleep(0.1)

    if sort_by_date:
        processed.sort(key=lambda x: x["ts"], reverse=True)

    logging.info("Данные получены для %d новых ссылок.", len(processed))
    return processed


def save_table(pending_items: list, sent_items: list, output_path: Path) -> None:
    """Сохраняет таблицу в формате Textile (шапка + pending + маркер + sent)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("|_.Дата публикации|_.Заголовок|_.Ссылка|\n")
        for item in pending_items:
            f.write(f"|{item['date_str']}|{item['title']}|{item['url']}|\n")
        f.write(SENT_MARKER + "\n")
        for item in sent_items:
            f.write(f"|{item['date_str']}|{item['title']}|{item['url']}|\n")
    logging.info("Сохранено %d ожидающих и %d отправленных строк в %s",
                 len(pending_items), len(sent_items), output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Сбор ссылок с канала Rutube")
    parser.add_argument("--config", default="config.ini",
                        help="Путь к config.ini (по умолчанию config.ini в папке скрипта)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = SCRIPT_DIR / config_path

    script_log = SCRIPT_DIR / SCRIPT_LOG_NAME
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(script_log, mode="w", encoding="utf-8"),
        ],
    )
    logging.info("=== Запуск rutube_parser (v2.6) ===")
    logging.info("Конфиг: %s", config_path)

    config = load_config(str(config_path))
    channel_url = normalize_channel_url(config.get("rutube", "channel_url").strip())
    output_file = config.get("rutube", "output_file").strip()
    output_path = Path(output_file)
    if not output_path.is_absolute():
        output_path = SCRIPT_DIR / output_path
    scroll_pause = config.getint("rutube", "scroll_pause", fallback=DEFAULT_SCROLL_PAUSE)
    max_scrolls = config.getint("rutube", "max_scrolls", fallback=DEFAULT_MAX_SCROLLS)

    max_items_str = config.get("rutube", "max_items", fallback="").strip()
    max_items = 0
    if max_items_str:
        try:
            max_items = int(max_items_str)
            if max_items < 0:
                logging.error("Параметр max_items не может быть отрицательным: %s", max_items_str)
                sys.exit(1)
        except ValueError:
            logging.error("Параметр max_items должен быть целым числом, получено: %s", max_items_str)
            sys.exit(1)

    max_duration = config.getint("rutube", "max_duration", fallback=DEFAULT_MAX_DURATION)
    if max_duration < 0:
        logging.error("Параметр max_duration не может быть отрицательным: %d", max_duration)
        sys.exit(1)

    sort_by_date = config.getboolean("rutube", "sort_by_date", fallback=DEFAULT_SORT_BY_DATE)

    if max_items > 0:
        if sort_by_date:
            logging.info("Ограничение max_items: %d новых записей (применяется ПОСЛЕ сортировки)", max_items)
        else:
            logging.info("Ограничение max_items: %d новых записей (остановка при сборе)", max_items)
    else:
        logging.info("Ограничение max_items: не задано (добавляем все новые)")

    if max_duration > 0:
        logging.info("Лимит времени на прогон: %d с", max_duration)
    else:
        logging.info("Лимит времени на прогон: не задан")

    if sort_by_date:
        logging.info("Сортировка по дате публикации: включена (pending)")
    else:
        logging.info("Сортировка по дате публикации: отключена (pending сохраняет порядок)")

    existing_pending, existing_sent, known_ids = read_existing_table(output_path)

    deadline = time.time() + max_duration if max_duration > 0 else None

    driver = build_driver(config)
    try:
        identifier = get_channel_slug(driver, channel_url)

        # Строгий шаблон: ID видео и Shorts в Rutube — 32 hex-символа.
        # Это автоматически отсекает пагинацию (page-2) и служебные страницы.
        link_pattern = re.compile(r"/(video|shorts)/([0-9a-f]{32})")

        if identifier.isdigit():
            videos_url = f"https://rutube.ru/channel/{identifier}/videos/"
            shorts_url = f"https://rutube.ru/channel/{identifier}/shorts/"
        else:
            videos_url = f"https://rutube.ru/u/{identifier}/videos/"
            shorts_url = f"https://rutube.ru/u/{identifier}/shorts/"

        collected = []
        seen = set(known_ids)

        collect_limit = 0 if sort_by_date else max_items

        logging.info("Шаг 1: собираю с %s", videos_url)
        collect_from_page(
            driver, videos_url, link_pattern,
            scroll_pause, max_scrolls, collect_limit, deadline, collected, seen,
        )

        if not (not sort_by_date and max_items > 0 and len(collected) >= max_items):
            logging.info("Шаг 2: собираю с %s", shorts_url)
            collect_from_page(
                driver, shorts_url, link_pattern,
                scroll_pause, max_scrolls, collect_limit, deadline, collected, seen,
            )

        logging.info("Собрано новых ссылок: %d", len(collected))

        new_items = build_table_data(collected, sort_by_date)

        if sort_by_date:
            if max_items > 0 and len(new_items) > max_items:
                logging.info("Обрезаю новые записи до max_items=%d (самые новые из новых)", max_items)
                new_items = new_items[:max_items]
            all_pending = existing_pending + new_items
            all_pending.sort(key=lambda x: x["ts"], reverse=True)
        else:
            if max_items > 0 and len(new_items) > max_items:
                new_items = new_items[:max_items]
            all_pending = existing_pending + new_items

        save_table(all_pending, existing_sent, output_path)

    finally:
        driver.quit()

    return 0


if __name__ == "__main__":
    sys.exit(main())