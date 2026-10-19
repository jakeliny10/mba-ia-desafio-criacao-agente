import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aurora.regulation import Regulation
from aurora.storage import Conflict, Store, valid_date
from tests.fixtures import write_seeds


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "dados"
        write_seeds(self.data)
        self.store = Store(self.root / "test.sqlite3", self.data)
        self.store.initialize()
        for session_id, apartment in (("s1", "101"), ("s2", "201"), ("s3", "302")):
            self.store.register_session(session_id, apartment)

    def pending(
        self, session="s1", call="c1", confirmation="p1", details=None, action="reservar_area"
    ):
        details = details or {"area": "salao", "data": "2030-04-20", "taxa": 150}
        self.store.add_pending(
            confirmation_id=confirmation,
            session_id=session,
            call_id=call,
            action=action,
            details=details,
            author="assistente_aurora",
            invocation="inv1",
        )
        return details

    def execute(
        self,
        details,
        session="s1",
        apartment="101",
        call="c1",
        action="reservar_area",
        confirmed=True,
    ):
        return self.store.execute(
            session_id=session,
            apartment=apartment,
            call_id=call,
            action=action,
            details=details,
            confirmed=confirmed,
        )

    def test_seed_preserved_and_not_reimported(self):
        before = {file.name: file.read_bytes() for file in self.data.iterdir()}
        self.store.cancel("101", "quadra", "2030-03-09")
        self.store.initialize()
        self.assertEqual([], self.store.reservations("101"))
        self.assertEqual(before, {file.name: file.read_bytes() for file in self.data.iterdir()})

    def test_isolation_and_private_availability(self):
        self.assertEqual(
            "reserva_propria_nao_encontrada",
            self.store.cancel("101", "salao", "2030-03-16")["status"],
        )
        self.assertEqual("SEED-2", self.store.reservations("302")[0]["codigo"])
        self.assertFalse(self.store.available("salao", "2030-03-16"))
        self.assertNotIn("SEED-2", json.dumps(self.store.reservations("101")))
        self.assertEqual([], self.store.visitors("101"))

    def test_session_identity_is_immutable(self):
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as db:
            db.execute("UPDATE sessoes SET apartamento='302' WHERE id='s1'")
        with self.assertRaises(ValueError):
            self.execute({"area": "quadra", "data": "2030-04-20", "taxa": 0}, apartment="302")

    def test_claimed_confirmation_is_not_authorization(self):
        details = self.pending()
        self.assertEqual("confirmacao_pela_api_necessaria", self.execute(details)["status"])
        self.assertEqual(1, len(self.store.reservations("101")))

    def test_denial_and_duplicate_response(self):
        details = self.pending()
        self.store.decide("s1", "p1", False)
        self.assertEqual("negada", self.execute(details, confirmed=False)["status"])
        self.assertEqual(1, len(self.store.reservations("101")))
        with self.assertRaises(Conflict):
            self.store.decide("s1", "p1", True)

    def test_approval_has_exactly_one_effect(self):
        details = self.pending()
        self.store.decide("s1", "p1", True)
        first = self.execute(details)
        second = self.execute(details)
        self.assertEqual(first, second)
        self.assertEqual(2, len(self.store.reservations("101")))
        with self.assertRaises(Conflict):
            self.store.decide("s1", "p1", True)

    def test_confirmation_bound_to_session_and_arguments(self):
        details = self.pending()
        with self.assertRaises(Conflict):
            self.store.decide("s2", "p1", True)
        with self.assertRaises(Conflict):
            self.store.decide("s1", "unknown", True)
        self.store.decide("s1", "p1", True)
        changed = {**details, "data": "2030-07-01"}
        self.assertEqual("confirmacao_pela_api_necessaria", self.execute(changed)["status"])
        self.assertEqual(
            "confirmacao_pela_api_necessaria", self.execute(details, confirmed=False)["status"]
        )

    def test_free_reservation_and_canceled_code_never_reused(self):
        details = {"area": "quadra", "data": "2030-04-06", "taxa": 0}
        first = self.execute(details, confirmed=False)
        self.store.cancel("101", "quadra", "2030-04-06")
        self.store = Store(self.store.path, self.data)
        second = self.execute(details, call="c2", confirmed=False)
        self.assertNotEqual(first["codigo"], second["codigo"])
        self.assertEqual([], self.store.pending("s1"))

    def test_visitor_requires_approval(self):
        details = self.pending(
            action="autorizar_visitante", details={"nome": "Joana", "data": "2030-04-21"}
        )
        self.assertEqual(
            "confirmacao_pela_api_necessaria",
            self.execute(details, action="autorizar_visitante")["status"],
        )
        self.assertEqual([], self.store.visitors("101"))
        self.store.decide("s1", "p1", True)
        self.execute(details, action="autorizar_visitante")
        self.assertEqual([details], self.store.visitors("101"))

    def test_simultaneous_approvals_have_one_winner(self):
        details = self.pending()
        self.pending(session="s2", confirmation="p2", details=details)
        barrier = threading.Barrier(2)

        def approve(session, apartment, confirmation):
            barrier.wait()
            self.store.decide(session, confirmation, True)
            return self.execute(details, session=session, apartment=apartment)

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [
                pool.submit(approve, "s1", "101", "p1"),
                pool.submit(approve, "s2", "201", "p2"),
            ]
            statuses = sorted(task.result()["status"] for task in tasks)
        self.assertEqual(["indisponivel", "reservada"], statuses)
        total = [
            r
            for apt in ("101", "201")
            for r in self.store.reservations(apt)
            if r["data"] == details["data"]
        ]
        self.assertEqual(1, len(total))

    def test_unique_index_protects_direct_insert(self):
        with self.assertRaises(sqlite3.IntegrityError), self.store.connect() as db:
            db.execute("INSERT INTO reservas VALUES ('OTHER','201','quadra','2030-03-09',1)")

    def test_regulation_returns_only_requested_chapter(self):
        result = Regulation(self.data / "regulamento.md").consult("piscina")
        self.assertIn("19h", result["trecho"])
        self.assertNotIn("elevadores", json.dumps(result).lower())

    def test_restore_keeps_used_codes_and_restores_seed(self):
        details = {"area": "quadra", "data": "2030-04-06", "taxa": 0}
        first = self.execute(details)
        self.store.cancel("101", "quadra", "2030-03-09")
        self.store.restore()
        self.assertEqual(
            [{"codigo": "SEED-1", "area": "quadra", "data": "2030-03-09"}],
            self.store.reservations("101"),
        )
        self.assertIsNone(self.store.apartment_for_session("s1"))
        self.store.register_session("s1", "101")
        second = self.execute(details)
        self.assertNotEqual(first["codigo"], second["codigo"])

    def test_regulation_keeps_subsections_without_other_chapters(self):
        path = self.root / "regulamento-com-subsecoes.md"
        path.write_text(
            "# Regulamento\n## Piscina\n### Horários\nDomingo até 19h.\n"
            "### Limpeza\nFechada segunda-feira.\n## Elevadores\nOutro assunto.\n"
        )
        result = Regulation(path).consult("piscina")
        self.assertIn("19h", result["trecho"])
        self.assertNotIn("Outro assunto", result["trecho"])

    def test_dates_must_be_iso(self):
        self.assertEqual("2030-04-20", valid_date("2030-04-20"))
        for invalid in ("20300420", "2030-2-1", "2030-02-30"):
            with self.assertRaises(ValueError):
                valid_date(invalid)


if __name__ == "__main__":
    unittest.main()
