# -*- coding: utf-8 -*-
"""
main_window.py — окно оператора (Controller / Preview).

Слева — превью того, что идёт в эфир (QLabel с QPixmap), справа — панель управления:
  PDF: открыть, страницы ◀ ▶, поле «перейти к странице»;
  ОВЕРЛЕИ: текст сверху, большое число ± , таймер старт/стоп/сброс, часы,
          lower third (заголовок/подпись, показать/скрыть с анимацией);
  АУДИО: деки BGM/SFX — загрузка wav, play/pause/stop, громкость, master;
  ВЫВОД: кнопка «В ЭФИР» (External Out), выбор FPS.

Потоки: GUI ничего тяжёлого не делает. Кадры приходят сигналом Compositor.frameReady
в виде QImage, здесь конвертируются в QPixmap и ужимаются под размер превью.
"""

from __future__ import annotations

import os
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox,
                             QFileDialog, QGroupBox, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QMessageBox, QPushButton, QSlider, QSpinBox,
                             QVBoxLayout, QWidget)

from audio_mixer import AudioMixer
from compositor import Compositor, OverlayModel
from output_window import ExternalOutWindow, find_output_screen
from pdf_engine import PdfEngine


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MiniMix Live — оператор")
        self.resize(1280, 760)

        # --- ядра приложения -------------------------------------------------
        self.pdf = PdfEngine()                       # растрирование PDF (fitz)
        self.model = OverlayModel()                  # общие динамические данные
        self.mixer = AudioMixer()                    # sounddevice-микшер
        self.compositor = Compositor(self.pdf, self.model)   # поток рендера 30/60 FPS
        self.out_window: ExternalOutWindow | None = None     # External Out

        # кадры композитора -> и превью, и выходное окно
        self.compositor.frameReady.connect(self._on_frame)
        self.compositor.fpsUpdated.connect(
            lambda v: self.lbl_fps.setText(f"FPS: {v:4.1f}"))

        self._build_ui()

        # таймер обновления статусов аудио (не связан с рендером, 10 Гц достаточно)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_audio_status)
        self._status_timer.start(100)

        self.compositor.start()                      # запускаем поток рендера
        if not self.mixer.start():
            self.statusBar().showMessage(
                "Аудио-устройство недоступно — звук отключён (видео работает)", 8000)

    # ================================================================ UI

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)

        # ---------------- левая колонка: превью ------------------------------
        left = QVBoxLayout()
        self.preview = QLabel("Превью появится после загрузки PDF")
        self.preview.setMinimumSize(640, 360)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(
            "background:#101014; color:#8a8f98; border:2px solid #2b2f36;"
            "border-radius:6px; font-size:16px;")
        left.addWidget(self.preview, stretch=1)

        bar = QHBoxLayout()
        self.lbl_page = QLabel("Страница: — / —")
        self.lbl_fps = QLabel("FPS:  0.0")
        self.lbl_out = QLabel("Эфир: выключен")
        bar.addWidget(self.lbl_page)
        bar.addStretch(1)
        bar.addWidget(self.lbl_fps)
        bar.addSpacing(16)
        bar.addWidget(self.lbl_out)
        left.addLayout(bar)
        root.addLayout(left, stretch=3)

        # ---------------- правая колонка: панели управления ------------------
        right = QVBoxLayout()
        right.addWidget(self._build_pdf_group())
        right.addWidget(self._build_overlay_group())
        right.addWidget(self._build_lower_third_group())
        right.addWidget(self._build_audio_group())
        right.addWidget(self._build_output_group())
        right.addStretch(1)
        root.addLayout(right, stretch=2)

        self.setCentralWidget(central)

        # меню
        m_file = self.menuBar().addMenu("&Файл")
        act_open = QAction("Открыть PDF…", self, shortcut="Ctrl+O")
        act_open.triggered.connect(self.open_pdf)
        act_quit = QAction("Выход", self, shortcut="Ctrl+Q")
        act_quit.triggered.connect(self.close)
        m_file.addAction(act_open)
        m_file.addSeparator()
        m_file.addAction(act_quit)

        # горячие клавиши листания
        self._sc_prev = QAction(self, shortcut="Right", triggered=lambda: self.step_page(1))
        self._sc_next = QAction(self, shortcut="Left", triggered=lambda: self.step_page(-1))
        self._sc_go = QAction(self, shortcut="Space",
                              triggered=lambda: self.toggle_out())
        for a in (self._sc_prev, self._sc_next, self._sc_go):
            self.addAction(a)

    # ---------------------------------------------------------------- PDF

    def _build_pdf_group(self) -> QGroupBox:
        g = QGroupBox("PDF-презентация")
        lay = QHBoxLayout(g)
        btn_open = QPushButton("Открыть…")
        btn_open.clicked.connect(self.open_pdf)
        b_prev = QPushButton("◀")
        b_prev.clicked.connect(lambda: self.step_page(-1))
        b_next = QPushButton("▶")
        b_next.clicked.connect(lambda: self.step_page(1))
        self.spin_page = QSpinBox()
        self.spin_page.setMinimum(1)
        self.spin_page.valueChanged.connect(
            lambda v: self.compositor.goto_page(v - 1))
        self.btn_goto = QPushButton("Перейти")
        self.btn_goto.clicked.connect(
            lambda: self.compositor.goto_page(self.spin_page.value() - 1))
        for wdg in (btn_open, b_prev, b_next, self.spin_page, self.btn_goto):
            lay.addWidget(wdg)
        return g

    # ------------------------------------------------------------ оверлеи

    def _build_overlay_group(self) -> QGroupBox:
        g = QGroupBox("Динамический текст и цифры")
        outer = QVBoxLayout(g)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Текст сверху:"))
        self.ed_main = QLineEdit()
        self.ed_main.setPlaceholderText("Например: ФИНАЛ — раунд 3")
        self.ed_main.textChanged.connect(lambda t: setattr(self.model, "main_text", t))
        row1.addWidget(self.ed_main)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Большое число:"))
        self.lbl_number = QLabel("0")
        self.lbl_number.setMinimumWidth(90)
        self.lbl_number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_number.setStyleSheet(
            "font-size:26px; font-weight:bold; color:#e63946;"
            "background:#15161a; border-radius:6px; padding:2px 8px;")
        b_dec = QPushButton("−")
        b_dec.setFixedWidth(40)
        b_dec.clicked.connect(lambda: self._bump_number(-1))
        b_inc = QPushButton("+")
        b_inc.setFixedWidth(40)
        b_inc.clicked.connect(lambda: self._bump_number(+1))
        b_set = QPushButton("Задать…")
        b_set.clicked.connect(self._set_number_dialog)
        b_zero = QPushButton("Сброс")
        b_zero.clicked.connect(lambda: self._apply_number("0"))
        self.chk_num = QCheckBox("Показывать")
        self.chk_num.setChecked(True)
        self.chk_num.toggled.connect(lambda v: setattr(self.model, "number_visible", v))
        for wdg in (self.lbl_number, b_dec, b_inc, b_set, b_zero, self.chk_num):
            row2.addWidget(wdg)
        row2.addStretch(1)
        outer.addLayout(row2)

        row3 = QHBoxLayout()
        self.chk_clock = QCheckBox("Часы в эфире")
        self.chk_clock.toggled.connect(lambda v: setattr(self.model, "show_clock", v))
        self.chk_timer = QCheckBox("Таймер")
        self.chk_timer.toggled.connect(lambda v: setattr(self.model, "show_timer", v))
        self.btn_timer = QPushButton("Старт")
        self.btn_timer.setCheckable(True)
        self.btn_timer.clicked.connect(self._toggle_timer)
        self.btn_timer_reset = QPushButton("Сброс")
        self.btn_timer_reset.clicked.connect(self._reset_timer)
        for wdg in (self.chk_clock, self.chk_timer, self.btn_timer,
                    self.btn_timer_reset):
            row3.addWidget(wdg)
        row3.addStretch(1)
        outer.addLayout(row3)

        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Акцент:"))
        self.btn_color = QPushButton()
        self.btn_color.setFixedSize(48, 24)
        self.btn_color.setStyleSheet(f"background:{self.model.accent_color};")
        self.btn_color.clicked.connect(self._pick_color)
        row4.addWidget(self.btn_color)
        row4.addWidget(QLabel("FPS:"))
        self.cmb_fps = QComboBox()
        self.cmb_fps.addItems(["30", "60"])
        self.cmb_fps.setCurrentText("60")
        self.cmb_fps.currentTextChanged.connect(
            lambda t: self.compositor.set_fps(int(t)))
        row4.addWidget(self.cmb_fps)
        row4.addStretch(1)
        outer.addLayout(row4)
        return g

    # -------------------------------------------------------- lower third

    def _build_lower_third_group(self) -> QGroupBox:
        g = QGroupBox("Нижняя плашка (Lower Third)")
        outer = QVBoxLayout(g)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Заголовок:"))
        self.ed_lt_title = QLineEdit()
        self.ed_lt_title.textChanged.connect(
            lambda t: setattr(self.model, "lt_title", t))
        r1.addWidget(self.ed_lt_title)
        outer.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Подпись:"))
        self.ed_lt_sub = QLineEdit()
        self.ed_lt_sub.textChanged.connect(
            lambda t: setattr(self.model, "lt_subtitle", t))
        r2.addWidget(self.ed_lt_sub)
        outer.addLayout(r2)
        r3 = QHBoxLayout()
        self.btn_lt_show = QPushButton("Показать плашку")
        self.btn_lt_show.setCheckable(True)
        self.btn_lt_show.clicked.connect(self._toggle_lt)
        r3.addWidget(self.btn_lt_show)
        r3.addStretch(1)
        outer.addLayout(r3)

        # Анимация выезда — через QTimer (проще и без кастомных Qt-свойств):
        # каждые ~33 мс двигаем model.lt_progress к целевому значению.
        self._lt_target = 1.0
        self._lt_timer = QTimer(self)
        self._lt_timer.timeout.connect(self._animate_lt)
        self._lt_timer.start(33)
        return g

    # ------------------------------------------------------------- аудио

    def _build_audio_group(self) -> QGroupBox:
        g = QGroupBox("Аудио микшер (sounddevice)")
        outer = QVBoxLayout(g)
        self.deck_rows = []
        for i, caption in enumerate(("BGM", "SFX")):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{caption}:"))
            lbl_file = QLabel("— не загружено —")
            lbl_file.setMinimumWidth(140)
            btn_load = QPushButton("WAV…")
            btn_load.clicked.connect(lambda checked=False, idx=i: self._load_deck(idx))
            btn_play = QPushButton("▶")
            btn_play.setFixedWidth(36)
            btn_play.clicked.connect(
                lambda checked=False, idx=i: self.mixer.play(idx))
            btn_stop = QPushButton("■")
            btn_stop.setFixedWidth(36)
            btn_stop.clicked.connect(
                lambda checked=False, idx=i: self.mixer.stop(idx))
            sld = QSlider(Qt.Orientation.Horizontal)
            sld.setRange(0, 100)
            sld.setValue(80)
            sld.valueChanged.connect(
                lambda v, idx=i: self.mixer.set_gain(idx, v / 100.0))
            loop_chk = QCheckBox("Loop")
            loop_chk.setChecked(i == 0)
            loop_chk.toggled.connect(
                lambda v, idx=i: setattr(self.mixer.decks[idx], "loop", v))
            for wdg in (lbl_file, btn_load, btn_play, btn_stop, sld, loop_chk):
                row.addWidget(wdg)
            outer.addLayout(row)
            self.deck_rows.append((lbl_file, sld))
        # master
        rowm = QHBoxLayout()
        rowm.addWidget(QLabel("Master:"))
        self.sld_master = QSlider(Qt.Orientation.Horizontal)
        self.sld_master.setRange(0, 100)
        self.sld_master.setValue(100)
        self.sld_master.valueChanged.connect(
            lambda v: self.mixer.set_master(v / 100.0))
        rowm.addWidget(self.sld_master)
        self.lbl_master = QLabel("100%")
        rowm.addWidget(self.lbl_master)
        outer.addLayout(rowm)
        return g

    # ------------------------------------------------------------ вывод

    def _build_output_group(self) -> QGroupBox:
        g = QGroupBox("Вывод (External Out)")
        lay = QHBoxLayout(g)
        self.btn_out = QPushButton("🔴 В ЭФИР (второй монитор)")
        self.btn_out.setCheckable(True)
        self.btn_out.setStyleSheet(
            "QPushButton { background:#243b2a; font-weight:bold; padding:8px; }"
            "QPushButton:checked { background:#7a1f1f; color:white; }")
        self.btn_out.toggled.connect(self.toggle_out)
        lay.addWidget(self.btn_out)
        return g

    # ================================================================ СЛОТЫ

    # ---- PDF ----

    def open_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть PDF", "",
                                              "Документы PDF (*.pdf)")
        if not path:
            return
        try:
            count = self.pdf.load(path)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка PDF", str(e))
            return
        self.spin_page.setMaximum(count)
        self.spin_page.setValue(1)
        self.compositor.goto_page(0)
        self.statusBar().showMessage(f"Загружено: {os.path.basename(path)} ({count} стр.)")

    def step_page(self, delta: int) -> None:
        if not self.pdf.is_loaded:
            return
        new = max(0, min(self.pdf.page_count - 1, self.compositor.page + delta))
        self.compositor.goto_page(new)
        self.spin_page.blockSignals(True)            # не вызывать valueChanged рекурсией
        self.spin_page.setValue(new + 1)
        self.spin_page.blockSignals(False)

    # ---- оверлеи ----

    def _apply_number(self, text: str) -> None:
        self.model.big_number = text
        self.lbl_number.setText(text)

    def _bump_number(self, delta: int) -> None:
        try:
            val = int("".join(ch for ch in self.model.big_number if ch.isdigit() or ch == "-") or "0")
        except ValueError:
            val = 0
        val = max(0, val + delta)
        self._apply_number(str(val))

    def _set_number_dialog(self) -> None:
        text, ok = QInputDialog.getText(self, "Значение числа",
                                        "Что показать в эфире:",
                                        text=self.model.big_number)
        if ok:
            self._apply_number(text.strip()[:8])

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self.model.accent_color), self,
                                  "Цвет акцента")
        if c.isValid():
            self.model.accent_color = c.name()
            self.btn_color.setStyleSheet(f"background:{c.name()};")

    def _toggle_timer(self) -> None:
        m = self.model
        if self.btn_timer.isChecked():
            if not m.timer_running:
                m.timer_started_at = time.monotonic()
                m.timer_running = True
            self.btn_timer.setText("Пауза")
        else:
            if m.timer_running:
                m.timer_accum += time.monotonic() - m.timer_started_at
                m.timer_running = False
            self.btn_timer.setText("Старт")

    def _reset_timer(self) -> None:
        m = self.model
        m.timer_running = False
        m.timer_accum = 0.0
        m.timer_started_at = time.monotonic()
        self.btn_timer.setChecked(False)
        self.btn_timer.setText("Старт")

    # ---- lower third анимация (через QTimer — дёшево и без кастомных свойств) ----

    def _toggle_lt(self) -> None:
        self.model.lt_enabled = True                 # плашка всегда «включена»…
        self._lt_target = 1.0 if self.btn_lt_show.isChecked() else 0.0

    def _animate_lt(self) -> None:
        """Каждые ~33 мс двигаем lt_progress к цели — получаем плавный выезд."""
        cur = self.model.lt_progress
        step = 0.08
        if cur < self._lt_target:
            self.model.lt_progress = min(self._lt_target, cur + step)
        elif cur > self._lt_target:
            self.model.lt_progress = max(self._lt_target, cur - step)

    # ---- аудио ----

    def _load_deck(self, idx: int) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите WAV-файл", "",
                                              "WAV аудио (*.wav)")
        if not path:
            return
        try:
            self.mixer.load_track(idx, path)         # декодирование в RAM (GUI-поток)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка аудио", str(e))
            return
        self.deck_rows[idx][0].setText(os.path.basename(path)[:28])
        if idx == 0:
            self.mixer.play(0)                       # музыку сразу запускаем

    def _refresh_audio_status(self) -> None:
        """10 Гц опрашиваем деки и показываем прогресс в статус-баре."""
        playing0, p0 = self.mixer.deck_status(0)
        playing1, _ = self.mixer.deck_status(1)
        tip = f"BGM: {f'▶ играет {p0:.0%}' if playing0 else 'стоп'} | " \
              f"SFX: {'▶ играет' if playing1 else 'готов'}"
        self.statusBar().showMessage(tip, 250)

    # ---- external out ----

    def toggle_out(self, checked: bool | None = None) -> None:
        if checked is None:
            checked = not self.btn_out.isChecked()
        if checked:
            screen, secondary = find_output_screen(prefer_secondary=True)
            if screen is None:
                QMessageBox.warning(self, "Мониторы", "Экраны не найдены.")
                self.btn_out.setChecked(False)
                return
            if self.out_window is None or not self.out_window.isValid():
                # isValid() — проверка на «висячий» C++ объект: окно могли закрыть
                # по Esc, и Qt мог его удалить (WA_DeleteOnClose не ставим, но
                # на всякий случай проверяем указатель перед переиспользованием).
                self.out_window = ExternalOutWindow()
                # если оператор закрыл эфир по Esc — синхронизируем кнопку
                self.out_window.closed.connect(
                    lambda: (self.btn_out.setChecked(False),
                             self.lbl_out.setText("Эфир: выключен"),
                             self.btn_out.setText("🔴 В ЭФИР (второй монитор)")))
            geo = screen.geometry()
            # Композитор рендерит ровно в физический размер экрана вывода
            dpr = screen.devicePixelRatio()
            self.compositor.set_output_size(int(geo.width() * dpr),
                                            int(geo.height() * dpr))
            self.out_window.show_on_screen(screen)
            where = f"второй монитор {screen.name()} {geo.width()}x{geo.height()}" \
                if secondary else "ТЕСТ: основной экран (второй монитор не найден)"
            self.lbl_out.setText(f"Эфир: ВКЛ — {where}")
            self.btn_out.setText("⏹ Снять с эфира")
        else:
            if self.out_window is not None and self.out_window.isValid():
                self.out_window.hide()
            self.lbl_out.setText("Эфир: выключен")
            self.btn_out.setText("🔴 В ЭФИР (второй монитор)")
        self.btn_out.blockSignals(True)
        self.btn_out.setChecked(checked)
        self.btn_out.blockSignals(False)

    # ---- кадры ----

    def _on_frame(self, image) -> None:
        """Приём кадра из потока композитора (queued connection → GUI-поток)."""
        self.lbl_page.setText(
            f"Страница: {self.compositor.page + 1} / {max(1, self.pdf.page_count)}")
        # Превью: уменьшаем кадр до размера метки (scaled — копия, но маленькая)
        pm = QPixmap.fromImage(image).scaled(
            self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)   # превью — без costly smooth
        self.preview.setPixmap(pm)
        # Тот же кадр — в эфирное окно (конвертация QImage->QPixmap внутри окна)
        if self.out_window is not None and self.out_window.isValid() \
                and self.out_window.isVisible():
            self.out_window.set_frame(image)

    # ---- закрытие ----

    def closeEvent(self, event) -> None:
        """Корректно гасим потоки и устройства — иначе sounddevice оставит «жужжание»."""
        self._status_timer.stop()
        self.compositor.stop()                       # ждём завершения потока рендера
        self.mixer.shutdown()                        # останавливаем аудиопоток
        self.pdf.close()                             # освобождаем fitz.Document
        if self.out_window is not None:
            self.out_window.close()
        super().closeEvent(event)
