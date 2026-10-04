# -*- coding: utf-8 -*-
"""
make_demo_assets.py — генерирует демо-контент для быстрой проверки приложения:
  demo/presentation.pdf — 6 слайдов (рисуем сами через fitz, чтобы не искать PDF);
  demo/bgm.wav          — фоновая петля (мягкие аккорды, 12 с);
  demo/sfx_ding.wav     — джингл «динь» (2 с).

Запуск: python make_demo_assets.py
"""

import math
import os
import struct
import wave

import fitz  # PyMuPDF — умеет и читать, и СОЗДАВАТЬ PDF


def make_pdf(path: str, pages: int = 6) -> None:
    doc = fitz.open()
    W, H = 960, 540                                  # 16:9 в пунктах
    titles = ["МИНИМИКС LIVE", "О ПРОЕКТЕ", "ПРЕЗЕНТАЦИЯ",
              "РАУНД 1", "РАУНД 2", "ФИНАЛ"]
    colors = [(0.09, 0.13, 0.25), (0.16, 0.09, 0.25), (0.09, 0.22, 0.16),
              (0.28, 0.12, 0.10), (0.10, 0.18, 0.28), (0.22, 0.16, 0.06)]
    for i in range(pages):
        page = doc.new_page(width=W, height=H)
        r, g, b = colors[i % len(colors)]
        page.draw_rect(fitz.Rect(0, 0, W, H), color=None,
                       fill=(r, g, b))                # фон слайда
        page.draw_rect(fitz.Rect(0, H - 26, W, H), color=None,
                       fill=(0.85, 0.22, 0.27))       # акцентная полоса снизу
        page.insert_text(fitz.Point(70, 170), titles[i % len(titles)],
                         fontsize=54, fontname="helv",
                         color=(1, 1, 1))
        page.insert_text(fitz.Point(70, 230),
                         f"Слайд {i + 1} из {pages} — демонстрация растра fitz",
                         fontsize=22, fontname="helv", color=(0.8, 0.85, 0.95))
        page.insert_text(fitz.Point(70, 300),
                         "Поверх этого раstra композитор рисует:",
                         fontsize=18, fontname="helv", color=(0.7, 0.75, 0.85))
        for k, line in enumerate(["• большой текст сверху",
                                  "• цифры-счётчик со свечением",
                                  "• таймер / часы",
                                  "• lower third плашка"]):
            page.insert_text(fitz.Point(90, 335 + k * 30), line,
                             fontsize=16, fontname="helv", color=(0.9, 0.92, 1))
    doc.save(path)
    doc.close()


def _write_wav(path: str, samples: list[float], sr: int = 44100) -> None:
    """Стерео 16-bit PCM WAV без numpy — чтобы генератор работал всегда."""
    frames = bytearray()
    for s in samples:
        v = max(-1.0, min(1.0, s))
        iv = int(v * 32767)
        frames += struct.pack("<hh", iv, iv)         # L=R
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


def make_bgm(path: str, seconds: float = 12.0, sr: int = 44100) -> None:
    """Мягкий фоновый луп: сумма синусов аккорда A-minor с медленной огибающей."""
    n = int(seconds * sr)
    freqs = (220.0, 261.63, 329.63)                  # A3, C4, E4
    out = []
    for i in range(n):
        t = i / sr
        env = 0.5 - 0.5 * math.cos(2 * math.pi * t / seconds)   # плавный вход/выход для лупа
        s = sum(math.sin(2 * math.pi * f * t) for f in freqs) / len(freqs)
        s += 0.3 * math.sin(2 * math.pi * 110 * t) * (0.6 + 0.4 * math.sin(2 * math.pi * 0.5 * t))
        out.append(0.22 * env * s)
    _write_wav(path, out, sr)


def make_ding(path: str, seconds: float = 1.6, sr: int = 44100) -> None:
    """Джингл: затухающий синус 1318 Гц (E6) + обертон."""
    n = int(seconds * sr)
    out = []
    for i in range(n):
        t = i / sr
        env = math.exp(-4.0 * t)
        s = math.sin(2 * math.pi * 1318.5 * t) + 0.4 * math.sin(2 * math.pi * 2637 * t)
        out.append(0.5 * env * s)
    _write_wav(path, out, sr)


def main() -> None:
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")
    os.makedirs(here, exist_ok=True)
    make_pdf(os.path.join(here, "presentation.pdf"))
    make_bgm(os.path.join(here, "bgm.wav"))
    make_ding(os.path.join(here, "sfx_ding.wav"))
    print("Готово. Демо-файлы в ./demo:")
    for f in sorted(os.listdir(here)):
        print("  ", f)


if __name__ == "__main__":
    main()
