import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = "OpenPanelReleaseSync/1.0"
VERSION_RE = re.compile(r"(\d+(?:[._-]\d+)+)")


@dataclass(frozen=True)
class ReleaseAsset:
    version: str
    url: str
    filename: str


@dataclass(frozen=True)
class AppConfig:
    check_interval_hours: int
    download_url: str
    page_timeout_seconds: int
    download_timeout_seconds: int
    download_max_retries: int
    retry_delay_seconds: int
    button_id: str
    file_pattern: str
    remote_name: str
    remote_path: str
    keep_last_versions: int
    upload_max_retries: int
    upload_extra_flags: list[str]
    rclone_config_path: str | None
    rclone_config_path_env: str | None
    use_service_account_json: bool
    service_account_json_env: str | None
    temp_dir: Path
    version_file: Path
    state_file: Path


def log(message: str) -> None:
    print(message, flush=True)


def load_config(config_path: Path) -> AppConfig:
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    network = raw_config.get("network", {})
    parsing = raw_config.get("parsing", {})
    google_drive = raw_config.get("google_drive", {})
    rclone = raw_config.get("rclone", {})
    local_paths = raw_config.get("local_paths", {})

    return AppConfig(
        check_interval_hours=int(raw_config["check_interval_hours"]),
        download_url=str(raw_config["download_url"]),
        page_timeout_seconds=int(network.get("page_timeout_seconds", 60)),
        download_timeout_seconds=int(network.get("download_timeout_seconds", 14400)),
        download_max_retries=int(network.get("download_max_retries", 3)),
        retry_delay_seconds=int(network.get("retry_delay_seconds", 10)),
        button_id=str(parsing["button_id"]),
        file_pattern=str(parsing["file_pattern"]),
        remote_name=str(google_drive["remote_name"]),
        remote_path=str(google_drive["remote_path"]),
        keep_last_versions=int(google_drive["keep_last_versions"]),
        upload_max_retries=int(google_drive.get("upload_max_retries", 3)),
        upload_extra_flags=list(google_drive.get("upload_extra_flags", [])),
        rclone_config_path=rclone.get("config_path"),
        rclone_config_path_env=rclone.get("config_path_env"),
        use_service_account_json=bool(rclone.get("use_service_account_json", False)),
        service_account_json_env=rclone.get("service_account_json_env"),
        temp_dir=Path(str(local_paths["temp_dir"])).expanduser(),
        version_file=Path(str(local_paths.get("version_file", "data/version.txt"))),
        state_file=Path(str(local_paths.get("state_file", "data/state.json"))),
    )


def read_current_version(version_file: Path) -> str:
    if not version_file.exists():
        return ""
    return version_file.read_text(encoding="utf-8").strip()


def write_current_version(version_file: Path, version: str) -> None:
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(f"{version}\n", encoding="utf-8")


def read_state_timestamp(state_file: Path) -> datetime | None:
    if not state_file.exists():
        return None

    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        raw_timestamp = state.get("last_checked_at", "")
        if not raw_timestamp:
            return None
        return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def write_state_timestamp(state_file: Path, checked_at: datetime) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {"last_checked_at": checked_at.astimezone(timezone.utc).isoformat()}
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def should_skip_by_interval(config: AppConfig, now_utc: datetime) -> bool:
    last_checked_at = read_state_timestamp(config.state_file)
    if last_checked_at is None:
        return False

    next_allowed_at = last_checked_at + timedelta(hours=config.check_interval_hours)
    if now_utc < next_allowed_at:
        log(
            "[INFO] Check interval not reached yet, "
            f"next run after {next_allowed_at.astimezone(timezone.utc).isoformat()}"
        )
        return True

    return False


def normalize_version(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts)


def fetch_download_page(config: AppConfig) -> str:
    log(f"[INFO] Fetching page: {config.download_url}")
    last_error: Exception | None = None

    for attempt in range(1, config.download_max_retries + 1):
        try:
            response = requests.get(
                config.download_url,
                timeout=config.page_timeout_seconds,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as error:
            last_error = error
            log(
                f"[WARN] Page fetch attempt {attempt}/{config.download_max_retries} failed: {error}"
            )
            if attempt < config.download_max_retries:
                time.sleep(config.retry_delay_seconds)

    assert last_error is not None
    raise last_error


def extract_version(value: str) -> str | None:
    match = VERSION_RE.search(value)
    if not match:
        return None
    return match.group(1).replace("_", ".").replace("-", ".")


def select_latest_asset(html: str, config: AppConfig) -> ReleaseAsset:
    soup = BeautifulSoup(html, "html.parser")
    button = soup.find(id=config.button_id)
    if button is None:
        raise RuntimeError(f"Не найден элемент с id={config.button_id}")

    href = button.get("href")
    if not href:
        raise RuntimeError(f"У элемента id={config.button_id} отсутствует href")

    absolute_url = urljoin(config.download_url, href.strip())
    filename = Path(urlparse(absolute_url).path).name
    if not fnmatch.fnmatch(filename.lower(), config.file_pattern.lower()):
        raise RuntimeError(
            f"Имя файла {filename} не соответствует шаблону {config.file_pattern}"
        )

    version = extract_version(filename)
    if not version:
        raise RuntimeError("Не удалось извлечь версию из имени файла")

    asset = ReleaseAsset(version=version, url=absolute_url, filename=filename)
    log(f"[INFO] Parsed version: {asset.version} ({asset.filename})")
    return asset


def download_file(asset: ReleaseAsset, config: AppConfig, temp_dir: Path) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / asset.filename
    log(f"[INFO] Downloading file to: {target}")

    last_error: Exception | None = None
    for attempt in range(1, config.download_max_retries + 1):
        try:
            with requests.get(
                asset.url,
                stream=True,
                timeout=config.download_timeout_seconds,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                response.raise_for_status()
                expected_size = int(response.headers.get("Content-Length", "0") or "0")
                written_size = 0
                with target.open("wb") as file_handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            file_handle.write(chunk)
                            written_size += len(chunk)

                if written_size <= 0:
                    raise RuntimeError("Downloaded file is empty")
                if expected_size > 0 and written_size != expected_size:
                    raise RuntimeError(
                        "Downloaded file size mismatch: "
                        f"expected {expected_size}, got {written_size}"
                    )

                return target
        except (requests.RequestException, RuntimeError) as error:
            last_error = error
            if target.exists():
                target.unlink(missing_ok=True)
            log(
                f"[WARN] Download attempt {attempt}/{config.download_max_retries} failed: {error}"
            )
            if attempt < config.download_max_retries:
                time.sleep(config.retry_delay_seconds)

    assert last_error is not None
    raise last_error


def build_rclone_target(config: AppConfig) -> str:
    remote_path = config.remote_path.strip("/")
    if remote_path:
        return f"{config.remote_name}:{remote_path}"
    return f"{config.remote_name}:"


def build_command_env(config: AppConfig, temp_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    temp_dir.mkdir(parents=True, exist_ok=True)

    if config.rclone_config_path_env and config.rclone_config_path:
        env.setdefault(
            config.rclone_config_path_env,
            str(Path(config.rclone_config_path).expanduser()),
        )

    if config.use_service_account_json and config.service_account_json_env:
        service_account_json = os.getenv(config.service_account_json_env, "").strip()
        if service_account_json:
            service_account_path = temp_dir / "service-account.json"
            service_account_path.write_text(service_account_json, encoding="utf-8")
            remote_env_name = (
                f"RCLONE_CONFIG_{config.remote_name.upper()}_SERVICE_ACCOUNT_FILE"
            )
            env[remote_env_name] = str(service_account_path)

    return env


def build_rclone_flags(config: AppConfig) -> list[str]:
    flags = [
        "--retries",
        str(config.upload_max_retries),
        "--low-level-retries",
        str(config.upload_max_retries),
    ]
    flags.extend(config.upload_extra_flags)
    return flags


def build_rclone_cmd(config: AppConfig, *args: str) -> list[str]:
    command = ["rclone"]
    if config.rclone_config_path:
        command.extend(["--config", str(Path(config.rclone_config_path).expanduser())])
    command.extend(args)
    return command


def run_command(
    command: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    log(f"[INFO] Running command: {' '.join(command)}")
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        env=env,
    )


def remote_file_exists(
    filename: str, rclone_target: str, config: AppConfig, env: dict[str, str]
) -> bool:
    result = run_command(
        build_rclone_cmd(config, "lsjson", rclone_target, "--files-only"),
        env=env,
    )
    files = parse_lsjson(result.stdout)
    return any(item.get("Name") == filename for item in files)


def rclone_copy(
    local_file: Path, rclone_target: str, config: AppConfig, env: dict[str, str]
) -> None:
    run_command(
        build_rclone_cmd(
            config,
            "copyto",
            *build_rclone_flags(config),
            str(local_file),
            f"{rclone_target.rstrip('/')}/{local_file.name}",
        ),
        env=env,
    )
    log(f"[INFO] Uploaded file to {rclone_target}")


def cleanup_temp_files(*paths: Path) -> None:
    for path in paths:
        if path.exists() and path.is_file():
            path.unlink(missing_ok=True)
            log(f"[INFO] Removed temp file: {path}")


def parse_lsjson(stdout: str) -> list[dict]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Не удалось разобрать ответ rclone lsjson") from error

    if not isinstance(data, list):
        raise RuntimeError("rclone lsjson вернул неожиданный формат")
    return [item for item in data if isinstance(item, dict) and not item.get("IsDir")]


def cleanup_old_versions(
    rclone_target: str,
    keep_count: int,
    file_pattern: str,
    config: AppConfig,
    env: dict[str, str],
) -> None:
    result = run_command(build_rclone_cmd(config, "lsjson", rclone_target), env=env)
    files = parse_lsjson(result.stdout)
    filtered_files = [
        item
        for item in files
        if fnmatch.fnmatch(item.get("Name", "").lower(), file_pattern.lower())
    ]
    if len(filtered_files) <= keep_count:
        log(f"[INFO] Cleanup skipped, files count: {len(filtered_files)}")
        return

    filtered_files.sort(
        key=lambda item: (
            normalize_version(extract_version(item.get("Name", "")) or "0"),
            item.get("ModTime", ""),
            item.get("Name", ""),
        ),
        reverse=True,
    )
    stale_files = filtered_files[keep_count:]
    for file_info in stale_files:
        name = file_info["Name"]
        run_command(
            build_rclone_cmd(
                config,
                "deletefile",
                f"{rclone_target.rstrip('/')}/{name}",
            ),
            env=env,
        )
        log(f"[INFO] Removed old file: {name}")


def ensure_rclone_available(config: AppConfig, env: dict[str, str]) -> None:
    try:
        run_command(build_rclone_cmd(config, "version"), env=env)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("rclone недоступен в окружении") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync latest OpenPanel release to Google Drive"
    )
    parser.add_argument("--config", default="config/openpanel.json")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config))
    except Exception as error:
        log(f"[ERROR] Config load failed: {error}")
        return 1

    temp_dir = config.temp_dir
    command_env = build_command_env(config, temp_dir)
    rclone_target = build_rclone_target(config)
    service_account_path = temp_dir / "service-account.json"
    now_utc = datetime.now(timezone.utc)
    log(f"[INFO] Configured check interval: {config.check_interval_hours}h")

    if should_skip_by_interval(config, now_utc):
        return 0

    try:
        ensure_rclone_available(config, command_env)
        html = fetch_download_page(config)
        latest_asset = select_latest_asset(html, config)
    except requests.RequestException as error:
        log(f"[ERROR] Site request failed: {error}")
        return 1
    except Exception as error:
        log(f"[ERROR] Initialization failed: {error}")
        return 1

    current_version = read_current_version(config.version_file)
    log(f"[INFO] Current saved version: {current_version or '<empty>'}")
    if current_version and normalize_version(current_version) >= normalize_version(
        latest_asset.version
    ):
        write_state_timestamp(config.state_file, now_utc)
        log("[INFO] No new version found")
        return 0

    downloaded_file = temp_dir / latest_asset.filename
    try:
        if remote_file_exists(
            latest_asset.filename, rclone_target, config, command_env
        ):
            log("[INFO] File already exists in Google Drive, upload skipped")
        else:
            downloaded_file = download_file(latest_asset, config, temp_dir)
            rclone_copy(downloaded_file, rclone_target, config, command_env)
        cleanup_old_versions(
            rclone_target,
            config.keep_last_versions,
            config.file_pattern,
            config,
            command_env,
        )
    except requests.RequestException as error:
        log(f"[ERROR] Download failed: {error}")
        return 1
    except subprocess.CalledProcessError as error:
        if error.stdout:
            log(error.stdout.strip())
        if error.stderr:
            log(error.stderr.strip())
        log(f"[ERROR] External command failed: {' '.join(error.cmd)}")
        return 1
    except Exception as error:
        log(f"[ERROR] Sync failed: {error}")
        return 1
    finally:
        cleanup_temp_files(downloaded_file, service_account_path)

    write_current_version(config.version_file, latest_asset.version)
    write_state_timestamp(config.state_file, now_utc)
    log(f"[INFO] Updated saved version to: {latest_asset.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
