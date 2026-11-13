# Публикация на GitHub

Короткий ответ: **в репозиторий уходит только исходный код (~1 МБ)**.
Пользователям ничего собирать не нужно — они скачивают готовый архив
из раздела Releases.

Релиз 1.0 состоит из:

| Ассет | Что это | Обязателен |
|---|---|---|
| `NoukoCloud-win64.zip` | portable-сборка (папка с `NoukoCloud.exe`) | да |
| `NoukoCloud-win64.zip.sha256` | контрольная сумма архива | да |
| `latest.json` | манифест релиза (версия, размер, sha256) | да |
| `THIRD_PARTY_NOTICES.md` | лицензии сторонних библиотек | да |
| `NoukoCloud-Setup-<версия>.exe` | установщик (Inno Setup, без подписи — см. §7) | да |

Имя архива **нельзя менять**: от него зависит постоянная ссылка
`releases/latest/download/NoukoCloud-win64.zip`.

---

## 1. Что лежит в репозитории

```
app/            исходный код программы
tests/          автотесты (stdlib unittest)
docs/           инструкция, архитектура, подключение аккаунтов, релиз
packaging/      сборка .exe (PyInstaller), сценарии релиза
tools/          ffmpeg, манифест релиза, публикация на GitHub
run.py          запуск из исходников
requirements.txt зависимости
THIRD_PARTY_NOTICES.md лицензии сторонних библиотек
README.md, LICENSE, .gitignore, .gitattributes
```

## 2. Что НЕ должно попасть в git

| Что | Почему | Где лежит вместо |
|---|---|---|
| `dist/` | бинарная сборка | GitHub **Releases** |
| `build/` | промежуточные файлы PyInstaller | генерируется при сборке |
| `bin/ffmpeg.exe` | чужая программа | качается скриптом `tools/fetch_ffmpeg.py` |
| `release/` | архив, `.sha256`, `latest.json` | GitHub **Releases** |
| `config.json` | **ключи OAuth** | `%APPDATA%\NoukoCloud\config.json` |
| `tokens.dat` | **токены входа** (DPAPI) | `%APPDATA%\NoukoCloud\tokens.dat` |
| `*.log`, `cache.db`, `*.part` | личные данные и мусор | `%APPDATA%\NoukoCloud\` |

Всё это уже закрыто `.gitignore`. Перед коммитом проверьте:

```powershell
git status --short
git ls-files | Select-String "config.json|tokens.dat|dist/|bin/|release/"
```

Второй вывод должен быть пустым.

## 3. Кому что нужно

| Кто | Что делает | Что качает |
|---|---|---|
| **Пользователь (монтажёр)** | скачивает установщик `NoukoCloud-Setup-*.exe` (или zip), запускает | ничего |
| **Разработчик** | `git clone` → `pip install -r requirements.txt` → `python run.py` | Python 3.12+, ~120 МБ пакетов |
| **Сборщик релиза** | `packaging\release.ps1 -Version X.Y.Z` | Python + ffmpeg (качается сам) + Inno Setup 6 |

## 4. Как выпустить релиз

### Шаг 1. Прогнать тесты

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python -m unittest discover -s tests -t . -v
```

Тесты должны быть зелёными **до** сборки. Два полных прогона одновременно
не запускать: тесты single-instance, кэша и портов мешают друг другу.

### Шаг 2. Закоммитить и пометить версией

Версия живёт в двух местах: `app/__init__.py` (`__version__`) и текст
`CHANGES` в `tools/publish_release.py` (из него берутся заметки релиза).

```powershell
git add .
git commit -m "НоуКо Cloud 1.0.0"
git tag -a v1.0.0 -m "1.0.0"
git push origin main --tags
```

### Шаг 3. Собрать и опубликовать

```powershell
packaging\release.ps1 -Version 1.0.0
```

Скрипт делает всё по порядку:

1. `packaging\build.ps1` — собирает `dist\NoukoCloud\` (без ffmpeg берёт
   `bin\ffmpeg.exe`, если его нет — качает `tools\fetch_ffmpeg.py`);
2. архив `release\NoukoCloud-win64.zip` (имя фиксированное);
3. `tools\release_manifest.py` — **проверяет, что в архиве нет настоящих
   ключей OAuth**, считает SHA256 и пишет `NoukoCloud-win64.zip.sha256`
   и `latest.json`;
4. `packaging\build_installer.ps1` — собирает установщик
   `release\installer\NoukoCloud-Setup-<версия>.exe` (Inno Setup; нужен
   установленный Inno Setup 6, ищется в `%LOCALAPPDATA%\Programs\Inno\ISCC.exe`
   и в `Program Files\Inno Setup 6`);
5. `tools\publish_release.py` — создаёт/обновляет релиз на GitHub и заливает
   ассеты (zip, `.sha256`, `latest.json`, `THIRD_PARTY_NOTICES.md`, установщик).

Если Inno Setup не установлен, релиз можно опубликовать и без установщика —
`release.ps1` предупредит об этом (флаг `-SkipInstaller` запрещает сборку
установщика явно).

Если шаг 3 нашёл в архиве настоящие ключи — публикация останавливается.
Соберите заново **без** ключей (по умолчанию они и не вставляются):

```powershell
packaging\release.ps1 -Version 1.0.0          # публичный релиз, без ключей
packaging\release.ps1 -Version 1.0.0 -WithKeys # приватная раздача (осторожно!)
```

`-WithKeys` вставляет `config.json` с ключами рядом с exe — такой архив
**нельзя** выкладывать публично.

Токен для публикации берётся из `git credential fill` — в репозитории
его быть не должно.

### Шаг 4. Проверить релиз

* ссылка `releases/latest/download/NoukoCloud-win64.zip` отдаёт архив;
* `NoukoCloud-win64.zip.sha256` совпадает с `sha256sum -c`;
* `latest.json` содержит ту же версию, размер и sha256;
* в распакованном архиве **нет** `config.json` с ключами (в публичном релизе);
* установщик ставится без прав администратора, ярлык появился в «Пуск»,
  в `%LOCALAPPDATA%\Programs\NoukoCloud` лежит `NoukoCloud.exe`;
* после удаления программы на месте остались `%APPDATA%\NoukoCloud`
  и папка загрузок;
* в журнале `%APPDATA%\NoukoCloud\logs\noukocloud.log` после первого
  запуска нет ни ERROR, ни WARNING.

## 5. Сборка одним файлом (по желанию)

```powershell
packaging\build.ps1 -OneFile
```

Получится один `NoukoCloud.exe`, который сам распаковывается во временную
папку при запуске. Запускается чуть медленнее, но пользователю удобнее.
ffmpeg в этом случае всё равно должен лежать рядом отдельной папкой `bin/`.

## 6. Что стоит сделать перед публикацией

* [ ] Завести приложения в Яндексе и Google (см. `docs/OAUTH_SETUP.md`)
* [ ] Проверить, что приложение Яндекса — типа «Для доступа к API»
* [ ] `python -m unittest discover -s tests -t .` — все тесты зелёные
* [ ] Проверить на машине без Python: поставить установщик и запустить
      программу; отдельно — распаковать zip и запустить exe
* [ ] Проверить мастер первого запуска на чистом профиле
      (`%APPDATA%\NoukoCloud` удалён) и на профиле с прежней версией
* [ ] Убедиться, что в публичном архиве нет `config.json` с секретами

## 7. Подпись и установщик

**Установщик сделан** (`packaging/installer.iss`, Inno Setup 6):

* ставится per-user в `%LOCALAPPDATA%\Programs\NoukoCloud`, права
  администратора не нужны (`PrivilegesRequired=lowest`);
* ярлык в меню «Пуск», по желанию — на рабочем столе;
* свой деинсталлятор; при удалении **не трогает** `%APPDATA%\NoukoCloud`
  (настройки, токены) и папку загрузок пользователя — об этом сказано
  на последнем экране установщика;
* portable `config.json` с ключами в установщик не попадает (`Excludes`);
* язык установщика — русский.

**Подписи нет.** Сертификата OV/EV на руках нет, поэтому Windows и
SmartScreen показывают «Неизвестный издатель» — это ожидаемо, так и написано
в заметках релиза. Пользователю нужно нажать «Подробнее» → «Выполнить в
любом случае». Подписать нужно exe, установщик, деинсталлятор и
helper-процесс обновления; для каждого — метка времени (RFC 3161), иначе
подпись просрочится. EV-сертификат даёт зелёный SmartScreen сразу.

**Обновление.** Полный сценарий (скачивание, проверка подписи, откат)
перенесён на 1.1. В 1.0 программа только проверяет GitHub Releases и
открывает страницу релиза; выключается в настройках.

## 8. Как откатить релиз

Релиз на GitHub можно удалить (Releases → Delete) — постоянная ссылка
`releases/latest/download/...` продолжит отдавать предыдущую версию.
Тег можно удалить: `git tag -d v1.0.0 && git push origin --delete v1.0.0`.
