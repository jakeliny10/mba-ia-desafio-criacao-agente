import argparse

from aurora.config import Settings
from aurora.storage import Store


def main():
    parser = argparse.ArgumentParser(description="Assistente do Residencial Aurora")
    parser.add_argument("command", choices=["servir", "restaurar"])
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "restaurar":
        # API must be stopped. Keep the reservation-code ledger across restoration.
        Store(settings.database, settings.data_dir).restore()
        for suffix in ("", "-wal", "-shm"):
            (settings.storage_dir / ("sessoes.sqlite3" + suffix)).unlink(missing_ok=True)
        print("Reservas e visitantes restaurados de dados/. Sessões apagadas.")
    else:
        import uvicorn

        uvicorn.run("aurora.api:app", host="0.0.0.0", port=8000, workers=1)


if __name__ == "__main__":
    main()
