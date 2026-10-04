# -*- coding: utf-8 -*-
"""
main.py — точка входа MiniMix Live (легковесный аналог vMix для PDF + оверлеи + аудио).

Запуск:  python main.py [путь.pdf]
Флаг QT_QPA_PLATFORM=offscreen используется только для автотестов в CI.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Импорт PyQt6 здесь, а не на уровне модуля, чтобы понятные ошибки были,
    # если библиотека не установлена.
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("Не найден PyQt6. Установите зависимости:\n"
              "  pip install -r requirements.txt")
        return 1

    from main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MiniMix Live")

    win = MainWindow()
    win.show()

    # Опционально: PDF-файл первым аргументом командной строки
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        try:
            count = win.pdf.load(os.path.abspath(sys.argv[1]))
            win.spin_page.setMaximum(count)
            win.compositor.goto_page(0)
            win.statusBar().showMessage(
                f"Загружено: {os.path.basename(sys.argv[1])} ({count} стр.)")
        except Exception as e:
            print(f"Не удалось открыть PDF: {e}")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
