"""Synchronize config.yaml into SQLite app_config."""
from db.crud import DatabaseManager
from settings import RuntimeSettings, load_config


def main() -> None:
    runtime = RuntimeSettings.from_env()
    db = DatabaseManager(runtime.database_url)
    new_cfg = load_config(runtime.config_path)
    current_version = db.get_config_version()
    new_version = db.save_config(new_cfg, current_version)
    print(f"Successfully synchronized {runtime.config_path} to SQLite (version {current_version} -> {new_version})")


if __name__ == "__main__":
    main()
