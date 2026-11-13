#!/usr/bin/env python3
# Версия: v2.6
"""
Добавляет комментарии к нескольким задачам Redmine через браузер.
Задачи задаются секциями [task:<метка>]. Для каждой задачи текст
собирается из comment_files или comment_text.

Перед задачей могут запускаться парсеры (секции [prepare:<имя>]).
Список парсеров для задачи — параметр prepare. Если в [redmine] задан
prepare_all = true, все парсеры, на которые ссылаются задачи, запускаются
до цикла. Один и тот же парсер в рамках одного прогона не запускается
дважды (кэш).

Файлы из comment_files могут содержать разделитель "--- SENT ---".
В этом случае для отправки берётся ТОЛЬКО часть до разделителя (таблица pending).
Сам разделитель и часть после него (sent) в комментарий Redmine не попадают.
После успешной отправки pending перемещается под разделитель (sent).
Это позволяет не отправлять одни и те же ссылки повторно.

Длинный текст разбивается на части (max_comment_length), каждая часть
отправляется отдельным комментарием. Это обходит ограничение Redmine
на размер поля notes (65535 символов). Ввод текста выполняется через
JavaScript, что мгновенно и не зависит от длины текста.

Режимы работы:
  (без флагов)         — обычный прогон по задачам;
  --login              — одноразовый вход в Redmine;
  --prepare <имя>      — запустить один парсер и выйти, без отправки комментариев;
  --prepare-all        — запустить все парсеры из [prepare:*] и выйти.

По умолчанию конфиг ищется в файле config.ini рядом со скриптом.
Путь можно переопределить аргументом --config.
"""

import argparse
import configparser
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
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
DEFAULT_TASK_PAUSE = 2
DEFAULT_HEADLESS = False
DEFAULT_CLOSE_AFTER = False
DEFAULT_PARSER_TIMEOUT = 600

DEFAULT_NOTES_FIELD_ID = "issue_notes"
DEFAULT_SUBMIT_BUTTON_NAME = "commit"

# По умолчанию ограничиваем размер одного комментария, чтобы не упереться
# в лимит Redmine (65535 символов).
DEFAULT_MAX_COMMENT_LENGTH = 50000

SENT_MARKER = "--- SENT ---"

DRIVER_NAMES = ("chromedriver", "yandexdriver")
SCRIPT_DIR = Path(__file__).resolve().parent
LOGIN_LOG_NAME = "chromium_login.log"
SCRIPT_LOG_NAME = "redmine_commenter.log"
TASK_SECTION_PREFIX = "task:"
PREPARE_SECTION_PREFIX = "prepare:"


@dataclass
class Task:
    """Одна задача: метка, URL, файлы с текстом, inline-текст и список парсеров."""
    label: str
    issue_url: str
    comment_files: list
    inline_text: str
    source: str
    prepare: list = field(default_factory=list)


@dataclass
class PrepareSpec:
    """Описание парсера: имя, путь к скрипту, доп. аргументы, таймаут."""
    name: str
    script: Path
    args: list
    timeout: int


def load_config(config_path: str) -> configparser.ConfigParser:
    """Загружает конфиг и проверяет базовые секции и параметры."""
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
    if not config.get("browser", "mode", fallback="").strip():
        logging.error("В секции [browser] не задан обязательный параметр 'mode'")
        sys.exit(1)

    mode = config.get("browser", "mode").strip().lower()
    if mode not in ("auto", "attach", "launch"):
        logging.error("Параметр [browser] mode должен быть auto, attach или launch, получено: %s", mode)
        sys.exit(1)

    return config


def parse_prepare_sections(config: configparser.ConfigParser) -> dict:
    """Собирает описания всех парсеров из секций [prepare:*]."""
    specs = {}
    for section in config.sections():
        if not section.startswith(PREPARE_SECTION_PREFIX):
            continue
        name = section[len(PREPARE_SECTION_PREFIX):].strip()
        if not name:
            logging.error("Секция [%s]: пустое имя парсера", section)
            sys.exit(1)
        script_raw = config.get(section, "script", fallback="").strip()
        if not script_raw:
            logging.error("Секция [%s]: не задан обязательный параметр 'script'", section)
            sys.exit(1)
        script = Path(script_raw)
        if not script.is_absolute():
            script = SCRIPT_DIR / script
        args_raw = config.get(section, "args", fallback="").strip()
        args = shlex.split(args_raw) if args_raw else []
        timeout = config.getint(section, "timeout", fallback=DEFAULT_PARSER_TIMEOUT)
        specs[name] = PrepareSpec(name=name, script=script, args=args, timeout=timeout)
        logging.info("[prepare:%s] скрипт=%s, timeout=%d", name, script, timeout)
    return specs


def parse_tasks(config: configparser.ConfigParser) -> list:
    """Собирает задачи из [task:*]. Валидирует URL, файлы и prepare."""
    task_sections = [s for s in config.sections() if s.startswith(TASK_SECTION_PREFIX)]
    if not task_sections:
        logging.error(
            "В конфиге нет ни одной секции [task:<метка>]. "
            "Добавьте хотя бы одну. Подробнее — раздел «Настройка задач» в readme.md."
        )
        sys.exit(1)

    tasks = []
    for section in task_sections:
        label = section[len(TASK_SECTION_PREFIX):].strip() or section
        issue_url = config.get(section, "issue_url", fallback="").strip()
        if not issue_url:
            logging.error("Секция [%s]: не задан обязательный параметр 'issue_url'", section)
            sys.exit(1)

        prepare_raw = config.get(section, "prepare", fallback="").strip()
        prepare_list = [p.strip() for p in prepare_raw.split(",") if p.strip()] if prepare_raw else []
        for pname in prepare_list:
            prepare_section = f"{PREPARE_SECTION_PREFIX}{pname}"
            if not config.has_section(prepare_section):
                logging.error(
                    "Секция [%s]: указан prepare=%s, но секция [%s] отсутствует",
                    section, pname, prepare_section,
                )
                sys.exit(1)

        files_raw = config.get(section, "comment_files", fallback="")
        file_entries = [line.strip() for line in files_raw.splitlines() if line.strip()]
        inline = config.get(section, "comment_text", fallback="").strip()

        if not file_entries and not inline:
            logging.error(
                "Секция [%s]: задайте 'comment_files' или 'comment_text'.", section
            )
            sys.exit(1)

        resolved = []
        for entry in file_entries:
            p = Path(entry)
            if not p.is_absolute():
                p = SCRIPT_DIR / p
            if not p.is_file():
                logging.error("Секция [%s]: файл не найден — %s", section, p)
                sys.exit(1)
            resolved.append(p)

        if resolved:
            source = "файлы: " + ", ".join(p.name for p in resolved)
        else:
            source = "inline comment_text"

        prepare_str = ", ".join(prepare_list) if prepare_list else "—"
        tasks.append(Task(label=label, issue_url=issue_url, comment_files=resolved,
                          inline_text=inline, source=source, prepare=prepare_list))
        logging.info("[%s] задача принята: %s (%s, prepare=%s)",
                     label, issue_url, source, prepare_str)

    return tasks


def has_sent_marker(path: Path) -> bool:
    """Проверяет, содержит ли файл разделитель отправленных комментариев."""
    try:
        return SENT_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def read_comment_file(path: Path) -> tuple:
    """Читает файл с текстом комментария.
    
    Возвращает (текст, has_marker, is_empty_pending).
    Если файл содержит разделитель, возвращает ТОЛЬКО часть до него (pending).
    Сам разделитель и часть после него в текст комментария не попадают.
    Если pending пуст (нет строк данных), возвращает ("", True, True).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"не удалось прочитать {path}: {exc}")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} не в UTF-8: {exc}")

    if SENT_MARKER in content:
        parts = content.split(SENT_MARKER, 1)
        pending = parts[0].strip()
        lines = [l for l in pending.splitlines()
                 if l.strip().startswith("|") and not l.strip().startswith("|_.")]
        if not lines:
            return "", True, True
        return pending, True, False
    else:
        return content.strip(), False, False


def mark_file_sent(path: Path) -> None:
    """Перемещает pending-часть файла под разделитель (в sent)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logging.error("Не удалось прочитать %s для пометки отправки: %s", path, exc)
        return

    if SENT_MARKER not in content:
        return

    parts = content.split(SENT_MARKER, 1)
    pending_part = parts[0]
    sent_part = parts[1] if len(parts) > 1 else ""

    pending_lines = pending_part.splitlines()
    header_lines = []
    data_lines = []
    for line in pending_lines:
        stripped = line.strip()
        if stripped.startswith("|_."):
            header_lines.append(line)
        elif stripped.startswith("|"):
            data_lines.append(line)

    if not data_lines:
        return

    sent_lines = [l for l in sent_part.splitlines() if l.strip().startswith("|")]
    new_sent_lines = sent_lines + data_lines

    new_pending = "\n".join(header_lines) + "\n" if header_lines else ""
    new_sent = "\n".join(new_sent_lines) + "\n"
    new_content = new_pending + SENT_MARKER + "\n" + new_sent

    try:
        path.write_text(new_content, encoding="utf-8")
        logging.info("Файл %s: %d строк перемещено в отправленные", path, len(data_lines))
    except OSError as exc:
        logging.error("Не удалось записать %s: %s", path, exc)


def assemble_comment(task: Task) -> tuple:
    """Собирает текст комментария из файлов или inline.
    
    Возвращает (текст, причина_пропуска).
    Если причина_пропуска не None — задачу нужно пропустить (не ошибка).
    """
    if task.comment_files:
        parts = []
        has_marker_files = 0
        empty_marker_files = 0
        for path in task.comment_files:
            try:
                text, has_marker, is_empty = read_comment_file(path)
            except ValueError as exc:
                raise ValueError(str(exc))
            if has_marker:
                has_marker_files += 1
                if is_empty:
                    empty_marker_files += 1
                    continue
            if not text:
                raise ValueError(f"файл {path} пуст")
            parts.append(text)

        if not parts:
            if has_marker_files > 0 and empty_marker_files == has_marker_files:
                return "", "все файлы с маркером не содержат новых записей"
            raise ValueError("все файлы пусты")

        return "\n\n".join(parts), None
    else:
        if not task.inline_text:
            raise ValueError("пустой inline-текст")
        return task.inline_text, None


def split_comment_text(text: str, max_length: int) -> list:
    """Разбивает текст на части, не превышающие max_length символов.
    
    Старается резать по границам строк, чтобы не разрывать таблицу или абзац.
    """
    if max_length <= 0 or len(text) <= max_length:
        return [text]

    parts = []
    lines = text.splitlines(keepends=True)
    current_chunk = []

    for line in lines:
        # Если одна строка сама по себе длиннее лимита — режем её принудительно.
        if len(line) > max_length:
            if current_chunk:
                parts.append("".join(current_chunk))
                current_chunk = []
            for i in range(0, len(line), max_length):
                parts.append(line[i:i + max_length])
            continue

        # Если добавление строки превысит лимит — начинаем новый блок.
        if sum(len(l) for l in current_chunk) + len(line) > max_length:
            parts.append("".join(current_chunk))
            current_chunk = [line]
        else:
            current_chunk.append(line)

    if current_chunk:
        parts.append("".join(current_chunk))

    return parts


def _tail(text: str, lines: int = 20) -> str:
    """Возвращает последние `lines` строк текста."""
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


def run_parser(spec: PrepareSpec, config_path: Path) -> bool:
    """Запускает парсер. Возвращает True при коде 0, иначе False."""
    if not spec.script.is_file():
        logging.error("[prepare:%s] скрипт не найден: %s", spec.name, spec.script)
        return False
    if spec.script.suffix == ".py":
        base = [sys.executable, str(spec.script)]
    else:
        if not os.access(str(spec.script), os.X_OK):
            logging.error("[prepare:%s] скрипт не исполняемый: %s", spec.name, spec.script)
            return False
        base = [str(spec.script)]

    cmd = base + ["--config", str(config_path)] + spec.args
    logging.info("[prepare:%s] запуск: %s", spec.name, " ".join(shlex.quote(x) for x in cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            timeout=spec.timeout,
        )
    except subprocess.TimeoutExpired:
        logging.error("[prepare:%s] таймаут %d с", spec.name, spec.timeout)
        return False
    except OSError as exc:
        logging.error("[prepare:%s] не удалось запустить: %s", spec.name, exc)
        return False

    if proc.returncode != 0:
        logging.error("[prepare:%s] завершился с кодом %d", spec.name, proc.returncode)
        if proc.stderr.strip():
            logging.error("[prepare:%s] stderr (последние строки):\n%s",
                          spec.name, _tail(proc.stderr))
        return False

    logging.info("[prepare:%s] успешно завершён", spec.name)
    if proc.stdout.strip():
        logging.info("[prepare:%s] вывод (последние строки):\n%s",
                     spec.name, _tail(proc.stdout, 5))
    return True


def _ordered_parsers_for_tasks(tasks: list, prepare_specs: dict) -> list:
    """Уникальные имена парсеров, на которые ссылаются задачи, в порядке [prepare:*]."""
    referenced = set()
    for t in tasks:
        referenced.update(t.prepare)
    return [name for name in prepare_specs if name in referenced]


def _ensure_parsers(task: Task, prepare_specs: dict, config_path: Path,
                    parser_cache: set, parser_status: dict) -> bool:
    """Гарантирует, что все парсеры задачи запущены и успешны (с кэшем на прогон)."""
    for name in task.prepare:
        if name in parser_cache:
            if not parser_status.get(name, False):
                return False
            continue
        spec = prepare_specs.get(name)
        if spec is None:
            logging.error("[%s] нет секции [prepare:%s]", task.label, name)
            parser_status[name] = False
            parser_cache.add(name)
            return False
        logging.info("[%s] запускаю парсер %s", task.label, name)
        ok = run_parser(spec, config_path)
        parser_status[name] = ok
        parser_cache.add(name)
        if not ok:
            return False
    return True


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


def show_update_form(driver: webdriver.Chrome, timeout: int, prefix: str = "") -> bool:
    """Делает видимой форму редактирования (#update) на странице задачи."""
    try:
        update = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.ID, "update"))
        )
    except TimeoutException:
        logging.error("%sБлок #update не найден на странице задачи.", prefix)
        return False

    if update.is_displayed():
        logging.info("%sФорма редактирования уже открыта.", prefix)
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
        logging.info("%sФорма редактирования открыта.", prefix)
    except WebDriverException as exc:
        logging.error("%sНе удалось открыть форму редактирования: %s", prefix, exc)
        return False

    try:
        WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.ID, "issue_notes"))
        )
    except TimeoutException:
        logging.error("%sПоле #issue_notes не стало видимым после showAndScrollTo.", prefix)
        return False

    return True


def fill_notes_field(driver: webdriver.Chrome, notes_field_id: str, text: str, prefix: str = "") -> bool:
    """Вводит текст в поле комментария через JavaScript.
    
    send_keys не используется: для больших текстов он работает очень медленно
    и может обрываться. Прямая установка value через JS срабатывает мгновенно
    и не зависит от объёма текста.
    """
    try:
        notes = driver.find_element(By.ID, notes_field_id)
    except WebDriverException as exc:
        logging.error("%sПоле #%s не найдено: %s", prefix, notes_field_id, exc)
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", notes)
        time.sleep(0.2)
    except WebDriverException:
        pass

    try:
        driver.execute_script(
            "var el = arguments[0]; el.value = arguments[1];"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
            "el.dispatchEvent(new Event('change', {bubbles: true}));",
            notes, text,
        )
        logging.info("%sТекст установлен через JavaScript (%d символов).", prefix, len(text))
        return True
    except WebDriverException as exc:
        logging.error("%sJS-вставка не удалась: %s", prefix, exc)
        return False


def submit_form(driver: webdriver.Chrome, timeout: int, prefix: str = "") -> bool:
    """Нажимает кнопку «Отправить» именно в форме редактирования #issue-form."""
    try:
        button = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "#issue-form input[type='submit'][name='commit']")
            )
        )
        button.click()
        logging.info("%sКнопка «Отправить» нажата.", prefix)
        return True
    except (TimeoutException, WebDriverException) as exc:
        logging.error("%sНе удалось нажать кнопку «Отправить»: %s", prefix, exc)
        return False


def verify_comment(driver: webdriver.Chrome, comment_text: str, timeout: int, prefix: str = "") -> bool:
    """Ждёт появления текста комментария в блоке истории задачи."""
    marker = comment_text[:50]
    try:
        WebDriverWait(driver, timeout).until(
            EC.text_to_be_present_in_element((By.ID, "history"), marker)
        )
        return True
    except TimeoutException:
        logging.warning("%sТекст не найден в #history за %s с.", prefix, timeout)
    try:
        return marker in driver.page_source
    except WebDriverException:
        return False


def process_task(driver: webdriver.Chrome, task: Task, comment_text: str,
                 config: configparser.ConfigParser) -> bool:
    """Обрабатывает одну задачу: открыть URL, показать форму, ввести, отправить, проверить.
    
    Если текст длиннее max_comment_length, он разбивается на несколько частей,
    каждая отправляется отдельным комментарием.
    """
    timeout = config.getint("redmine", "timeout", fallback=DEFAULT_TIMEOUT)
    wait_after = config.getint("redmine", "wait_after_submit", fallback=DEFAULT_WAIT_AFTER_SUBMIT)
    notes_field_id = config.get("redmine", "notes_field_id", fallback=DEFAULT_NOTES_FIELD_ID).strip()
    max_length = config.getint("redmine", "max_comment_length", fallback=DEFAULT_MAX_COMMENT_LENGTH)
    prefix = f"[{task.label}] "

    parts = split_comment_text(comment_text, max_length)
    total_parts = len(parts)
    if total_parts > 1:
        logging.info("%sТекст разбит на %d частей (лимит %d символов).",
                     prefix, total_parts, max_length)

    # Открываем задачу один раз. Форма переиспользуется для всех частей.
    try:
        logging.info("%sОткрываю задачу: %s", prefix, task.issue_url)
        driver.get(task.issue_url)
    except WebDriverException as exc:
        logging.error("%sНе удалось открыть URL: %s", prefix, exc)
        return False

    if not show_update_form(driver, timeout, prefix):
        return False

    for idx, part in enumerate(parts, 1):
        part_prefix = f"{prefix}({idx}/{total_parts}) " if total_parts > 1 else prefix

        # Перед каждой частью (кроме первой) нужно снова открыть форму:
        # после отправки страница перезагружается, и #update снова скрывается.
        if idx > 1:
            if not show_update_form(driver, timeout, part_prefix):
                return False

        if not fill_notes_field(driver, notes_field_id, part, part_prefix):
            logging.error("%sНе удалось ввести текст части %d.", part_prefix, idx)
            return False

        if not submit_form(driver, timeout, part_prefix):
            return False

        time.sleep(wait_after)
        if not verify_comment(driver, part, timeout, part_prefix):
            logging.warning("%sТекст части %d не найден на странице после отправки.", part_prefix, idx)
            return False

        logging.info("%sЧасть %d отправлена.", part_prefix, idx)

    logging.info("%sВсе части отправлены.", prefix)
    return True


def run_tasks(config: configparser.ConfigParser, tasks: list, prepare_specs: dict,
              config_path: Path) -> int:
    """Прогон по задачам. Возвращает код выхода."""
    prepare_all = config.getboolean("redmine", "prepare_all", fallback=False)
    parser_cache = set()
    parser_status = {}

    if prepare_all:
        ordered = _ordered_parsers_for_tasks(tasks, prepare_specs)
        if ordered:
            logging.info("prepare_all=true: запуск парсеров до цикла (%d)", len(ordered))
            for name in ordered:
                spec = prepare_specs.get(name)
                if spec is None:
                    logging.error("Задача ссылается на несуществующий парсер [prepare:%s]", name)
                    parser_status[name] = False
                else:
                    parser_status[name] = run_parser(spec, config_path)
                parser_cache.add(name)
        else:
            logging.info("prepare_all=true, но ни одна задача не ссылается на парсеры")

    logging.info("Задач к обработке: %d", len(tasks))
    driver = None
    was_launched_here = False
    ok_count = 0
    failed_labels = []
    skipped_labels = []

    try:
        driver, was_launched_here = build_driver(config)
        pause = config.getint("redmine", "task_pause", fallback=DEFAULT_TASK_PAUSE)
        total = len(tasks)
        for idx, task in enumerate(tasks, 1):
            logging.info("=== Задача %d/%d [%s] ===", idx, total, task.label)

            if prepare_all:
                prep_ok = all(parser_status.get(n, False) for n in task.prepare)
            else:
                prep_ok = _ensure_parsers(task, prepare_specs, config_path, parser_cache, parser_status)

            if not prep_ok:
                logging.error("[%s] пропущена: парсер подготовки не отработал", task.label)
                failed_labels.append(task.label)
                if idx < total and pause > 0:
                    time.sleep(pause)
                continue

            try:
                comment_text, skip_reason = assemble_comment(task)
            except ValueError as exc:
                logging.error("[%s] %s", task.label, exc)
                failed_labels.append(task.label)
                if idx < total and pause > 0:
                    time.sleep(pause)
                continue

            if skip_reason:
                logging.info("[%s] пропущена: %s", task.label, skip_reason)
                skipped_labels.append(task.label)
                if idx < total and pause > 0:
                    time.sleep(pause)
                continue

            try:
                success = process_task(driver, task, comment_text, config)
            except Exception as exc:
                logging.error("[%s] Непредвиденная ошибка: %s", task.label, exc)
                success = False

            if success:
                ok_count += 1
                for path in task.comment_files:
                    if has_sent_marker(path):
                        mark_file_sent(path)
            else:
                failed_labels.append(task.label)

            if idx < total and pause > 0:
                logging.info("Пауза %d с перед следующей задачей.", pause)
                time.sleep(pause)
    finally:
        if driver is not None:
            close_after = config.getboolean("browser", "close_after", fallback=DEFAULT_CLOSE_AFTER)
            if was_launched_here and close_after:
                driver.quit()
                logging.info("Браузер закрыт (close_after = true).")
            else:
                logging.info("Браузер оставлен открытым.")

    total = len(tasks)
    logging.info("Итого: обработано %d, успешно %d, пропущено %d, с ошибкой %d",
                 total, ok_count, len(skipped_labels), len(failed_labels))
    if failed_labels:
        logging.error("Задачи с ошибками: %s", ", ".join(failed_labels))
        return 1
    return 0


def run_login(config: configparser.ConfigParser) -> None:
    """Открывает выделенный профиль для одноразового входа в Redmine."""
    logging.info("Старт run_login.")
    binary = config.get("browser", "browser_binary", fallback=DEFAULT_BROWSER_BINARY).strip()
    profile_dir = resolve_profile_dir(config)
    login_url = config.get("redmine", "login_url", fallback="").strip()
    log_path = Path(profile_dir) / LOGIN_LOG_NAME

    extra_args = config.get("browser", "login_extra_args", fallback="").strip()
    extra = shlex.split(extra_args) if extra_args else []

    cmd = [binary, f"--user-data-dir={profile_dir}"] + extra
    if login_url:
        cmd.append(login_url)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Добавление комментариев к задачам Redmine через браузер")
    parser.add_argument("--config", default="config.ini",
                        help="Путь к config.ini (по умолчанию config.ini в папке скрипта)")
    parser.add_argument("--login", action="store_true",
                        help="Одноразовый вход в Redmine")
    parser.add_argument("--prepare", metavar="NAME",
                        help="Запустить один парсер (секция [prepare:NAME]) и выйти, без отправки комментариев")
    parser.add_argument("--prepare-all", action="store_true",
                        help="Запустить все парсеры из секций [prepare:*] и выйти, без отправки комментариев")
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
    logging.info("=== Запуск redmine_commenter (v2.6), args=%s ===", vars(args))

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = SCRIPT_DIR / config_path
    config_path = config_path.resolve()
    logging.info("Конфиг: %s", config_path)

    config = load_config(str(config_path))

    if args.login:
        run_login(config)
        return 0

    if args.prepare:
        specs = parse_prepare_sections(config)
        spec = specs.get(args.prepare)
        if spec is None:
            logging.error("Нет секции [prepare:%s] в конфиге", args.prepare)
            return 1
        return 0 if run_parser(spec, config_path) else 1

    if args.prepare_all:
        specs = parse_prepare_sections(config)
        if not specs:
            logging.error("В конфиге нет ни одной секции [prepare:*]")
            return 1
        ok = True
        for name, spec in specs.items():
            logging.info("Запуск парсера %s", name)
            if not run_parser(spec, config_path):
                ok = False
        return 0 if ok else 1

    tasks = parse_tasks(config)
    prepare_specs = parse_prepare_sections(config)
    return run_tasks(config, tasks, prepare_specs, config_path)


if __name__ == "__main__":
    sys.exit(main())