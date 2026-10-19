import re
import unicodedata
from pathlib import Path


def normalize(text):
    return "".join(
        char
        for char in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(char)
    )


class Regulation:
    """Index headings locally; return just one relevant section, never the document."""

    def __init__(self, path: Path):
        text = path.read_text(encoding="utf-8")
        headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+)$", text))
        # Split at chapter level, keeping subsections such as opening hours inside
        # their parent chapter. Never fall back to returning the entire document.
        levels = [len(heading.group(1)) for heading in headings]
        chapter_levels = [level for level in set(levels) if levels.count(level) > 1]
        chapter_level = min(chapter_levels) if chapter_levels else None
        headings = [heading for heading in headings if len(heading.group(1)) == chapter_level]
        self.sections = []
        for index, heading in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            body = text[heading.end() : end].strip()
            if body:
                self.sections.append((heading.group(2), body))

    def consult(self, topic: str):
        words = set(re.findall(r"[a-z0-9]+", normalize(topic)))
        words -= {"a", "o", "de", "da", "do", "das", "dos", "e", "em", "para", "sobre"}
        ranked = []
        for title, body in self.sections:
            title_words = set(re.findall(r"[a-z0-9]+", normalize(title)))
            score = len(words & title_words)
            if score:
                ranked.append((score, title, body))
        if not ranked:
            return {"status": "assunto_nao_localizado", "mensagem": "Informe o assunto específico."}
        ranked.sort(key=lambda row: row[0], reverse=True)
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return {"status": "assunto_ambiguo", "mensagem": "Especifique um único assunto."}
        _, title, body = ranked[0]
        return {"fonte": "dados/regulamento.md", "secao": title, "trecho": body}
