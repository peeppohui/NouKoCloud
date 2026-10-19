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

### Шаг 1. Собрать

```powershell
git clone https://github.com/<ваш-аккаунт>/svsCloud.git
cd svsCloud
packaging\build.ps1 `
    -YandexClientId "1234567890" -YandexClientSecret "секрет" `
    -GoogleClientId "xxxx.apps.googleusercontent.com" -GoogleClientSecret "GOCSPX-..." `
    -GoogleApiKey "AIza..."
```

Скрипт сам поставит зависимости, скачает ffmpeg, соберёт `dist\svsCloud\`
и положит рядом с exe `config.json` с ключами — **пользователю не нужно
ничего вводить, достаточно нажать «Аккаунты» → «Войти»**.

> Ключи OAuth лежат в репозитории только если вы добавите `config.json`
> вручную — не добавляйте. В релиз они попадают через сборку.

### Шаг 2. Залить исходники

```powershell
git add .
git commit -m "svsCloud 0.1.0"
git tag v0.1.0
git push origin main --tags
```

### Шаг 3. Прикрепить сборку к Releases

На странице репозитория: **Releases → Draft a new release → v0.1.0**.

Приложить:
* `dist\svsCloud` — запакованным в zip (например `svsCloud-0.1.0-win64.zip`);
  GitHub не примет папку, только файл;
* `docs\ARCHITECTURE.md` и `docs\OAUTH_SETUP.md` — если хотите, чтобы они были
  видны прямо на странице релиза (обычно достаточно ссылки на репозиторий).

Проверьте, что zip меньше ~400 МБ — лимит GitHub на один файл 2 ГБ, но
большие файлы лучше отдавать отдельной ссылкой (S3/Яндекс.Диск).

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
* [ ] Прогнать `python -m unittest discover -s tests -t .` — должно быть 71 OK
* [ ] Проверить на машине без Python: распаковать zip и запустить exe
