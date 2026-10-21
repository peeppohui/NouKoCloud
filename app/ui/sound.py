"""Звуки интерфейса: щелчок кнопки, открытие ссылки, очередь, готово, ошибка.

Звуки — короткие тихие WAV из app/ui/assets/sounds (синтез, tools/make_sounds.py).
Выключаются в «Настройках» (config.ui_sounds).
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QUrl

from .theme import ASSETS_DIR

log = logging.getLogger(__name__)

SOUNDS = ("click", "toggle", "open", "queue", "success", "error")
VOLUME = 0.6        # 0..1; меняется ползунком в «Настройках»
#: Копий каждого звука: новый щелчок не обрывает предыдущий на полуслове
#: (QSoundEffect.play() перезапускает звук с начала).
VOICES = 3
#: Не чаще: десяток кликов подряд не должен превращаться в дробь.
MIN_GAP = {"click": 0.05, "success": 0.6, "error": 0.8}


class SoundBank(QObject):
    def __init__(self, enabled: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.enabled = enabled
        self._effects: dict[str, object] = {}      # первая копия (для громкости/тестов)
        self._voices: dict[str, list] = {}
        self._next: dict[str, int] = {}
        self._last: dict[str, float] = {}
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except Exception as exc:  # pragma: no cover - сборка без QtMultimedia
            log.info("Звуки интерфейса недоступны: %s", exc)
            return
        for name in SOUNDS:
            path = ASSETS_DIR / "sounds" / f"{name}.wav"
            if not path.exists():
                continue
            voices = []
            for _ in range(VOICES):
                effect = QSoundEffect(self)
                effect.setSource(QUrl.fromLocalFile(str(path)))
                effect.setVolume(VOLUME)
                voices.append(effect)
            self._voices[name] = voices
            self._effects[name] = voices[0]

    def set_volume(self, percent: int) -> None:
        """Громкость в процентах (0–100)."""
        volume = max(0, min(100, int(percent))) / 100
        for voices in self._voices.values():
            for effect in voices:
                effect.setVolume(volume)

    def play(self, name: str, force: bool = False) -> None:
        if not self.enabled:
            return
        effect = self._effects.get(name)
        if effect is None:
            return
        now = time.monotonic()
        if not force and now - self._last.get(name, 0.0) < MIN_GAP.get(name, 0.1):
            return
        self._last[name] = now
        voices = self._voices.get(name)
        if voices and self._effects.get(name) is voices[0]:
            # Свободная копия, а если все играют — та, что начала раньше всех.
            index = self._next.get(name, 0)
            free = [v for v in voices if not v.isPlaying()]
            effect = free[0] if free else voices[index % len(voices)]
            self._next[name] = index + 1
        try:
            effect.play()
        except Exception:
            log.debug("Звук %s не проигрался", name, exc_info=True)


_bank: SoundBank | None = None


def bank() -> SoundBank:
    global _bank
    if _bank is None:
        _bank = SoundBank()
    return _bank


def play(name: str) -> None:
    bank().play(name)
