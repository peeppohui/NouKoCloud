# Публикация на GitHub

Короткий ответ: **в репозиторий уходит только исходный код (~1 МБ)**.
Пользователям ничего собирать не нужно — они скачивают готовый `.exe`
из раздела Releases.

---

## 1. Что лежит в репозитории

```
app/            исходный код программы (~90 файлов, 600 КБ)
tests/          автотесты (71 шт.)
docs/           архитектура, инструкция по OAuth, скриншот
packaging/      сборка .exe (PyInstaller)
tools/          загрузка ffmpeg
run.py          запуск из исходников
requirements.txt зависимости
README.md, LICENSE, .gitignore, .gitattributes
```

## 2. Что НЕ должно попасть в git

| Что | Почему | Где лежит вместо |
|---|---|---|
| `dist/` (328 МБ) | бинарная сборка, git её не любит | GitHub **Releases** |
| `build/` | промежуточные файлы PyInstaller | генерируется при сборке |
| `bin/ffmpeg.exe` (~80 МБ) | чужая программа | качается скриптом `tools/fetch_ffmpeg.py` |
| `config.json` | **ключи OAuth** | `%APPDATA%\svsCloud\config.json` |
| `tokens.dat` | **токены входа** (DPAPI) | `%APPDATA%\svsCloud\tokens.dat` |
| `*.log`, `cache.db`, `*.part` | личные данные и мусор | `%APPDATA%\svsCloud\` |

Всё это уже закрыто `.gitignore`. **Перед первым коммитом проверьте:**

```powershell
git status --short
git ls-files | Select-String "config.json|tokens.dat|dist/|bin/"
```

Второй вывод должен быть пустым.

## 3. Кому что нужно

| Кто | Что делает | Что качает |
|---|---|---|
| **Пользователь (монтажёр)** | скачивает `.exe` из Releases, распаковывает, запускает | ничего |
| **Разработчик** | `git clone` → `pip install -r requirements.txt` → `python run.py` | Python 3.12+, ~120 МБ пакетов |
| **Сборщик релиза** | `packaging\build.ps1` | Python + ffmpeg (качается сам) |

## 4. Как выпустить релиз

### Одна команда

```powershell
packaging\release.ps1 -Version 0.2.0
```

Скрипт сам:

1. соберёт `dist\svsCloud` (зависимости, ffmpeg, PyInstaller);
2. упакует его в `release\svsCloud-win64.zip` — **имя архива постоянное**,
   чтобы ссылка `releases/latest/download/svsCloud-win64.zip` никогда
   не ломалась между версиями;
3. создаст (или обновит) релиз с тегом `v<версия>` и зальёт архив через
   API GitHub. Токен берётся из хранилища git (Git Credential Manager) —
   при первом запуске откроется окно входа.

Если в сборку нужны ключи OAuth (чтобы монтажёрам не пришлось их вводить):

```powershell
packaging\release.ps1 -Version 0.2.0 `
    -YandexClientId "1234567890" -YandexClientSecret "секрет" `
    -GoogleClientId "xxxx.apps.googleusercontent.com" -GoogleClientSecret "GOCSPX-..."
```

Опции: `-SkipBuild` (не пересобирать, только перезалить архив),
`-SkipFfmpeg` (ffmpeg уже скачан).

### Шаг 2. Залить исходники

```powershell
git add .
git commit -m "svsCloud 0.2.0"
git tag v0.2.0
git push origin main --tags
```

> Тег `v0.2.0` должен указывать на тот же коммит, что и `main` — релиз
> создаётся скриптом через API и берёт содержимое ветки по умолчанию.

### Шаг 3. Прикрепить сборку к Releases (вручную)

Обычно не нужно — всё делает `release.ps1`. Если всё же руками:
**Releases → Draft a new release → v0.2.0**, приложить
`release\svsCloud-win64.zip` (GitHub не примет папку, только файл).

Лимит GitHub на один файл — 2 ГБ, наш архив — 136 МБ, запас большой.

## 5. Сборка одним файлом (по желанию)

```powershell
packaging\build.ps1 -OneFile
```

Получится один `svsCloud.exe`, который сам распаковывается во
временную папку при запуске. Запускается чуть медленнее, но пользователю
удобнее («один файл — и работает»). ffmpeg в этом случае всё равно должен
лежать рядом отдельной папкой `bin/`.

## 6. Что стоит сделать перед публикацией

* [ ] Завести приложения в Яндексе и Google (см. `docs/OAUTH_SETUP.md`)
* [ ] Проверить, что приложение Яндекса — типа «Для доступа к API»
* [ ] Добавить иконку `packaging/app.ico` (сейчас иконки нет — будет дефолтная)
* [ ] Решить, показывать ли свои ключи OAuth в сборке (удобно монтажёрам)
* [ ] Прогнать `python -m unittest discover -s tests -t .` — должно быть 74 OK
* [ ] Проверить на машине без Python: распаковать zip и запустить exe
