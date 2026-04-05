# OpenPanel Release Sync

Скрипт читает настройки только из JSON-конфига, заходит на страницу загрузки OpenPanel, ищет элемент с `id=second_button`, берёт `href`, извлекает имя `.exe`, получает версию из имени файла, скачивает релиз, загружает его в Google Drive через `rclone`, удаляет старые версии и обновляет `data/version.txt`.

## Структура

```text
.
├── .github/
│   └── workflows/
│       └── openpanel-sync.yml
├── config/
│   └── openpanel.json
├── data/
│   ├── state.json
│   └── version.txt
├── scripts/
│   └── sync_openpanel.py
└── requirements.txt
```

## Config

Файл [config/openpanel.json](config/openpanel.json) обязателен.

```json
{
  "check_interval_hours": 24,
  "download_url": "https://ospanel.io/download/",
  "network": {
    "page_timeout_seconds": 60,
    "download_timeout_seconds": 14400,
    "download_max_retries": 3,
    "retry_delay_seconds": 10
  },
  "parsing": {
    "button_id": "second_button",
    "file_pattern": "open_server_panel_*.exe"
  },
  "google_drive": {
    "remote_name": "gdrive",
    "remote_path": "OpenServer",
    "keep_last_versions": 3,
    "upload_max_retries": 3,
    "upload_extra_flags": [
      "--size-only"
    ]
  },
  "rclone": {
    "config_path": "~/.config/rclone/rclone.conf",
    "config_path_env": "RCLONE_CONFIG",
    "use_service_account_json": true,
    "service_account_json_env": "RCLONE_SERVICE_ACCOUNT_JSON"
  },
  "local_paths": {
    "temp_dir": "/tmp/openserver",
    "version_file": "data/version.txt",
    "state_file": "data/state.json"
  }
}
```

## GitHub Actions

- `RCLONE_CONF` — содержимое `rclone.conf`.
- `RCLONE_SERVICE_ACCOUNT_JSON` — JSON service account, если используется для Google Drive.

Workflow запускается каждый час, а прикладной интервал хранится в config. У GitHub Actions нет нативной поддержки динамического `schedule` из файла репозитория, поэтому cron в workflow задаётся отдельно от JSON-конфига.
Фактическое соблюдение `check_interval_hours` обеспечивается через [data/state.json](data/state.json), который коммитится обратно в репозиторий.

## Локальный запуск

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/sync_openpanel.py --config config/openpanel.json
```

## Текущая реализация

- версия берётся из имени файла по шаблону `open_server_panel_*.exe`;
- сравнение версий выполняется по числовым частям, не строкой;
- `version.txt` хранится в репозитории;
- `check_interval_hours` реально работает через `data/state.json`;
- парсинг идёт по `id=second_button`, затем по `href`;
- скачивание выполняется через `requests` с retry, timeout и проверкой размера файла;
- временные файлы после завершения удаляются;
- загрузка в Google Drive идёт через `rclone` с retry и флагами из config;
- если файл уже есть в Google Drive, повторная загрузка пропускается;
- старые версии удаляются после сортировки по версии из имени файла, с fallback на `ModTime`;
- если сайт, скачивание или Google Drive недоступны, `version.txt` не обновляется.
