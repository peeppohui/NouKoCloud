; Установщик НоуКо Cloud (per-user, без подписи).
;
; Сборка:  powershell -File packaging\build_installer.ps1 -Version 1.0.0
;
; Подписи у установщика нет: Windows SmartScreen покажет предупреждение
; «Неизвестный издатель» — так и задумано для 1.0 (решение владельца
; продукта от 12.11.2026). Пользователь нажимает «Подробнее» → «Выполнить
; в любом случае».
;
; Установка идёт в %LOCALAPPDATA%\Programs\NoukoCloud (без прав
; администратора). При удалении программы настройки, входы в аккаунты,
; очередь и скачанные файлы НЕ трогаются: они лежат в %APPDATA%\NoukoCloud
; и в папке загрузок.

#define MyAppName "НоуКо Cloud"
#define MyAppNameShort "NoukoCloud"
#define MyAppPublisher "Нouko Cloud"
#define MyAppExeName "NoukoCloud.exe"

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\NoukoCloud"
#endif

[Setup]
AppId={{6F3D6A1E-9C42-4C58-8E7B-2A5D7C9E0B44}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\NoukoCloud
DefaultGroupName={#MyAppNameShort}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release\installer
OutputBaseFilename=NoukoCloud-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать значок на рабочем столе"; GroupDescription: "Дополнительно:"
Name: "runafterinstall"; Description: "Запустить {#MyAppName} после установки"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
; config.json рядом с программой — переносимые настройки сборки. В
; установщик он не попадает: ключи пользователь вводит сам (мастер
; настройки или «Настройки»).
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "config.json"

[Icons]
Name: "{group}\{#MyAppNameShort}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Удалить {#MyAppNameShort}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppNameShort}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent runascurrentuser; Tasks: runafterinstall

[Code]
{ Напоминание на последней странице мастера: удаление программы не стирает
  настройки, входы в аккаунты и скачанные файлы. }
procedure InitializeWizard();
var
  Note: String;
begin
  Note := 'Настройки программы, входы в аккаунты и история загрузок лежат в %APPDATA%\NoukoCloud, ' +
    'а скачанные файлы — в папке загрузок. При удалении программы они остаются на месте. ' +
    'Удалить их можно вручную.';
  WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#13 + Note;
end;
