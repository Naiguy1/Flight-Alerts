"""Synchronize config.yaml into SQLite app_config."""
from db.crud import DatabaseManager
from settings import RuntimeSettings, load_config


def main() -> None:
    runtime = RuntimeSettings.from_env()
    new_cfg = load_config(runtime.config_path)
    db = DatabaseManager(runtime.database_url)
    try:
        db.get_config(new_cfg)
        current_version = db.get_config_version()
        if db.get_config(new_cfg) == new_cfg:
            print(f"SQLite configuration is synchronized (version {current_version})")
            return
        new_version = db.save_config(new_cfg, current_version)
        print(f"Synchronized YAML to SQLite (version {current_version} -> {new_version})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
