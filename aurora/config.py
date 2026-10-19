import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "aurora"
USER_ID = "morador"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "dados"
    storage_dir: Path = ROOT / "var"
    model: str = "gemini-3.5-flash-lite"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            storage_dir=Path(os.getenv("AURORA_STORAGE_DIR") or ROOT / "var").resolve(),
            model=os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite",
        )

    @property
    def database(self):
        return self.storage_dir / "condominio.sqlite3"

    @property
    def sessions_url(self):
        return f"sqlite+aiosqlite:///{self.storage_dir / 'sessoes.sqlite3'}"
