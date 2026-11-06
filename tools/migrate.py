"""Run serialized, versioned database migrations before web/worker startup."""
from db.crud import DatabaseManager
from settings import RuntimeSettings, load_config


def main() -> None:
    runtime = RuntimeSettings.from_env()
    defaults = load_config(runtime.config_path)
    database = DatabaseManager(runtime.database_url)
    try:
        database.get_config(defaults)
        database.warn_config_drift(defaults)
        if not database.health().get("ready", False):
            raise SystemExit("Database migration readiness check failed")
        print("Database schema and configuration initialized")
    finally:
        database.close()


if __name__ == "__main__":
    main()
