"""Persistência e autorização: nenhuma decisão crítica depende do modelo."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path


class Conflict(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def valid_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use uma data no formato AAAA-MM-DD.")
    return date.fromisoformat(value).isoformat()


class Store:
    def __init__(self, path: Path, data_dir: Path):
        self.path = path
        self.data_dir = data_dir

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        # Read all seeds first: missing/invalid data must not create a partial database.
        seeds = {
            name: json.loads((self.data_dir / f"{name}.json").read_text())
            for name in ("apartamentos", "areas", "reservas", "visitantes")
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS apartamentos (numero TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS areas (
                    id TEXT PRIMARY KEY, nome TEXT NOT NULL, taxa REAL NOT NULL CHECK(taxa>=0)
                );
                CREATE TABLE IF NOT EXISTS reservas (
                    codigo TEXT PRIMARY KEY,
                    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
                    area TEXT NOT NULL REFERENCES areas(id), data TEXT NOT NULL,
                    ativa INTEGER NOT NULL DEFAULT 1 CHECK(ativa IN (0,1))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS reserva_ativa_por_data
                    ON reservas(area, data) WHERE ativa=1;
                CREATE TABLE IF NOT EXISTS codigos_emitidos (id INTEGER PRIMARY KEY AUTOINCREMENT);
                CREATE TABLE IF NOT EXISTS visitantes (
                    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
                    nome TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(apartamento, nome, data)
                );
                CREATE TABLE IF NOT EXISTS sessoes (
                    id TEXT PRIMARY KEY, apartamento TEXT NOT NULL REFERENCES apartamentos(numero)
                );
                CREATE TRIGGER IF NOT EXISTS apartamento_imutavel
                    BEFORE UPDATE OF apartamento ON sessoes
                    BEGIN SELECT RAISE(ABORT, 'Apartamento da sessão é imutável'); END;
                CREATE TABLE IF NOT EXISTS confirmacoes (
                    id TEXT PRIMARY KEY, sessao TEXT NOT NULL REFERENCES sessoes(id),
                    chamada TEXT NOT NULL, acao TEXT NOT NULL, detalhes TEXT NOT NULL,
                    autor TEXT NOT NULL, invocacao TEXT NOT NULL,
                    estado TEXT NOT NULL DEFAULT 'pendente', aprovado INTEGER,
                    UNIQUE(sessao, chamada)
                );
                CREATE TABLE IF NOT EXISTS operacoes (
                    sessao TEXT NOT NULL, chamada TEXT NOT NULL, resultado TEXT NOT NULL,
                    PRIMARY KEY(sessao, chamada)
                );
                CREATE TABLE IF NOT EXISTS metadados (chave TEXT PRIMARY KEY);
            """)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM metadados WHERE chave='inicializado'").fetchone():
                return
            db.executemany(
                "INSERT INTO apartamentos VALUES (?)",
                [(str(item["numero"]),) for item in seeds["apartamentos"]],
            )
            db.executemany("INSERT INTO areas VALUES (:id,:nome,:taxa)", seeds["areas"])
            db.executemany(
                "INSERT INTO reservas(codigo,apartamento,area,data) "
                "VALUES (:codigo,:apartamento,:area,:data)",
                seeds["reservas"],
            )
            db.executemany(
                "INSERT INTO visitantes VALUES (:apartamento,:nome,:data)", seeds["visitantes"]
            )
            db.execute("INSERT INTO metadados VALUES ('inicializado')")

    def restore(self):
        """Restore visible seed state, retaining the ledger of all previously issued codes."""
        self.initialize()
        reservations = json.loads((self.data_dir / "reservas.json").read_text())
        visitors = json.loads((self.data_dir / "visitantes.json").read_text())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE reservas SET ativa=0")
            db.executemany(
                "INSERT INTO reservas(codigo,apartamento,area,data,ativa) "
                "VALUES (:codigo,:apartamento,:area,:data,1) "
                "ON CONFLICT(codigo) DO UPDATE SET apartamento=excluded.apartamento, "
                "area=excluded.area,data=excluded.data,ativa=1",
                reservations,
            )
            db.execute("DELETE FROM visitantes")
            db.executemany("INSERT INTO visitantes VALUES (:apartamento,:nome,:data)", visitors)
            db.execute("DELETE FROM operacoes")
            db.execute("DELETE FROM confirmacoes")
            db.execute("DELETE FROM sessoes")

    def apartment_exists(self, apartment):
        with self.connect() as db:
            return bool(
                db.execute("SELECT 1 FROM apartamentos WHERE numero=?", (apartment,)).fetchone()
            )

    def register_session(self, session_id, apartment):
        with self.connect() as db:
            db.execute("INSERT INTO sessoes VALUES (?,?)", (session_id, apartment))

    def apartment_for_session(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT apartamento FROM sessoes WHERE id=?", (session_id,)).fetchone()
            return row[0] if row else None

    def areas(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,nome,taxa FROM areas ORDER BY id")]

    def area(self, area_id):
        return next((area for area in self.areas() if area["id"] == area_id), None)

    def reservations(self, apartment):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT codigo,area,data FROM reservas WHERE apartamento=? AND ativa=1 ORDER BY data,codigo",
                    (apartment,),
                )
            ]

    def visitors(self, apartment):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT nome,data FROM visitantes WHERE apartamento=? ORDER BY data,nome",
                    (apartment,),
                )
            ]

    def available(self, area_id, day):
        with self.connect() as db:
            return not db.execute(
                "SELECT 1 FROM reservas WHERE area=? AND data=? AND ativa=1", (area_id, day)
            ).fetchone()

    def cancel(self, apartment, area_id, day):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE reservas SET ativa=0 WHERE apartamento=? AND area=? AND data=? AND ativa=1",
                (apartment, area_id, day),
            ).rowcount
            return {"status": "cancelada" if changed else "reserva_propria_nao_encontrada"}

    def add_pending(
        self, *, confirmation_id, session_id, call_id, action, details, author, invocation
    ):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO confirmacoes(id,sessao,chamada,acao,detalhes,autor,invocacao) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    confirmation_id,
                    session_id,
                    call_id,
                    action,
                    canonical(details),
                    author,
                    invocation,
                ),
            )

    def pending(self, session_id):
        with self.connect() as db:
            return [
                {"id": row["id"], "acao": row["acao"], "detalhes": json.loads(row["detalhes"])}
                for row in db.execute(
                    "SELECT * FROM confirmacoes WHERE sessao=? AND estado='pendente' ORDER BY rowid",
                    (session_id,),
                )
            ]

    def decide(self, session_id, confirmation_id, approved):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE confirmacoes SET estado='respondida',aprovado=? "
                "WHERE sessao=? AND id=? AND estado='pendente'",
                (int(approved), session_id, confirmation_id),
            ).rowcount
            if not changed:
                raise Conflict("Confirmação não está pendente nesta sessão.")
            return dict(
                db.execute("SELECT * FROM confirmacoes WHERE id=?", (confirmation_id,)).fetchone()
            )

    def execute(self, *, session_id, apartment, call_id, action, details, confirmed=False):
        """Consume the exact approved operation and write its result in the same transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT apartamento FROM sessoes WHERE id=?", (session_id,)
            ).fetchone()
            if not owner or owner[0] != apartment:
                raise ValueError("Identidade da sessão inválida.")
            prior = db.execute(
                "SELECT resultado FROM operacoes WHERE sessao=? AND chamada=?",
                (session_id, call_id),
            ).fetchone()
            if prior:
                return json.loads(prior[0])
            charged = (
                action == "reservar_area"
                and db.execute("SELECT taxa FROM areas WHERE id=?", (details["area"],)).fetchone()[
                    0
                ]
                > 0
            )
            if charged or action == "autorizar_visitante":
                grant = db.execute(
                    "SELECT * FROM confirmacoes WHERE sessao=? AND chamada=? "
                    "AND acao=? AND detalhes=? AND estado='respondida'",
                    (session_id, call_id, action, canonical(details)),
                ).fetchone()
                if not grant:
                    return {"status": "confirmacao_pela_api_necessaria"}
                if not grant["aprovado"]:
                    result = {"status": "negada", "mensagem": "Nenhuma alteração realizada."}
                    self._record(db, session_id, call_id, result)
                    return result
                if not confirmed:
                    return {"status": "confirmacao_pela_api_necessaria"}
            if action == "reservar_area":
                # Monotonic IDs plus the permanent PK prevent reuse, even after cancellation.
                while True:
                    sequence = db.execute("INSERT INTO codigos_emitidos DEFAULT VALUES").lastrowid
                    code = f"AUR-{sequence:08d}"
                    if not db.execute("SELECT 1 FROM reservas WHERE codigo=?", (code,)).fetchone():
                        break
                try:
                    db.execute(
                        "INSERT INTO reservas(codigo,apartamento,area,data) VALUES (?,?,?,?)",
                        (code, apartment, details["area"], details["data"]),
                    )
                    result = {"status": "reservada", "codigo": code, **details}
                except sqlite3.IntegrityError:
                    if not db.execute(
                        "SELECT 1 FROM reservas WHERE area=? AND data=? AND ativa=1",
                        (details["area"], details["data"]),
                    ).fetchone():
                        raise
                    result = {"status": "indisponivel", "mensagem": "Área ocupada nesta data."}
            elif action == "autorizar_visitante":
                db.execute(
                    "INSERT OR IGNORE INTO visitantes VALUES (?,?,?)",
                    (apartment, details["nome"], details["data"]),
                )
                result = {"status": "autorizado", **details}
            else:
                raise ValueError("Ação desconhecida.")
            self._record(db, session_id, call_id, result)
            return result

    @staticmethod
    def _record(db, session_id, call_id, result):
        db.execute("INSERT INTO operacoes VALUES (?,?,?)", (session_id, call_id, canonical(result)))

    def operation_result(self, session_id, call_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT resultado FROM operacoes WHERE sessao=? AND chamada=?",
                (session_id, call_id),
            ).fetchone()
            return json.loads(row[0]) if row else None
