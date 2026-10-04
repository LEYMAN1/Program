# -*- coding: utf-8 -*-
"""
pdf_engine.py — растрирование страниц PDF в QImage через PyMuPDF (fitz).

КЛЮЧЕВЫЕ МОМЕНТЫ (память и потоки):
1. fitz.Pixmap живёт в C++ памяти PyMuPDF, НЕ в куче Python. Поэтому его нужно
   явно освобождать (del), иначе при листании сотен страниц вырастет RSS.
2. Pixmap может быть CMYK / с альфа-каналом / 16 бит — для QImage гарантированно
   нужен 8-битный RGB. Нормализация: `fitz.Pixmap(fitz.csRGB, pix)` конвертирует
   любое цветовое пространство в RGB; alpha отбрасывается (страницы PDF непрозрачны).
3. pix.samples — memoryview на C++ буфер fitz. QImage(data,...) НЕ владеет памятью,
   поэтому сначала делаем КОПИЮ в кучу Python: bytes(data), и сразу .copy() —
   QImage становится полностью независимым объектом Qt, безопасным для передачи
   между потоками (QImage — implicitly shared, atomic refcount), а C++ буфер fitz
   можно освобождать немедленно.
4. Кэшируем отрендеренные страницы (LRU на N штук), чтобы повторный показ страницы
   был мгновенным, но память не росла бесконечно.
"""

from __future__ import annotations

import os
from collections import OrderedDict

import fitz  # PyMuPDF
from PyQt6.QtGui import QImage


class PdfEngine:
    """Загружает PDF и отдаёт страницы как готовые к рисованию QImage."""

    def __init__(self, max_cache_pages: int = 12):
        self._doc: fitz.Document | None = None      # открытый документ
        self._path: str | None = None               # путь к файлу (для перезагрузки)
        self._page_count: int = 0
        self._cache: OrderedDict[int, QImage] = OrderedDict()  # LRU-кэш страниц
        self._max_cache = max(2, max_cache_pages)

    # ------------------------------------------------------------------ public

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def is_loaded(self) -> bool:
        return self._doc is not None and self._page_count > 0

    def load(self, path: str) -> int:
        """Открыть PDF. Возвращает количество страниц (0 при ошибке)."""
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        self.close()                                   # закрываем предыдущий документ
        doc = fitz.open(path)                          # ленивое открытие, читается оглавление
        if doc.page_count == 0:
            doc.close()
            raise ValueError("PDF не содержит страниц")
        self._doc = doc
        self._path = path
        self._page_count = doc.page_count
        return self._page_count

    def close(self) -> None:
        """Явно освободить ресурсы fitz и кэш. Вызывать при закрытии приложения."""
        if self._doc is not None:
            try:
                self._doc.close()                      # закрывает файловый дескриптор PDF
            except Exception:
                pass
        self._doc = None
        self._path = None
        self._page_count = 0
        self._cache.clear()

    def render_page(self, index: int, target_w: int, target_h: int) -> QImage | None:
        """
        Отрендерить страницу `index` в QImage размером ~target_w x target_h
        С СОХРАНЕНИЕМ ПРОПОРЦИЙ (letterbox: пустые поля по краям, фон чёрный).

        Рендер делается сразу в целевой размер через матрицу scale — это быстрее,
        чем рендер в 300 dpi и последующее масштабирование в QPainter.
        """
        if self._doc is None or not (0 <= index < self._page_count):
            return None

        key = (index, target_w, target_h)
        img = self._cache.get(key)
        if img is not None:
            self._cache.move_to_end(key)               # LRU: тронули — поднимает в конец
            return img

        page = self._doc.load_page(index)              # страница грузится лениво
        rect = page.rect                               # размер страницы в пунктах (72 dpi)
        if rect.width <= 0 or rect.height <= 0:
            return None

        # Масштаб, вписывающий страницу в целевой прямоугольник целиком (aspect-fit)
        scale = min(target_w / rect.width, target_h / rect.height)
        mat = fitz.Matrix(scale, scale)                # матрица аффинного преобразования

        # get_pixmap всегда рендерит со сглаживанием; dpi-масштаб уже зашит в mat
        pix = page.get_pixmap(matrix=mat, alpha=False)

        # --- нормализация цветового пространства под QImage -------------------
        if pix.colorspace and pix.colorspace.n != 3:   # CMYK/Gray/прочее -> RGB
            rgb = fitz.Pixmap(fitz.csRGB, pix)
            del pix                                    # старый pixmap больше не нужен
            pix = rgb
        if pix.alpha:                                  # QImage Format_RGB888 без альфы
            noalpha = fitz.Pixmap(pix, 0)              # 0 = убрать альфа-канал
            del pix
            pix = noalpha

        # --- перенос пиксельных данных в QImage --------------------------------
        w, h, stride = pix.width, pix.height, pix.stride
        data = pix.samples                             # memoryview на буфер fitz
        # bytes(data) — КОПИЯ в кучу Python. После этого QImage живёт полностью
        # автономно, и pix можно освобождать без висячих указателей.
        image = QImage(bytes(data), w, h, stride, QImage.Format.Format_RGB888).copy()
        del pix                                        # немедленно вернуть C++ память fitz

        # Вставляем в кэш с соблюдением лимита
        self._cache[key] = image
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)            # выталкиваем давно неиспользуемую
        return image
