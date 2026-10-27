"""Run serialized, versioned database migrations before web/worker startup."""
from db.crud import DatabaseManager
from settings import RuntimeSettings, load_config


def main() -> None:
    runtime = RuntimeSettings.from_env()
    database = DatabaseManager(runtime.database_url)
    database.get_config(load_config(runtime.config_path))
    if not database.health().get("ready", False):
        raise SystemExit("Database migration readiness check failed")
    print("Database schema and configuration initialized")


if __name__ == "__main__":
    main()
