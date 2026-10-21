# НоуКо Cloud

Настольная утилита для монтажёров: открыть папку на **Яндекс.Диске** или
**Google Диске** (свою или по публичной ссылке), посмотреть превью,
отметить нужные файлы и скачать их с докачкой.

![стек](https://img.shields.io/badge/Python-3.12%2B-blue) ![Qt](https://img.shields.io/badge/PySide6-6.12-green)
[![Скачать](https://img.shields.io/badge/скачать-НоуКо%20Cloud-2f6fed?style=flat-square)](https://github.com/peeppohui/NouKoCloud/releases/latest/download/NoukoCloud-win64.zip)

## 📥 Скачать

**[📖 Инструкция для монтажёра](docs/ИНСТРУКЦИЯ.md)** — установка, скачивание по ссылке, превью, настройки, ограничения, решение проблем

**[⬇ Скачать НоуКо Cloud для Windows](https://github.com/peeppohui/NouKoCloud/releases/latest/download/NoukoCloud-win64.zip)** — 110 МБ

![Окно программы](docs/screenshot.png)

## Для разработчика

### Быстрый старт

```powershell
python -m pip install -r requirements.txt
python run.py
```

### Сборка .exe

```powershell
powershell packaging\build.ps1
```

Готовый файл: `dist\NoukoCloud\NoukoCloud.exe`. ffmpeg для превью не нужен — плеер
уже содержит FFmpeg. `bin\ffmpeg.exe` (см. `tools\fetch_ffmpeg.py`) — только
запасной путь для редких форматов, которые плеер не открыл.

Сборку можно сразу настроить на свои ключи OAuth — тогда монтажёрам не нужно
ничего вводить, достаточно нажать «Аккаунты» → «Войти»:

```powershell
powershell packaging\build.ps1 `
    -YandexClientId "1234567890" -YandexClientSecret "секрет" `
    -GoogleClientId "xxxx.apps.googleusercontent.com" -GoogleClientSecret "GOCSPX-..."
```

### Тесты

```powershell
python -m unittest discover -s tests -t .
```

Покрыты: планирование сегментов, backoff, разбор ссылок, разбор ответов API,
человеческие формулировки ошибок, скачивание с обрывом связи и докачкой,
Range-прокси превью, плеер превью (звук, перемотка, картинки, ошибки),
дерево файлов с чекбоксами (headless, Qt offscreen).

## Документация

| Документ | О чём |
|---|---|
| [docs/ИНСТРУКЦИЯ.md](docs/ИНСТРУКЦИЯ.md) | Инструкция для монтажёра: установка, скачивание по ссылке, превью, настройки, ограничения, решение проблем |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Стек и обоснование, компоненты, алгоритмы, этапы внедрения, подводные камни API |
| [docs/OAUTH_SETUP.md](docs/OAUTH_SETUP.md) | Как получить Client ID/Secret у Яндекса и Google, как раздать ключи монтажёрам |
| [docs/RELEASING.md](docs/RELEASING.md) | Как собрать и опубликовать релиз |

## Где что лежит

```
app/core/        модели, настройки, ошибки, шины, фоновой event loop
app/net/         HTTP-клиент, лимиты запросов, backoff, кэш в SQLite
app/oauth/       Яндекс и Google: вход, обновление токена, loopback-сервер
app/providers/   список файлов, ссылки на скачивание, разбор URL
app/downloader/  очередь, сегменты, докачка, состояние на диске
app/preview/     локальный Range-прокси, плеер на QtMultimedia (FFmpeg), ffmpeg-фолбэк
app/ui/          окно, дерево, очередь, диалоги, тема
docs/            инструкция для монтажёра, архитектура, инструкция по OAuth
packaging/       сборка .exe
tools/           загрузка ffmpeg
tests/           автотесты (stdlib unittest)
```
