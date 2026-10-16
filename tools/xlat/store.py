"""Кэш переводов: sqlite `work/t9n/cache.sqlite`.

Ключ записи — `key` из корпуса (`path|table|row|col`). Кэш-хит = status='ok'
и совпадают prompt_ver, glossary_sha, model, src_sha.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS t(
  key TEXT PRIMARY KEY,
  src_sha TEXT NOT NULL,
  src TEXT NOT NULL,
  ru TEXT,
  status TEXT NOT NULL,              -- ok | failed
  model TEXT,
  prompt_ver INTEGER,
  glossary_sha TEXT,
  attempts INTEGER DEFAULT 0,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  error TEXT,
  flags TEXT,
  review TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS t_src_sha ON t(src_sha);
CREATE INDEX IF NOT EXISTS t_status ON t(status);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def glossary_sha(glossary_path: str, style_path: str) -> str:
    h = hashlib.sha256()
    for p in (glossary_path, style_path):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()[:16]


def prompt_version(prompt_path: str) -> int:
    """Первая строка вида `<!-- prompt-ver: N -->`."""
    try:
        with open(prompt_path, encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return 0
    if "prompt-ver:" in first:
        tail = first.split("prompt-ver:")[1].strip().rstrip("-").strip().rstrip(">").strip()
        try:
            return int(tail)
        except ValueError:
            return 0
    return 0


class Store:
    def __init__(self, path: str = "work/t9n/cache.sqlite"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=60)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- чтение ---------------------------------------------------------
    def cached(self, key: str, src_sha: str, model: str, pver: int, gsha: str,
               ignore_versions: bool = False) -> str | None:
        """Перевод из кэша.

        `ignore_versions` снимает сверку `prompt_ver` и `glossary_sha`: строки, переведённые
        до правки промпта или глоссария, остаются валидными, а расхождения чинятся точечной
        перегонкой (`--retranslate-filter`). Иначе любая правка промпта или добавленная
        запись глоссария обнуляли бы весь кэш на 60 тыс. строк.
        """
        sql = "SELECT ru FROM t WHERE key=? AND src_sha=? AND status='ok' AND model=?"
        params: list = [key, src_sha, model]
        if not ignore_versions:
            sql += " AND prompt_ver=? AND glossary_sha=?"
            params.extend([pver, gsha])
        row = self.db.execute(sql, params).fetchone()
        return row[0] if row else None

    def translation_by_sha(self, src_sha: str, model: str, pver: int, gsha: str) -> str | None:
        row = self.db.execute(
            "SELECT ru FROM t WHERE src_sha=? AND status='ok' AND model=? AND prompt_ver=? "
            "AND glossary_sha=? LIMIT 1",
            (src_sha, model, pver, gsha),
        ).fetchone()
        return row[0] if row else None

    def counts(self) -> dict:
        out = {}
        for status, n in self.db.execute("SELECT status, COUNT(*) FROM t GROUP BY status"):
            out[status] = n
        return out

    # --- запись ---------------------------------------------------------
    def put(
        self,
        key: str,
        src: str,
        src_sha: str,
        ru: str | None,
        status: str,
        model: str,
        pver: int,
        gsha: str,
        attempts: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: str | None = None,
        flags: str | None = None,
        review: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO t(key, src_sha, src, ru, status, model, prompt_ver, glossary_sha,
                             attempts, tokens_in, tokens_out, error, flags, review, updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               -- Провал повторной попытки НЕ должен уничтожать готовый перевод:
               -- оставляем прежний текст, статус, модель и пометки, меняем только
               -- счётчик попыток, причину и время.
               ON CONFLICT(key) DO UPDATE SET
                 ru=CASE WHEN excluded.status='failed' AND t.status='ok'
                         THEN t.ru ELSE excluded.ru END,
                 status=CASE WHEN excluded.status='failed' AND t.status='ok'
                             THEN t.status ELSE excluded.status END,
                 flags=CASE WHEN excluded.status='failed' AND t.status='ok'
                            THEN t.flags ELSE excluded.flags END,
                 model=CASE WHEN excluded.status='failed' AND t.status='ok'
                            THEN t.model ELSE excluded.model END,
                 prompt_ver=CASE WHEN excluded.status='failed' AND t.status='ok'
                                 THEN t.prompt_ver ELSE excluded.prompt_ver END,
                 glossary_sha=CASE WHEN excluded.status='failed' AND t.status='ok'
                                   THEN t.glossary_sha ELSE excluded.glossary_sha END,
                 attempts=excluded.attempts, tokens_in=excluded.tokens_in,
                 tokens_out=excluded.tokens_out, error=excluded.error,
                 review=excluded.review, updated_at=excluded.updated_at""",
            (
                key,
                src_sha,
                src,
                ru,
                status,
                model,
                pver,
                gsha,
                attempts,
                tokens_in,
                tokens_out,
                error,
                flags,
                review,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    def commit(self) -> None:
        self.db.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
        self.db.commit()
