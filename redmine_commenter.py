#!/usr/bin/env python3
# Версия: v1.10
"""
Добавляет комментарий к задаче Redmine через браузер.
Режимы: auto (по умолчанию), attach, launch.
В auto проверяется, отвечает ли порт как Chrome DevTools; если нет —
запускается выделенный экземпляр браузера с профилем скрипта.

Текст комментария берётся из файла comment_file (если задан) или из
comment_text. Драйвер ищется в папке скрипта: сначала chromedriver,
затем yandexdriver.
"""

import argparse
import configparser
import logging
import os
import shlex
import subprocess
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
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# --- Константы по умолчанию ---
DEFAULT_BROWSER_BINARY = "/usr/bin/chromium"
DEFAULT_DEBUGGER_PORT = 9222
DEFAULT_TIMEOUT = 15
DEFAULT_WAIT_AFTER_SUBMIT = 3
DEFAULT_HEADLESS = False
DEFAULT_CLOSE_AFTER = False

DEFAULT_NOTES_FIELD_ID = "issue_notes"
DEFAULT_SUBMIT_BUTTON_NAME = "commit"

DRIVER_NAMES = ("chromedriver", "yandexdriver")
SCRIPT_DIR = Path(__file__).resolve().parent
LOGIN_LOG_NAME = "chromium_login.log"
SCRIPT_LOG_NAME = "redmine_commenter.log"


def load_config(config_path: str) -> configparser.ConfigParser:
    """Загружает конфиг и проверяет обязательные параметры."""
    config = configparser.ConfigParser()
    try:
        read_files = config.read(config_path, encoding="utf-8")
        if not read_files:
            logging.error("Файл конфига не найден: %s", config_path)
            sys.exit(1)
    except configparser.Error as exc:
        logging.error("Ошибка разбора конфига: %s", exc)
        sys.exit(1)

    if "redmine" not in config:
        logging.error("В конфиге отсутствует секция [redmine]")
        sys.exit(1)
    if "browser" not in config:
        logging.error("В конфиге отсутствует секция [browser]")
        sys.exit(1)

    if not config.get("redmine", "issue_url", fallback="").strip():
        logging.error("В секции [redmine] не задан обязательный параметр 'issue_url'")
        sys.exit(1)
    if not config.get("browser", "mode", fallback="").strip():
        logging.error("В секции [browser] не задан обязательный параметр 'mode'")
        sys.exit(1)

    comment_text = config.get("redmine", "comment_text", fallback="").strip()
    comment_file = config.get("redmine", "comment_file", fallback="").strip()
    if not comment_text and not comment_file:
        logging.error(
            "В секции [redmine] должен быть задан либо 'comment_text', либо 'comment_file'"
        )
        sys.exit(1)

    mode = config.get("browser", "mode").strip().lower()
    if mode not in ("auto", "attach", "launch"):
        logging.error("Параметр [browser] mode должен быть auto, attach или launch, получено: %s", mode)
        sys.exit(1)

    return config


def read_comment_text(config: configparser.ConfigParser) -> str:
    """Читает текст комментария из comment_file (приоритет) или comment_text."""
    comment_file = config.get("redmine", "comment_file", fallback="").strip()
    if comment_file:
        path = Path(comment_file)
        if not path.is_absolute():
            path = SCRIPT_DIR / path
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logging.error("Не удалось прочитать comment_file %s: %s", path, exc)
            sys.exit(1)
        except UnicodeDecodeError as exc:
            logging.error("comment_file %s не в UTF-8: %s", path, exc)
            sys.exit(1)
        if not text:
            logging.error("Файл comment_file %s пустой.", path)
            sys.exit(1)
        logging.info("Текст комментария прочитан из файла: %s (%d символов)", path, len(text))
        return text

    text = config.get("redmine", "comment_text", fallback="").strip()
    logging.info("Текст комментария взят из comment_text (%d символов)", len(text))
    return text


def resolve_driver_path(config: configparser.ConfigParser):
    """Ищет драйвер: chromedriver → yandexdriver в папке скрипта → driver_path → None."""
    for name in DRIVER_NAMES:
        candidate = SCRIPT_DIR / name
        if candidate.is_file():
            if os.access(str(candidate), os.X_OK):
                logging.info("Использую драйвер из папки скрипта: %s", candidate)
                return str(candidate)
            logging.warning(
                "Найден %s, но файл не исполняемый. Выполните: chmod +x %s. "
                "Пробую следующий источник.",
                candidate, candidate,
            )
    from_config = config.get("browser", "driver_path", fallback="").strip()
    if from_config:
        logging.info("Использую драйвер из конфига: %s", from_config)
        return from_config
    logging.info("Драйвер не найден в папке скрипта и не задан в конфиге — используется Selenium Manager.")
    return None


def resolve_profile_dir(config: configparser.ConfigParser) -> str:
    """Возвращает абсолютный путь к выделенному профилю скрипта."""
    raw = config.get("browser", "profile_dir", fallback="./browser_profile").strip()
    path = Path(raw) if Path(raw).is_absolute() else SCRIPT_DIR / raw
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def is_debugger_port_ready(port: int) -> bool:
    """Проверяет, отвечает ли порт как отладочный интерфейс Chrome DevTools."""
    url = f"http://localhost:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return resp.status == 200 and "Browser" in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def build_driver(config: configparser.ConfigParser):
    """Создаёт WebDriver. Возвращает (driver, was_launched_here)."""
    mode = config.get("browser", "mode").strip().lower()
    port = config.getint("browser", "debugger_port", fallback=DEFAULT_DEBUGGER_PORT)
    driver_path = resolve_driver_path(config)
    service = Service(executable_path=driver_path)

    port_ready = is_debugger_port_ready(port)
    logging.info("Режим %s, порт %s отладочный: %s", mode, port, "да" if port_ready else "нет")

    use_attach = (mode == "attach") or (mode == "auto" and port_ready)

    if use_attach:
        if not port_ready:
            logging.warning(
                "Режим attach, но порт %s не отвечает как DevTools. "
                "Если браузер не запущен с --remote-debugging-port=%s — "
                "смените mode на auto в config.ini.", port, port,
            )
        options = Options()
        options.debugger_address = f"localhost:{port}"
        try:
            return webdriver.Chrome(service=service, options=options), False
        except WebDriverException as exc:
            logging.error("Не удалось подключиться к браузеру: %s", exc)
            if mode == "attach":
                sys.exit(1)
            logging.info("Перехожу к запуску нового экземпляра.")

    binary = config.get("browser", "browser_binary", fallback=DEFAULT_BROWSER_BINARY).strip()
    profile_dir = resolve_profile_dir(config)
    options = Options()
    options.binary_location = binary
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument(f"--remote-debugging-port={port}")
    if config.getboolean("browser", "headless", fallback=DEFAULT_HEADLESS):
        options.add_argument("--headless=new")
    logging.info("Запускаю %s с профилем %s и портом отладки %s", binary, profile_dir, port)
    try:
        return webdriver.Chrome(service=service, options=options), True
    except WebDriverException as exc:
        logging.error("Не удалось создать WebDriver: %s", exc)
        logging.error(
            "Возможные причины: несовпадение версий драйвера и браузера "
            "(мажорные версии должны совпадать), конфликт профиля с уже "
            "запущенным экземпляром, браузер не принимает --remote-debugging-port."
        )
        sys.exit(1)


def show_update_form(driver: webdriver.Chrome, timeout: int) -> bool:
    """Делает видимой форму редактирования (#update) на странице задачи."""
    try:
        update = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.ID, "update"))
        )
    except TimeoutException:
        logging.error("Блок #update не найден на странице задачи.")
        return False

    if update.is_displayed():
        logging.info("Форма редактирования уже открыта.")
        return True

    try:
        driver.execute_script(
            "if (typeof showAndScrollTo === 'function') {"
            "  showAndScrollTo('update', 'issue_notes');"
            "} else {"
            "  var el = document.getElementById('update');"
            "  if (el) { el.style.display = ''; }"
            "}"
        )
        logging.info("Форма редактирования открыта через showAndScrollTo.")
    except WebDriverException as exc:
        logging.error("Не удалось открыть форму редактирования: %s", exc)
        return False

    try:
        WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.ID, "issue_notes"))
        )
    except TimeoutException:
        logging.error("Поле #issue_notes не стало видимым после showAndScrollTo.")
        return False

    return True


def fill_notes_field(driver: webdriver.Chrome, notes_field_id: str, text: str) -> bool:
    """Вводит текст в поле комментария. Fallback на JS, если send_keys блокируется."""
    try:
        notes = driver.find_element(By.ID, notes_field_id)
    except WebDriverException as exc:
        logging.error("Поле #%s не найдено: %s", notes_field_id, exc)
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", notes)
        time.sleep(0.2)
    except WebDriverException:
        pass

    try:
        notes.click()
    except WebDriverException:
        pass

    try:
        notes.clear()
        notes.send_keys(text)
        logging.info("Текст введён через send_keys.")
        return True
    except WebDriverException as exc:
        logging.warning("send_keys не удался (%s), пробую через JS.", exc)

    try:
        driver.execute_script(
            "var el = arguments[0]; el.value = arguments[1];"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
            "el.dispatchEvent(new Event('change', {bubbles: true}));",
            notes, text,
        )
        logging.info("Текст установлен через JavaScript.")
        return True
    except WebDriverException as exc:
        logging.error("JS-вставка не удалась: %s", exc)
        return False


def submit_form(driver: webdriver.Chrome, timeout: int) -> bool:
    """Нажимает кнопку «Отправить» именно в форме редактирования #issue-form."""
    try:
        button = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "#issue-form input[type='submit'][name='commit']")
            )
        )
        button.click()
        logging.info("Кнопка «Отправить» нажата.")
        return True
    except (TimeoutException, WebDriverException) as exc:
        logging.error("Не удалось нажать кнопку «Отправить»: %s", exc)
        return False


def verify_comment(driver: webdriver.Chrome, comment_text: str, timeout: int) -> bool:
    """Ждёт появления текста комментария в блоке истории задачи."""
    marker = comment_text[:50]
    try:
        WebDriverWait(driver, timeout).until(
            EC.text_to_be_present_in_element((By.ID, "history"), marker)
        )
        return True
    except TimeoutException:
        logging.warning("Текст не найден в #history за %s с.", timeout)
    try:
        return marker in driver.page_source
    except WebDriverException:
        return False


def add_comment(driver: webdriver.Chrome, config: configparser.ConfigParser) -> bool:
    """Открывает задачу, показывает форму, вводит текст, отправляет, проверяет."""
    issue_url = config.get("redmine", "issue_url").strip()
    timeout = config.getint("redmine", "timeout", fallback=DEFAULT_TIMEOUT)
    wait_after = config.getint("redmine", "wait_after_submit", fallback=DEFAULT_WAIT_AFTER_SUBMIT)
    notes_field_id = config.get("redmine", "notes_field_id", fallback=DEFAULT_NOTES_FIELD_ID).strip()

    comment_text = read_comment_text(config)

    try:
        logging.info("Открываю задачу: %s", issue_url)
        driver.get(issue_url)
    except WebDriverException as exc:
        logging.error("Не удалось открыть URL: %s", exc)
        return False

    if not show_update_form(driver, timeout):
        return False

    if not fill_notes_field(driver, notes_field_id, comment_text):
        logging.error("Не удалось ввести текст комментария.")
        return False

    if not submit_form(driver, timeout):
        return False

    time.sleep(wait_after)
    if verify_comment(driver, comment_text, timeout):
        logging.info("Комментарий добавлен.")
        return True
    logging.warning("Текст комментария не найден на странице после отправки.")
    return False


def run_login(config: configparser.ConfigParser) -> None:
    """Открывает выделенный профиль для одноразового входа в Redmine."""
    logging.info("Старт run_login.")
    binary = config.get("browser", "browser_binary", fallback=DEFAULT_BROWSER_BINARY).strip()
    profile_dir = resolve_profile_dir(config)
    issue_url = config.get("redmine", "issue_url").strip()
    log_path = Path(profile_dir) / LOGIN_LOG_NAME

    extra_args = config.get("browser", "login_extra_args", fallback="").strip()
    extra = shlex.split(extra_args) if extra_args else []

    cmd = [binary, f"--user-data-dir={profile_dir}"] + extra + [issue_url]
    logging.info("Команда запуска: %s", " ".join(shlex.quote(x) for x in cmd))
    logging.info("Лог Chromium: %s", log_path)

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
    except FileNotFoundError:
        logging.error("Не найден исполняемый файл браузера: %s", binary)
        sys.exit(1)
    except Exception as exc:
        logging.error("Ошибка при запуске браузера: %s", exc)
        sys.exit(1)

    print(f"Браузер запущен (pid={proc.pid}).")
    print(f"Лог Chromium: {log_path}")
    print("Войдите в Redmine в открытом окне браузера.")
    print("Когда закончите вход (или если браузер закрылся) — нажмите Enter.")
    input()
    exit_code = proc.poll()
    if exit_code is None:
        logging.info("Браузер всё ещё работает (pid=%s).", proc.pid)
    else:
        logging.warning("Браузер завершился с кодом %s. Лог Chromium: %s", exit_code, log_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Добавление комментария в Redmine через браузер")
    parser.add_argument("--config", default="config.ini", help="Путь к config.ini (по умолчанию config.ini в текущем каталоге)")
    parser.add_argument("--login", action="store_true", help="Одноразовый вход в Redmine")
    args = parser.parse_args()

    script_log = SCRIPT_DIR / SCRIPT_LOG_NAME
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(script_log, mode="w", encoding="utf-8"),
        ],
    )
    logging.info("=== Запуск redmine_commenter (v1.10), args=%s ===", vars(args))

    config = load_config(args.config)
    if args.login:
        run_login(config)
        sys.exit(0)

    driver = None
    was_launched_here = False
    success = False
    try:
        driver, was_launched_here = build_driver(config)
        success = add_comment(driver, config)
    except Exception as exc:
        logging.error("Непредвиденная ошибка: %s", exc)
    finally:
        if driver is not None:
            close_after = config.getboolean("browser", "close_after", fallback=DEFAULT_CLOSE_AFTER)
            if was_launched_here and close_after:
                driver.quit()
                logging.info("Браузер закрыт (close_after = true).")
            else:
                logging.info("Браузер оставлен открытым.")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()