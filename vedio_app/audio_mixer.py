# -*- coding: utf-8 -*-
"""
audio_mixer.py — многодорожечный аудиомикшер в реальном времени на sounddevice + numpy.

АРХИТЕКТУРА:
- Один общий OutputStream (callback-поток аудио драйвера, приоритет реального времени).
- НЕСКОЛЬКО логических треков (деки) микшируются внутри ОДНОГО колбэка:
  out[:n] += track_buffer[:n] * gain   — простое аддитивное сложение numpy-массивов.
- Почему один поток, а не N потоков: звуковой драйвер (WASAPI/CoreAudio/ALSA)
  вызывается квантами по ~2-10 мс; держать один буфер и складывать в него треки
  векторизованными операциями numpy — самый дешёвый способ без щелчков.

ВАЖНО ПРО ПОТОКИ И ПАМЯТЬ (критично для audio callback):
1. В колбэке НЕЛЬЗЯ: создавать файлы, аллоцировать большие массивы, использовать
   print/logging, брать блокировки Python, которые могут «заснуть». Колбэк обязан
   вернуть управление за время меньше блока, иначе драйвер вставит glitch (XRUN).
2. Поэтому: все декодированные треки — это заранее загруженные numpy-массивы
   float32 (обычный C-order, моно/стерео приведены к стерео при загрузке).
3. Громкость меняется из GUI-потока. Чтение/запись одного float в атрибуте объекта
   атомарны благодаря GIL, поэтому «грязные» lock'и не нужны. Для индексов позиций
   используем простые int — они тоже читаются/пишутся атомарно под GIL.
4. stop()/play() только выставляют флаги — никаких join() внутри колбэка.
5. Выход клиппуется np.clip(-1, 1), чтобы сумма нескольких громких треков
   не давала переполнения (16/24-бит ЦАП схлопнул бы сигнал в хрип).
"""

from __future__ import annotations

import os
import threading
import wave

import numpy as np

# ВАЖНО: PortAudio (нативная библиотека, которую грузит sounddevice) может отсутствовать —
# на CI, сервере или машине без звукового драйвера. Импортируем устойчиво: приложение
# должно запускаться и работать с видео даже без звука (звук просто отключается).
try:
    import sounddevice as sd
    SD_AVAILABLE = True
except (OSError, ImportError):
    sd = None
    SD_AVAILABLE = False


class Deck:
    """Один трек: PCM-буфер float32 [N, 2], позиция воспроизведения, флаги."""

    def __init__(self, name: str = ""):
        self.name = name
        self.data: np.ndarray | None = None     # [samples, 2] float32, -1..1
        self.pos: int = 0                       # текущая позиция в сэмплах
        self.playing: bool = False              # флаг управляется из GUI, читается из колбэка
        self.loop: bool = True                  # зациклить (для фоновой музыки)
        self.gain: float = 0.8                  # 0..1
        self.sr: int = 44100                    # частота диска трека (при загрузке ресемплим)
        self.new_pos: int = 0                   # рабочая копия позиции для колбэка
                                                # (см. комментарий о гонке в load_track/_callback)

    @property
    def frames(self) -> int:
        return 0 if self.data is None else len(self.data)

    def unload(self) -> None:
        self.data = None
        self.pos = 0
        self.playing = False


class AudioMixer:
    """
    Микшер минимум из 2 дек (BGM + SFX), расширяемый до N.
    Использует один sd.OutputStream с callback-режимом.
    """

    def __init__(self, device_channels: int = 2, blocksize: int = 512):
        self._sr = 44100                        # внутренний формат всего графа
        self._channels = max(1, min(2, device_channels))
        self._blocksize = blocksize             # 512 сэмплов ≈ 11.6 мс @44.1k — комфорт для CPU
        self.decks: list[Deck] = [
            Deck("BGM / музыка"),               # дека 0 — фоновая музыка (loop)
            Deck("SFX / джингл"),               # дека 1 — эффекты, играются один раз
        ]
        self.decks[0].loop = True
        self.decks[1].loop = False
        self.master_gain: float = 1.0           # общая громкость
        self._stream: sd.OutputStream | None = None
        self._lock = threading.Lock()           # ТОЛЬКО для открытия/закрытия потока,
                                                # внутри колбэка лок НЕ берётся
        self._running = False

    # ------------------------------------------------------------- утилиты загрузки

    def _load_wav(self, path: str) -> np.ndarray:
        """
        Прочитать WAV (PCM 8/16/24/32 или IEEE-float) -> float32 [N, 2] на частоте self._sr.
        Читаем через stdlib `wave` + np.frombuffer — без ffmpeg и тяжёлых зависимостей.
        Ресемплинг — линейной интерполяцией (быстро и достаточно для live-джинглов).
        """
        with wave.open(path, "rb") as wf:
            n_ch = wf.getnchannels()
            sw = wf.getsampwidth()
            framerate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        if sw == 1:
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sw == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sw == 3:                                   # 24-bit PCM
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            vals = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
            vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
            arr = vals.astype(np.float32) / 8388608.0
        elif sw == 4:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"Неизвестная ширина сэмпла: {sw}")

        if n_ch == 1:
            arr = np.repeat(arr[:, None], 2, axis=1)     # mono -> stereo
        elif n_ch > 2:
            arr = arr.reshape(-1, n_ch)[:, :2]
        else:
            arr = arr.reshape(-1, 2)
        arr = np.ascontiguousarray(arr, dtype=np.float32)

        # Линейный ресемплинг к общей частоте графа (чтобы сложение буферов было побайтовым)
        if framerate != self._sr:
            dst_n = int(round(len(arr) * self._sr / framerate))
            x_old = np.linspace(0.0, 1.0, num=len(arr), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=dst_n, endpoint=False)
            res = np.empty((dst_n, 2), dtype=np.float32)
            for c in range(2):
                res[:, c] = np.interp(x_new, x_old, arr[:, c]).astype(np.float32)
            arr = res
        return arr

    def load_track(self, deck_index: int, path: str) -> None:
        """Загрузить WAV в дек. Вызывать из GUI-потока (файловое IO в колбэк не лезет!)."""
        if not (0 <= deck_index < len(self.decks)):
            raise IndexError(deck_index)
        ext = os.path.splitext(path)[1].lower()
        if ext != ".wav":
            raise ValueError("Поддерживаются только .wav (PCM). Конвертируйте файл: ffmpeg -i in.mp3 out.wav")
        data = self._load_wav(path)                      # вся работа с памятью — здесь, ДО старта
        deck = self.decks[deck_index]
        # ВАЖНО (гонка данных): RT-поток колбэка может читать deck.data/deck.pos прямо
        # сейчас. Поэтому обновляем поля в ПРАВИЛЬНОМ порядке: сначала new_pos (он ещё
        # не актуален для старого data), затем data, затем pos. В любой момент времени
        # колбэк видит либо полностью старое согласованное состояние, либо новое.
        deck.new_pos = 0
        deck.data = data                                 # замена ссылки атомарна под GIL
        deck.pos = 0
        deck.playing = False

    # ------------------------------------------------------------------ управление

    def play(self, deck_index: int, from_start: bool = True) -> None:
        d = self.decks[deck_index]
        if d.data is None:
            return
        if from_start:
            d.pos = 0
            d.new_pos = 0
        d.playing = True

    def pause(self, deck_index: int) -> None:
        self.decks[deck_index].playing = False

    def stop(self, deck_index: int) -> None:
        d = self.decks[deck_index]
        d.playing = False
        d.pos = 0
        d.new_pos = 0

    def set_gain(self, deck_index: int, value: float) -> None:
        self.decks[deck_index].gain = max(0.0, min(1.0, float(value)))

    def set_master(self, value: float) -> None:
        self.master_gain = max(0.0, min(1.0, float(value)))

    def trigger_sfx(self, path: str | None = None) -> None:
        """Быстрый джигл: если путь дан — грузим в SFX-деку, затем играем с нуля."""
        if path:
            self.load_track(1, path)
        self.play(1, from_start=True)

    # --------------------------------------------------------------- жизненный цикл

    def start(self) -> bool:
        """Открыть выходной стрим. Возвращает False, если аудио-устройства недоступны."""
        with self._lock:
            if self._stream is not None:
                return True
            if not SD_AVAILABLE:                 # нет PortAudio — работаем без звука
                return False
            try:
                # проверяем наличие хотя бы одного output-устройства
                if sd.query_devices(kind="output") is None:
                    return False
            except Exception:
                return False
            try:
                self._stream = sd.OutputStream(
                    samplerate=self._sr,
                    channels=self._channels,
                    dtype="float32",
                    blocksize=self._blocksize,
                    callback=self._callback,      # вызывается из RT-потока драйвера
                    finished_callback=lambda: None,
                )
                self._stream.start()
                self._running = True
                return True
            except Exception:
                self._stream = None
                return False

    def stop_stream(self) -> None:
        with self._lock:
            self._running = False
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    def shutdown(self) -> None:
        """Вызывать при выходе из приложения: глушим деки и закрываем поток."""
        for d in self.decks:
            d.playing = False
        self.stop_stream()

    # --------------------------------------------------------------------- колбэк

    def _callback(self, outdata: np.ndarray, frames: int,
                  time_info, status) -> None:
        """
        Пулбэк sounddevice: заполняем outdata (буфер от драйвера, shape=(frames, ch)).
        ЗДЕСЬ ЗАПРЕЩЕНЫ: аллокации, IO, локи, исключения. Всё уже готово в RAM.
        """
        if status:
            # XRUN и т.п. просто игнорируем — печатать в RT-потоке нельзя
            pass
        # Обнуляем выход (inplace, без создания нового массива)
        outdata[:] = 0.0                             # заполнение inplace, без аллокаций
        master = self.master_gain
        for deck in self.decks:
            # Локальные копии полей — защита от «рваных» чтений: GUI может в любую
            # миллисекунду менять data/pos/gain. Все вычисления ведём по копиям,
            # наружу пишем только финальный new_pos.
            data = deck.data
            if data is None or not deck.playing:
                continue
            gain = deck.gain
            loop = deck.loop
            pos = deck.new_pos
            total = len(data)
            if pos >= total:
                if loop:
                    pos = 0                          # зацикливание фона
                else:
                    deck.playing = False             # джингл сыгран — стоп
                    continue
            take = min(frames, total - pos)          # сколько сэмплов реально доступно
            ch = outdata.shape[1]
            # Аддитивное смешивание среза трека в выходной буфер (inplace +=).
            # Если GUI успел подменить трек (total изменился) — берём безопасный срез.
            outdata[:take, :ch] += data[pos:pos + take, :ch] * (gain * master)
            new_pos = pos + take
            if new_pos >= total:
                if loop:
                    new_pos = 0                      # хвост блока после цикла теряется (~11мс) — не слышно
                else:
                    deck.playing = False             # конец одиночного трека
            deck.new_pos = new_pos
            deck.pos = new_pos                       # позиция для индикатора прогресса
        # Защита от клиппинга суммы дорожек
        np.clip(outdata, -1.0, 1.0, out=outdata)     # inplace, без аллокаций

    # -------------------------------------------------------------- статус для GUI

    def deck_status(self, deck_index: int) -> tuple[bool, float]:
        """(играет_ли, прогресс 0..1) — опрашивается QTimer'ом превью-цикла."""
        d = self.decks[deck_index]
        if d.data is None or d.frames == 0:
            return False, 0.0
        return d.playing, min(1.0, d.pos / d.frames)
