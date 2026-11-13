# Сторонние компоненты и лицензии

НоуКо Cloud (Nouko Cloud) — программа с открытым исходным кодом.
В поставке (portable-архив `NoukoCloud-win64.zip`) используются следующие
сторонние библиотеки и программы. Тексты лицензий распространяются
вместе с программой; исходный код каждого компонента доступен по ссылкам
ниже.

Полный текст лицензии самой программы — в файле `LICENSE` рядом с архивом
и в репозитории.

---

## Qt 6 / PySide6

* **Версия:** PySide6 6.12.0 (Qt 6)
* **Лицензия:** LGPL версии 3.0 (GNU Lesser General Public License v3)
* **Роль:** графический интерфейс (Qt Widgets, Qt Multimedia, Qt Network)
* **Исходный код:**
  * PySide6 — <https://code.qt.io/cgit/pyside/pyside.git/>
  * Qt 6 — <https://github.com/qt/qtbase>
* **Текст лицензии:** <https://www.gnu.org/licenses/lgpl-3.0.html>

PySide6 используется как динамическая библиотека. Программа распространяется
в виде portable-архива; пользователь может заменить библиотеки Qt/PySide6
своими копиями (с той же лицензией) — это соответствует условиям LGPL v3.

## FFmpeg

* **Версия:** сборка из официальных сборок BtbN (Windows)
* **Лицензия:** LGPL версии 2.1 или новее (дополнительно — GPL v2
  для отдельных компонентов, отключённых при сборке)
* **Роль:** извлечение кадров превью, сохранение кадра в PNG, прокси-файлы
* **Исходный код:** <https://github.com/FFmpeg/FFmpeg>
* **Текст лицензии:** <https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html>

FFmpeg используется отдельной программой (`bin\ffmpeg.exe`), запускается
как внешний процесс. Сборка без опции `--enable-gpl` и без
`--enable-nonfree`.

## yt-dlp

* **Версия:** 2026.8.19 или новее
* **Лицензия:** Unlicense (общественное достояние)
* **Роль:** скачивание видео с социальных сетей
* **Исходный код:** <https://github.com/yt-dlp/yt-dlp>
* **Текст лицензии:** <https://unlicense.org/>

## httpx

* **Версия:** 0.28.1
* **Лицензия:** BSD 3-Clause License
* **Роль:** HTTP-клиент (API облаков, Range-запросы при докачке)
* **Исходный код:** <https://github.com/encode/httpx>
* **Текст лицензии:** <https://github.com/encode/httpx/blob/master/LICENSE>

## pywin32

* **Версия:** 312 или новее
* **Лицензия:** PSF (Python Software Foundation License)
* **Роль:** работа с реестром Windows (автозапуск), DPAPI (хранение токенов)
* **Исходный код:** <https://github.com/mhammond/pywin32>
* **Текст лицензии:** <https://github.com/mhammond/pywin32/blob/master/LICENSE

## Python и стандартная библиотека

* **Версия:** Python 3.12
* **Лицензия:** PSF License
* **Роль:** среда выполнения, в которой собрана portable-версия
* **Исходный код:** <https://github.com/python/cpython>
* **Текст лицензии:** <https://docs.python.org/3/license.html>

---

## Сторонние библиотеки Python

Portable-сборка содержит и транзитивные зависимости httpx и yt-dlp
(`httpcore`, `h11`, `certifi`, `idna`, `anyio`, `sniffio`, `websockets`,
`pycryptodomex`, `brotli`, `zstandard` и другие). Все они распространяются
под свободными лицензиями (BSD/MIT/Apache-2.0/PSF) и входят в
`python -m pip show -f` полного списка установленных пакетов.
Состав и версии пакетов фиксируются в `requirements.txt` репозитория.

---

## Шрифты

В программу включены файлы шрифтов `app/ui/assets/fonts/`:

| Шрифт | Файлы |
|---|---|
| Actay | `Actay-Regular.otf` |
| Actay Wide | `ActayWide-Bold.otf` |
| Actay Condensed | `ActayCondensed-Thin.otf` |
| Soyuz Grotesk | `SoyuzGrotesk-Bold.otf` |

Это коммерческие шрифты (собственность соответствующих foundry), они
подключаются программой как файлы (`app/ui/theme.py`, `load_fonts()`).
Если шрифт не загрузился, программа использует системный **Segoe UI**.

> **Перед публичным релизом** убедитесь, что условия лицензии этих шрифтов
> разрешают распространение вместе с программой. Если нет — уберите файлы
> из `app/ui/assets/fonts/`: программа продолжит работать на Segoe UI
> (проверьте внешний вид на тёмной и светлой темах).
