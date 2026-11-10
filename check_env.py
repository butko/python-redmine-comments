#!/usr/bin/env python3
# Версия: v1.0
"""
Проверка окружения для redmine_commenter.
Проверяет версию Python, наличие Selenium, браузера и драйвера.
Запускается перед основным скриптом или вручную.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def check_python_version() -> bool:
    """Проверяет, что версия Python не ниже 3.10."""
    print("Проверка версии Python...")
    if sys.version_info < (3, 10):
        print(f"  [ОШИБКА] Требуется Python 3.10+, текущая версия: {sys.version}")
        return False
    print(f"  [OK] Python {sys.version.split()[0]}")
    return True


def check_selenium() -> bool:
    """Проверяет, что библиотека Selenium установлена."""
    print("Проверка наличия библиотеки Selenium...")
    try:
        import selenium
        print(f"  [OK] Selenium установлен")
        return True
    except ImportError:
        print("  [ОШИБКА] Selenium не установлен.")
        print("  Выполните: pip install -r requirements.txt")
        return False


def find_browser_binary() -> str:
    """Ищет исполняемый файл браузера."""
    paths = ["/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/yandex-browser"]
    for p in paths:
        if os.path.isfile(p):
            return p
    return ""


def check_browser() -> bool:
    """Проверяет наличие браузера."""
    print("Проверка наличия браузера...")
    binary = find_browser_binary()
    if not binary:
        print("  [ОШИБКА] Не найден ни Chromium, ни Google Chrome, ни Яндекс.Браузер.")
        print("  Установите браузер или укажите путь в config.ini (browser_binary).")
        return False

    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
        version_str = result.stdout.strip()
        print(f"  [OK] Найден браузер: {binary} ({version_str})")
        
        # Извлекаем мажорную версию
        match = re.search(r"(\d+)\.", version_str)
        if match:
            print(f"  [INFO] Мажорная версия браузера: {match.group(1)}")
        return True
    except Exception as e:
        print(f"  [ОШИБКА] Не удалось запустить браузер {binary}: {e}")
        return False


def find_driver() -> str:
    """Ищет драйвер в папке скрипта или системе."""
    for name in ("chromedriver", "yandexdriver"):
        candidate = SCRIPT_DIR / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    # Проверка в PATH
    from shutil import which
    for name in ("chromedriver", "yandexdriver"):
        path = which(name)
        if path:
            return path
    return ""


def check_driver() -> bool:
    """Проверяет наличие драйвера и его версию."""
    print("Проверка наличия драйвера...")
    driver = find_driver()
    if not driver:
        print("  [ОШИБКА] Драйвер (chromedriver или yandexdriver) не найден.")
        print("  Скачайте драйвер, соответствующий версии браузера, и положите в папку скрипта.")
        return False

    try:
        result = subprocess.run([driver, "--version"], capture_output=True, text=True, timeout=5)
        version_str = result.stdout.strip()
        print(f"  [OK] Найден драйвер: {driver}")
        print(f"  [INFO] Версия драйвера: {version_str}")
        return True
    except Exception as e:
        print(f"  [ОШИБКА] Не удалось запустить драйвер {driver}: {e}")
        return False


def main() -> int:
    print("=== Проверка окружения redmine_commenter ===\n")
    
    checks = [
        check_python_version,
        check_selenium,
        check_browser,
        check_driver,
    ]
    
    success = True
    for check in checks:
        if not check():
            success = False
        print()

    if success:
        print("=== ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ ===")
        print("Внимание: убедитесь, что мажорные версии браузера и драйвера совпадают!")
        return 0
    else:
        print("=== ОБНАРУЖЕНЫ ОШИБКИ. Исправьте их перед запуском. ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())