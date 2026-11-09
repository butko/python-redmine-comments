#!/usr/bin/env python3
# Версия: v1.6
"""
Собирает ссылки на видео и Shorts с канала Rutube и сохраняет их в файл.
Сбор идёт с главной страницы канала (видео и Shorts в общем потоке, новые сверху).
Дополнительно запрашивает дату публикации через официальный API Rutube и
сортирует итоговый список по дате (самые новые сверху).

Если API недоступен, сохраняется порядок с главной страницы (он также
соответствует хронологии: новые сверху).
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
    """Возвращает слаг канала (часть после /u/), если URL имеет вид /u/<slug>/."""
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
    video_pattern: re.Pattern,
    shorts_pattern: re.Pattern,
    scroll_pause: int,
    max_scrolls: int,
    max_items: int,
    collected: list,
    seen: set,
) -> tuple:
    """Прокручивает страницу и добавляет найденные ссылки в общий список.

    Возвращает (общее_число_добавленных, сколько_добавлено_видео, сколько_добавлено_shorts).
    Если max_items > 0 — останавливается, как только в collected набирается
    max_items элементов.
    """
    logging.info("Открываю страницу: %s", url)
    try:
        driver.get(url)
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
    except (TimeoutException, WebDriverException) as exc:
        logging.error("Не удалось открыть %s: %s", url, exc)
        return 0, 0, 0

    added_video = 0
    added_shorts = 0
    last_height = 0
    stable_rounds = 0

    for i in range(1, max_scrolls + 1):
        anchors = driver.find_elements(By.TAG_NAME, "a")
        new_this_round = 0
        for a in anchors:
            href = a.get_attribute("href") or ""
            if href in seen:
                continue
            if video_pattern.search(href):
                collected.append(href)
                seen.add(href)
                added_video += 1
                new_this_round += 1
            elif shorts_pattern.search(href):
                collected.append(href)
                seen.add(href)
                added_shorts += 1
                new_this_round += 1

            if max_items > 0 and len(collected) >= max_items:
                logging.info(
                    "Достигнут лимит max_items=%d (прокрутка %d). Останавливаюсь.",
                    max_items, i,
                )
                return new_this_round, added_video, added_shorts

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(scroll_pause)

        new_height = driver.execute_script("return document.body.scrollHeight;")
        if new_height == last_height:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_height = new_height

        logging.info(
            "Прокрутка %d: всего %d ссылок (добавлено %d: видео %d, shorts %d), высота %s",
            i, len(collected), new_this_round, added_video, added_shorts, new_height,
        )

        if max_items > 0 and len(collected) >= max_items:
            logging.info(
                "Достигнут лимит max_items=%d (после прокрутки %d). Останавливаюсь.",
                max_items, i,
            )
            break

        if stable_rounds >= 3:
            logging.info("Высота страницы стабилизировалась — завершаю прокрутку.")
            break

    return len(collected), added_video, added_shorts


def extract_video_id(url: str) -> str:
    """Извлекает ID видео из URL Rutube (видео или shorts)."""
    match = re.search(r"rutube\.ru/(?:video|shorts)/([0-9a-f]+)", url)
    return match.group(1) if match else ""


def fetch_publication_ts(video_id: str) -> int:
    """Запрашивает дату публикации через API Rutube. Возвращает timestamp или 0."""
    if not video_id:
        return 0
    url = API_URL_TEMPLATE.format(video_id=video_id)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ts = data.get("publication_ts") or data.get("created_ts") or 0
            return int(ts)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return 0


def sort_by_publication_date(links: list) -> list:
    """Сортирует ссылки по дате публикации (самые новые сверху).

    Для каждой ссылки запрашивает дату через API. Если дату получить не удалось,
    элемент уходит в конец, сохраняя относительный порядок.
    """
    logging.info("Запрашиваю даты публикации для %d ссылок...", len(links))
    dated = []
    undated = []
    for idx, link in enumerate(links, 1):
        video_id = extract_video_id(link)
        ts = fetch_publication_ts(video_id)
        if ts > 0:
            dated.append((ts, link))
        else:
            undated.append(link)
        if idx % 50 == 0:
            logging.info("  обработано %d/%d", idx, len(links))
    dated.sort(key=lambda x: x[0], reverse=True)
    result = [link for _, link in dated] + undated
    logging.info("Даты получены для %d из %d ссылок.", len(dated), len(links))
    return result


def save_links(links: list, output_path: Path, as_markdown: bool = True) -> None:
    """Сохраняет ссылки в файл, по одной на строку, в заданном порядке."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for link in links:
            if as_markdown:
                f.write(f"* {link}\n")
            else:
                f.write(f"{link}\n")
    logging.info("Сохранено %d ссылок в %s", len(links), output_path)


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
    logging.info("=== Запуск rutube_parser (v1.6) ===")
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

    sort_by_date = config.getboolean("rutube", "sort_by_date", fallback=DEFAULT_SORT_BY_DATE)

    if max_items > 0:
        logging.info("Ограничение max_items: %d (общий лимит, видео + Shorts)", max_items)
    else:
        logging.info("Ограничение max_items: не задано (собираем все)")

    if sort_by_date:
        logging.info("Сортировка по дате публикации: включена")
    else:
        logging.info("Сортировка по дате публикации: отключена (порядок сбора)")

    driver = build_driver(config)
    try:
        identifier = get_channel_slug(driver, channel_url)

        video_pattern = re.compile(r"rutube\.ru/video/[0-9a-f]+")
        shorts_pattern = re.compile(r"rutube\.ru/shorts/[0-9a-f]+")

        main_url = (
            f"https://rutube.ru/channel/{identifier}/" if identifier.isdigit()
            else f"https://rutube.ru/u/{identifier}/"
        )

        collected = []
        seen = set()

        logging.info("Шаг 1: собираю с главной страницы канала (%s)", main_url)
        _, added_video, added_shorts = collect_from_page(
            driver, main_url, video_pattern, shorts_pattern,
            scroll_pause, max_scrolls, max_items, collected, seen,
        )
        logging.info(
            "После главной страницы: всего %d (видео %d, shorts %d)",
            len(collected), added_video, added_shorts,
        )

        if added_video == 0 and (max_items == 0 or len(collected) < max_items):
            videos_url = (
                f"https://rutube.ru/channel/{identifier}/videos/" if identifier.isdigit()
                else f"https://rutube.ru/u/{identifier}/videos/"
            )
            logging.info("Видео на главной не найдены — собираю с %s", videos_url)
            _, v_added, _ = collect_from_page(
                driver, videos_url, video_pattern, shorts_pattern,
                scroll_pause, max_scrolls, max_items, collected, seen,
            )
            added_video += v_added

        if added_shorts == 0 and (max_items == 0 or len(collected) < max_items):
            shorts_url = (
                f"https://rutube.ru/channel/{identifier}/shorts/" if identifier.isdigit()
                else f"https://rutube.ru/u/{identifier}/shorts/"
            )
            logging.info("Shorts на главной не найдены — собираю с %s", shorts_url)
            _, _, s_added = collect_from_page(
                driver, shorts_url, video_pattern, shorts_pattern,
                scroll_pause, max_scrolls, max_items, collected, seen,
            )
            added_shorts += s_added

        if max_items > 0 and len(collected) > max_items:
            logging.info("Обрезаю до max_items=%d", max_items)
            collected = collected[:max_items]

        if sort_by_date and collected:
            collected = sort_by_publication_date(collected)

        save_links(collected, output_path, as_markdown=True)
        logging.info(
            "Итого сохранено %d ссылок (видео %d, shorts %d)",
            len(collected), added_video, added_shorts,
        )
    finally:
        driver.quit()

    return 0


if __name__ == "__main__":
    sys.exit(main())