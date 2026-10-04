# -*- coding: utf-8 -*-
"""
compositor.py — движок наложения (оверлеи) + цикл рендера 30/60 FPS.

Здесь живут:
1. OverlayModel — описание динамического контента (текст, счётчики, таймер, lower third).
   Модель отделена от рендера: GUI пишет поля модели, композитор только читает.
2. render_frame() — чистая функция композитинга: QImage страницы + модель -> готовый кадр.
3. Compositor(QThread) — рабочий поток рендера. Кадр отдаётся наружу сигналом
   frameReady(QImage); приёмники (Preview / ExternalOut) конвертируют в QPixmap
   В СВОЁМ потоке (QPixmap нельзя создавать вне GUI-потока!).

ПОТОКИ И ПАМЯТЬ:
- PDF рендерится ТОЛЬКО при смене страницы/размера (self._base), в промежуточных
  кадрах перерисовываются лишь оверлеи — это дёшево.
- QImage передаётся через queued signal механизмом implicit sharing (copy-on-write):
  фактического копирования мегабайт пикселей при emit не происходит.
- Цикл: QTimer внутри QThread с интервалом 1000/FPS; если рендер дольше интервала,
  кадры просто выходят реже — пропусков не будет, только просадка FPS.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QImage, QPainter, QPainterPath,
                         QPen, QRadialGradient)

from pdf_engine import PdfEngine


# ================================================================ МОДЕЛЬ ОВЕРЛЕЕВ

@dataclass
class OverlayModel:
    """Все динамические данные эфира. Изменяется из GUI, читается из потока рендера."""
    main_text: str = ""            # крупный текст сверху
    big_number: str = "0"          # счётчик/цифры (баллы, обратный отсчёт)
    number_visible: bool = True
    show_clock: bool = False       # часы реального времени в углу
    show_timer: bool = False       # секундомер (управляется кнопками GUI)
    timer_running: bool = False
    timer_started_at: float = 0.0  # time.monotonic() последнего старта
    timer_accum: float = 0.0       # накопленное время до последней паузы
    # ---- lower third (нижняя плашка) ----
    lt_enabled: bool = True
    lt_title: str = ""
    lt_subtitle: str = ""
    lt_progress: float = 1.0       # 0..1 — анимация выезда/уезда плашки
    # ---- оформление ----
    accent_color: str = "#e63946"
    text_font_px: int = 72         # базовый размер для main_text (масштабируется под H)
    number_font_px: int = 160      # размер цифр
    show_page_badge: bool = True   # «3 / 12» в углу


# ==================================================================== РЕНДЕР КАДРА

def _fit_font(painter: QPainter, rect: QRectF, text: str, px: int,
              bold: bool = True) -> QFont:
    """Подбирает размер шрифта так, чтобы текст влез в rect по ширине."""
    font = QFont("Arial")
    font.setBold(bold)
    font.setPixelSize(max(10, px))
    for _ in range(12):                     # максимум 12 шагов уменьшения
        painter.setFont(font)
        fm = painter.fontMetrics()
        if fm.horizontalAdvance(text) <= rect.width() or font.pixelSize() <= 12:
            break
        font.setPixelSize(int(font.pixelSize() * 0.85))
    return font


def _draw_text_with_shadow(p: QPainter, rect: QRectF, text: str, font: QFont,
                           color: QColor,
                           align=Qt.AlignmentFlag.AlignCenter) -> None:
    """Текст с мягкой тенью — читаем и на светлых, и на тёмных слайдах."""
    p.setFont(font)
    shifted = QRectF(rect.x() + 3, rect.y() + 4, rect.width(), rect.height())
    p.setPen(QColor(0, 0, 0, 140))
    p.drawText(shifted, int(align), text)
    p.setPen(color)
    p.drawText(rect, int(align), text)


def _format_mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def render_frame(base: QImage | None, model: OverlayModel,
                 page_index: int, page_count: int,
                 W: int, H: int) -> QImage:
    """
    Шаг 1+2 композитинга: берёт растр страницы и рисует поверх все оверлеи.
    Возвращает НОВЫЙ QImage размером ровно W x H, готовый к конвертации в QPixmap.
    """
    canvas = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor(8, 8, 12))                # фон letterbox — почти чёрный

    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # ---------- слой 1: страница PDF (aspect-fit по центру) ----------
    if base is not None and not base.isNull():
        sw, sh = base.width(), base.height()
        scale = min(W / sw, H / sh)
        dw, dh = int(sw * scale), int(sh * scale)
        dx, dy = (W - dw) // 2, (H - dh) // 2
        p.drawImage(QRectF(dx, dy, dw, dh), base, QRectF(0, 0, sw, sh))
    else:
        # заглушка до загрузки PDF
        f = QFont("Arial", 28)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(120, 120, 130))
        p.drawText(canvas.rect(), int(Qt.AlignmentFlag.AlignCenter),
                   "PDF не загружен\nФайл -> Открыть PDF")

    m = 40                                       # внешний отступ плашек
    u = H / 1080.0                               # «юнит»: пропорции под любое разрешение

    # ---------- слой 2: крупный текст сверху ----------
    if model.main_text.strip():
        rect = QRectF(m, m * 0.6, W - 2 * m, 140 * u)
        font = _fit_font(p, rect, model.main_text, int(model.text_font_px * u))
        _draw_text_with_shadow(p, rect, model.main_text, font, QColor("#ffffff"))

    # ---------- слой 3: большие цифры (счётчик) ----------
    if model.number_visible and model.big_number.strip():
        num = model.big_number.strip()[:8]       # защита от гигантского ввода
        rect = QRectF(W * 0.25, H * 0.30, W * 0.50, H * 0.40)
        font = _fit_font(p, rect, num, int(model.number_font_px * u))
        # радиальное свечение цвета акцента под цифрами
        grad = QRadialGradient(QPointF(rect.center()), rect.width() * 0.6)
        c = QColor(model.accent_color)
        c.setAlpha(70)
        grad.setColorAt(0.0, c)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(rect, grad)
        _draw_text_with_shadow(p, rect, num, font, QColor(model.accent_color))

    # ---------- слой 4: часы (правый верхний угол) ----------
    if model.show_clock:
        t = time.strftime("%H:%M:%S")
        font = QFont("Consolas")
        font.setPixelSize(int(34 * u))
        font.setBold(True)
        rect = QRectF(W - m - 260 * u, m * 0.6, 260 * u, 50 * u)
        p.setFont(font)
        p.setPen(QColor(0, 0, 0, 120))
        p.drawText(rect.translated(2, 2), int(Qt.AlignmentFlag.AlignRight), t)
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignRight), t)

    # ---------- слой 5: таймер (левый верхний угол) ----------
    if model.show_timer:
        if model.timer_running:
            secs = model.timer_accum + (time.monotonic() - model.timer_started_at)
        else:
            secs = model.timer_accum
        txt = _format_mmss(secs)
        font = QFont("Consolas")
        font.setPixelSize(int(40 * u))
        font.setBold(True)
        rect = QRectF(m, m * 0.6, 220 * u, 56 * u)
        path = QPainterPath()
        path.addRoundedRect(rect, 10 * u, 10 * u)
        p.fillPath(path, QColor(0, 0, 0, 130))
        pen = QPen(QColor(model.accent_color))
        pen.setWidthF(max(1.0, 2 * u))
        p.strokePath(path, pen)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), txt)

    # ---------- слой 6: lower third (выезжающая нижняя плашка) ----------
    if model.lt_enabled and (model.lt_title.strip() or model.lt_subtitle.strip()):
        pr = max(0.0, min(1.0, model.lt_progress))   # 0 — спрятана, 1 — полностью видна
        bar_h = 150 * u
        bar_w = W * 0.62
        x = m - int(bar_w * (1.0 - pr))              # анимация въезда слева направо
        y = H - bar_h - m * 1.2
        rect = QRectF(x, y, bar_w, bar_h)
        path = QPainterPath()
        path.addRoundedRect(rect, 14 * u, 14 * u)
        p.fillPath(path, QColor(10, 12, 18, 210))    # полупрозрачная подложка
        p.fillRect(QRectF(x, y, 10 * u, bar_h), QColor(model.accent_color))
        # заголовок спикера
        tr = QRectF(x + 30 * u, y + 14 * u, bar_w - 50 * u, bar_h * 0.45)
        f1 = _fit_font(p, tr, model.lt_title or " ", int(46 * u))
        _draw_text_with_shadow(p, tr, model.lt_title, f1, QColor("#ffffff"),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # подпись/должность
        sr = QRectF(x + 30 * u, y + bar_h * 0.52, bar_w - 50 * u, bar_h * 0.40)
        f2 = QFont("Arial")
        f2.setPixelSize(int(30 * u))
        _draw_text_with_shadow(p, sr, model.lt_subtitle, f2, QColor("#cfd3da"),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    # ---------- слой 7: бейдж страницы ----------
    if model.show_page_badge and page_count > 0:
        txt = f"{page_index + 1} / {page_count}"
        font = QFont("Arial")
        font.setPixelSize(int(26 * u))
        font.setBold(True)
        rect = QRectF(W - m - 170 * u, H - m - 60 * u, 170 * u, 52 * u)
        path = QPainterPath()
        path.addRoundedRect(rect, 10 * u, 10 * u)
        p.fillPath(path, QColor(0, 0, 0, 120))
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), txt)

    p.end()                                          # обязательно завершить QPainter
    return canvas


# ============================================================== ПОТОК КОМПОЗИТОРА

class Compositor(QThread):
    """
    Рабочий поток: крутит цикл 30/60 FPS, отдаёт готовые кадры сигналом frameReady.
    GUI-поток НЕ блокируется никогда — всё тяжёлое (fitz, QPainter) здесь.
    """

    frameReady = pyqtSignal(QImage)     # новый кадр (implicit-shared, копия дешёвая)
    fpsUpdated = pyqtSignal(float)      # фактический FPS — для индикатора в GUI

    def __init__(self, pdf: PdfEngine, model: OverlayModel, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        self.model = model                    # общая ссылка: GUI пишет, мы читаем (GIL)
        self._target_size = (1920, 1080)      # разрешение External Out
        self._page = 0
        self._base: QImage | None = None      # кэш растра ТЕКУЩЕЙ страницы
        self._base_key: tuple | None = None   # (page, w, h) — ключ кэша
        self._dirty_base = True               # «перерендерить страницу»
        self._fps = 60
        self._running = False
        self._frames = 0
        self._t0 = time.monotonic()

    # ------------------------------------------------------------- API из GUI

    def set_output_size(self, w: int, h: int) -> None:
        """Задаёт физическое разрешение выходного окна (вызывается из GUI)."""
        w, h = max(320, int(w)), max(240, int(h))
        if self._target_size != (w, h):
            self._target_size = (w, h)
            self._dirty_base = True           # растр в новом размере ещё не готов

    def set_fps(self, fps: int) -> None:
        """Смена FPS из GUI. QTimer с новым интервалом создаётся в run() потока,
        поэтому при смене делаем корректный restart рабочего потока."""
        new_fps = max(10, min(120, int(fps)))
        if new_fps == self._fps:
            return
        self._fps = new_fps
        if self.isRunning():
            self.stop()
            self.start()

    def goto_page(self, index: int) -> None:
        count = max(1, self.pdf.page_count)
        index = max(0, min(count - 1, int(index)))
        if self._page != index:
            self._page = index
            self._dirty_base = True

    @property
    def page(self) -> int:
        return self._page

    # ------------------------------------------------------------- тело потока

    def run(self) -> None:
        """
        Внутри QThread создаём свой QTimer: у каждого потока собственный event loop,
        и таймер должен принадлежать ЭТОМУ потоку (поэтому не в __init__).
        """
        self._running = True
        timer = QTimer()                      # без parent — живёт в этом потоке
        timer.timeout.connect(self._tick)
        timer.start(int(1000 / self._fps))
        self._t0 = time.monotonic()
        self._frames = 0
        self.exec()                           # запуск event loop'а потока

    def stop(self) -> None:
        self._running = False
        self.quit()                           # останавливает exec()
        self.wait(2000)                       # ждём завершения (макс 2 с)

    def _tick(self) -> None:
        if not self._running:
            return
        w, h = self._target_size
        # Шаг 1: базовый растр страницы — ТОЛЬКО при смене страницы/размера
        key = (self._page, w, h)
        if self._dirty_base or self._base_key != key:
            self._base = self.pdf.render_page(self._page, w, h)
            self._base_key = key
            self._dirty_base = False
        # Шаг 2: композитинг оверлеев поверх базового растра
        frame = render_frame(self._base, self.model, self._page,
                             self.pdf.page_count, w, h)
        # Шаг 3: рассылаем кадр подписчикам (Preview + ExternalOut)
        self.frameReady.emit(frame)
        # учёт FPS раз в секунду
        self._frames += 1
        now = time.monotonic()
        if now - self._t0 >= 1.0:
            self.fpsUpdated.emit(self._frames / (now - self._t0))
            self._frames = 0
            self._t0 = now
