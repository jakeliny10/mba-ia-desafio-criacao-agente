"""Synthetic fixtures: never replace or rewrite the assignment's dados/ directory."""

import json


def write_seeds(path):
    path.mkdir(parents=True, exist_ok=True)
    values = {
        "apartamentos": [
            {"numero": n, "morador": "Morador de teste"} for n in ("101", "201", "302")
        ],
        "areas": [
            {"id": "quadra", "nome": "Quadra", "taxa": 0},
            {"id": "salao", "nome": "Salão", "taxa": 150},
        ],
        "reservas": [
            {"codigo": "SEED-1", "apartamento": "101", "area": "quadra", "data": "2030-03-09"},
            {"codigo": "SEED-2", "apartamento": "302", "area": "salao", "data": "2030-03-16"},
        ],
        "visitantes": [{"apartamento": "302", "nome": "Visitante privado", "data": "2030-03-16"}],
    }
    for name, value in values.items():
        (path / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    (path / "regulamento.md").write_text(
        "# Regulamento\n\n## Piscina\nAos domingos, fecha às 19h.\n\n"
        "## Elevadores\nTrecho exclusivo sobre elevadores.\n",
        encoding="utf-8",
    )
