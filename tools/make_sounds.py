"""Синтез звуков интерфейса «НоуКо Cloud» -> app/ui/assets/sounds/*.wav.

    python tools/make_sounds.py

Характер — мягкий и нежный: только чистые синусы (без шума и «железа»),
плавная атака, тёплые невысокие ноты, лёгкое «эхо» для воздуха и
сглаживание верхов. Громкость — тихая; общий уровень задаётся в
«Настройках» (ползунок «Громкость звуков»).
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "app" / "ui" / "assets" / "sounds"


def env(t: float, attack: float, decay: float) -> float:
    """Плавная атака (без щелчка) и мягкое затухание."""
    if t < 0:
        return 0.0
    if t < attack:
        return 0.5 - 0.5 * math.cos(math.pi * t / attack)
    return math.exp(-(t - attack) / decay)


def soft_tone(freq: float, t: float) -> float:
    """Тёплый тон: основной + совсем тихая октава (как приглушённая маримба)."""
    return math.sin(2 * math.pi * freq * t) + 0.08 * math.sin(2 * math.pi * 2 * freq * t)


def render(duration: float, fn) -> list[float]:
    return [fn(i / RATE) for i in range(int(duration * RATE))]


def notes(seq, duration: float, gap: float, attack: float, decay: float, fall: float = 1.0) -> list[float]:
    """Ноты одна за другой; каждая следующая чуть тише (fall).

    Звук длится, пока последняя нота не затихнет полностью, — без обрыва.
    """
    duration = max(duration, (len(seq) - 1) * gap + attack + 7 * decay)
    def fn(t: float) -> float:
        value = 0.0
        for index, freq in enumerate(seq):
            start = index * gap
            if t >= start:
                value += (fall ** index) * soft_tone(freq, t - start) * env(t - start, attack, decay)
        return value
    return render(duration, fn)


def glide(f0: float, f1: float, duration: float, attack: float, decay: float, curve: float) -> list[float]:
    """Нота, плавно скользящая по высоте (мягкое «блюм»); длится до полного затухания."""
    duration = max(duration, attack + 7 * decay)
    phase = 0.0
    out = []
    for i in range(int(duration * RATE)):
        t = i / RATE
        freq = f1 + (f0 - f1) * math.exp(-t / curve)
        phase += 2 * math.pi * freq / RATE
        out.append(math.sin(phase) * env(t, attack, decay))
    return out


def lowpass(samples: list[float], cutoff: float) -> list[float]:
    """Сглаживание верхов (однополюсный фильтр, два прохода)."""
    a = math.exp(-2 * math.pi * cutoff / RATE)
    for _ in range(2):
        y = 0.0
        out = []
        for x in samples:
            y = (1 - a) * x + a * y
            out.append(y)
        samples = out
    return samples


def air(samples: list[float], amount: float = 0.22, tail: float = 0.6) -> list[float]:
    """Лёгкое «эхо»: несколько затухающих отражений — звук не обрывается сухо."""
    taps = ((0.031, 0.55), (0.047, 0.42), (0.071, 0.30), (0.113, 0.18))
    out = samples + [0.0] * int(tail * RATE)
    dry = list(out)
    for delay, gain in taps:
        shift = int(delay * RATE)
        for i in range(shift, len(out)):
            out[i] += dry[i - shift] * gain * amount
    return out


def finish(samples: list[float], peak: float) -> list[float]:
    top = max(abs(s) for s in samples) or 1.0
    out = [s * peak / top for s in samples]
    # Длинное мягкое затухание: последние 45% звука (но не меньше 0,15 с)
    # плавно уходят в тишину по косинусу — без ощущения обрыва.
    fade = min(len(out), max(int(0.15 * RATE), int(len(out) * 0.45)))
    for i in range(fade):
        out[-1 - i] *= 0.5 - 0.5 * math.cos(math.pi * i / fade)
    return out


def save(name: str, samples: list[float], peak: float) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = finish(samples, peak)
    with wave.open(str(OUT / f"{name}.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in data))
    print(f"{name}.wav  {len(data) / RATE * 1000:.0f} мс")


def main() -> int:
    # Касание: низкая мягкая капля, едва слышная.
    save("click", air(lowpass(glide(520, 392, 0.09, 0.004, 0.045, 0.02), 1800), 0.15, 0.25), 0.20)
    # Смена темы: нежное скольжение вверх, как вдох.
    save("toggle", air(lowpass(glide(392, 587, 0.30, 0.040, 0.11, 0.07), 2000), 0.25), 0.22)
    # Ссылка открылась: две тёплые ноты (соль — си).
    save("open", air(lowpass(notes((392.0, 493.9), 0.55, 0.09, 0.012, 0.16, 0.85), 2400)), 0.24)
    # В очередь: мягкое «блюм» снизу вверх.
    save("queue", air(lowpass(glide(330, 494, 0.20, 0.010, 0.08, 0.04), 2000), 0.2, 0.4), 0.22)
    # Всё скачано: спокойное трезвучие с октавой (до — ми — соль — до).
    save("success", air(lowpass(notes((523.3, 659.3, 784.0, 1046.5), 1.0, 0.11, 0.015, 0.22, 0.85), 2600),
                        0.25), 0.24)
    # Ошибка: тихие ноты вниз (ми — до), без резкости.
    save("error", air(lowpass(notes((329.6, 261.6), 0.6, 0.14, 0.015, 0.16, 0.9), 1500)), 0.24)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
