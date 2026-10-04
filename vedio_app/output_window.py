# -*- coding: utf-8 -*-
"""
output_window.py — окно External Out (проектор / LED-экран) + поиск второго монитора.

КЛЮЧЕВЫЕ МОМЕНТЫ:
1. Мониторы в Qt: QApplication.screens() возвращает список QScreen в «логическом»
   порядке ОС. primaryScreen() — основной. Второй (index != primary) — целевой.
2. Окно привязывается к экрану через setGeometry(screen.geometry()) и показ
   именно в этой геометрии; на Windows этого достаточно, чтобы окно «прилипло»
   к нужному монитору (Qt перемещает окно между экранами по координатам).
3. Fullscreen без рамок: FramelessWindowHint + showFullScreen(). Курсор скрывается
   setBlankCursor(), окно поверх всех — WindowStaysOnTopHint.
4. ВАЖНО про QPixmap: он создаётся только в GUI-потоке. Кадр приходит QImage'ом
   из потока композитора, здесь делаем QPixmap.fromImage() и рисуем в paintEvent.
5. Если второго монитора нет — окно открывается на основном экране (режим теста),
   о чём сигнализирует текст в заголовке и метка в GUI.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QRect, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPixmap


def find_output_screen(prefer_secondary: bool = True):
    """
    Возвращает (QScreen, is_secondary):
      - при prefer_secondary и наличии >1 экрана — НЕосновной экран;
      - иначе — основной экран (тестовый режим).
    """
    app = QApplication.instance()
    screens = app.screens() if app else []
    primary = app.primaryScreen() if app else None
    if not screens:
        return None, False
    if prefer_secondary and len(screens) > 1:
        for s in screens:
            if s is not primary:
                # Берём первый неосновной. При 3+ экранах можно добавить выбор в GUI.
                return s, True
    return primary, False


class ExternalOutWindow(QWidget):
    """Безрамочное полноэкранное окно вывода последнего готового кадра."""

    closed = pyqtSignal()   # сообщаем контроллеру, что эфир закрыли (например, по Esc)

    def __init__(self, title: str = "External Out"):
        super().__init__()
        self.setWindowTitle(title)
        # Рамки нет, окно всегда поверх всех, игнорируем Alt+F4-подобные попытки закрыть
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setCursor(Qt.CursorShape.BlankCursor)   # курсор не виден на проекторе
        self.setStyleSheet("background: black;")     # фон до первого кадра
        self._pixmap: QPixmap | None = None
        self.screen_name: str = ""                   # куда выведены (для статуса)
        self.is_secondary: bool = False

    # --------------------------------------------------------------- вывод кадра

    def set_frame(self, image) -> None:
        """Принимает QImage из потока композитора, конвертирует в QPixmap (GUI-поток)."""
        # fromImage делает копию с преобразованием формата — безопасно держать ссылку
        self._pixmap = QPixmap.fromImage(image)
        self.update()                                # планируем paintEvent (не блокируясь)

    def paintEvent(self, event) -> None:
        """Рисуем кадр во весь клиентский прямоугольник (он = разрешению экрана)."""
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor(0, 0, 0))               # штора вокруг 4:3 слайдов
        if self._pixmap is not None and not self._pixmap.isNull():
            # KeepAspectRatio: композитор уже отдаёт кадр под размер экрана,
            # но на случай ресайза/HiDPI сохраняем пропорции.
            target = self._pixmap.scaled(
                r.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (r.width() - target.width()) // 2
            y = (r.height() - target.height()) // 2
            p.drawPixmap(x, y, target)
        p.end()

    # ------------------------------------------------------- показать на экране

    def show_on_screen(self, screen) -> None:
        """
        Разместить окно на всём экране и угнать в fullscreen.
        screen.geometry() — координаты экрана в виртуальном десктопе ОС
        (у второго монитора они могут быть, например, (1920,0) или (-1920,0)).
        Qt сам перенесёт окно на нужный физический монитор по этим координатам.
        """
        if screen is None:
            return
        geo: QRect = screen.geometry()
        # Явно ставим геометрию ДО показа: окно «переезжает» на нужный монитор
        self.setGeometry(geo)
        self.showFullScreen()                        # real fullscreen без рамок
        # Экстренный вариант для некоторых WM/Linux: raise + activate
        self.raise_()
        self.activateWindow()
        self.screen_name = screen.name()

    def keyPressEvent(self, event) -> None:
        """Esc — быстрый выход из эфира (защита от «залетевшего» фуллскрина)."""
        if event.key() == Qt.Key.Key_Escape:
            self.close()                          
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._pixmap = None                        # освобождаем QPixmap сразу
        self.closed.emit()                         # уведомляем контроллер
        super().closeEvent(event)
