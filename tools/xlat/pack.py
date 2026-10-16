"""Пакование правленых JSON в `.bdat` (`bdat-toolset pack`) с проверкой round-trip.

Вход: `work/bdat_json_ru` (копия JSON с подставленными переводами) и отчёт `apply.tsv`
со списком затронутых bdat-файлов (пути вида `bdat/gb/game/menu`).
Выход: `work/bdat_packed/<путь архива>/<stem>.bdat`, для каждого файла выполняется
обратная распаковка и семантическое сравнение JSON (тот же набор таблиц, те же строки
и ячейки) — иначе файл исключается из релиза и попадает в `work/reports/pack_failures.tsv`.

Запуск (из каталога `tools`):
    python -m xlat.pack --json ../work/bdat_json_ru --out ../work/bdat_packed \
        --apply-report ../work/reports/apply.tsv --tool ../work/src/bdat-rs/target/release/bdat-toolset.exe
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tempfile
from concurrent.futures import ProcessPoolExecutor
import os
import shutil
import subprocess


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def load_table(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def semantic_diff(a_dir: str, b_dir: str) -> list[str]:
    """Сравнивает каталоги таблиц (JSON) — список расхождений."""
    problems = []
    fa = {n for n in os.listdir(a_dir) if n.endswith(".json")}
    fb = {n for n in os.listdir(b_dir) if n.endswith(".json")}
    if fa != fb:
        problems.append(f"набор таблиц: только слева {sorted(fa - fb)[:3]}, только справа {sorted(fb - fa)[:3]}")
        return problems
    for name in sorted(fa):
        ta = load_table(os.path.join(a_dir, name))
        tb = load_table(os.path.join(b_dir, name))
        if ta != tb:
            problems.append(f"{name}: JSON отличается")
    return problems


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def same_tree(a: str, b: str) -> bool:
    """Совпадают ли каталоги таблиц (набор JSON и их содержимое)."""
    if not os.path.isdir(a) or os.path.isdir(b) is False:
        return False
    fa = sorted(n for n in os.listdir(a) if n.endswith(".json"))
    fb = sorted(n for n in os.listdir(b) if n.endswith(".json"))
    if fa != fb:
        return False
    for n in fa:
        pa, pb = os.path.join(a, n), os.path.join(b, n)
        if os.path.getsize(pa) == os.path.getsize(pb) and file_hash(pa) == file_hash(pb):
            continue
        # Байты разошлись — это ещё не изменение: `apply` перезаписывает JSON со своим
        # форматированием. Значимо только расхождение по значениям.
        try:
            with open(pa, encoding="utf-8") as fa_, open(pb, encoding="utf-8") as fb_:
                if json.load(fa_) != json.load(fb_):
                    return False
        except (OSError, ValueError):
            return False
    return True


def pack_one(job: dict) -> tuple[str, str, str]:
    """Пакует один bdat-файл и проверяет round-trip. Возвращает (path, статус, детали).

    Статусы: `packed`, `unchanged`, `failed`.
    """
    path, stem = job["path"], job["stem"]
    tables_src, schema_src = job["tables_src"], job["schema_src"]
    if not os.path.isdir(tables_src) or not os.path.isfile(schema_src):
        return path, "failed", f"нет {tables_src} или {schema_src}"
    if not job["pack_unchanged"] and same_tree(job["orig_tables"], tables_src):
        return path, "unchanged", ""
    with tempfile.TemporaryDirectory(prefix="bdatpack_") as tmp:
        stage = os.path.join(tmp, stem)
        os.makedirs(stage, exist_ok=True)
        shutil.copy2(schema_src, os.path.join(stage, f"{stem}.bschema"))
        shutil.copytree(tables_src, os.path.join(stage, stem), dirs_exist_ok=True)
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir, exist_ok=True)
        res = run([job["tool"], "pack", stage, "-o", out_dir, "-f", "json"])
        bdat = os.path.join(out_dir, f"{stem}.bdat")
        if res.returncode != 0 or not os.path.isfile(bdat):
            return path, "failed", (res.stderr or res.stdout or "pack не создал файл")[-200:]
        back_dir = os.path.join(tmp, "back")
        extract_cmd = [job["tool"], "extract", bdat, "-o", back_dir, "-f", "json"]
        if os.path.isfile(job["hashes"]):
            extract_cmd += ["--hashes", job["hashes"]]
        res = run(extract_cmd)
        back_tables = os.path.join(back_dir, stem)
        if res.returncode != 0 or not os.path.isdir(back_tables):
            return path, "failed", (res.stderr or "extract не удался")[-200:]
        problems = semantic_diff(tables_src, back_tables)
        if problems:
            return path, "failed", "; ".join(problems)[:200]
        dst = os.path.join(job["out"], path + ".bdat")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(bdat, dst)
    return path, "packed", ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.pack")
    ap.add_argument("--json", default="../work/bdat_json_ru")
    ap.add_argument("--orig", default="../work/bdat_json",
                    help="дерево исходных JSON: файлы без изменений не пакуются")
    ap.add_argument("--pack-unchanged", action="store_true",
                    help="паковать и файлы без изменений (по умолчанию пропускаются)")
    ap.add_argument("--out", default="../work/bdat_packed")
    ap.add_argument("--apply-report", default="../work/reports/apply.tsv")
    ap.add_argument("--tool", default="../work/src/bdat-rs/target/release/bdat-toolset.exe")
    ap.add_argument("--only", default="", help="ограничить пути (через запятую), напр. gb/game/menu")
    ap.add_argument("--jobs", type=int, default=8,
                    help="параллельных упаковщиков (каждый файл — два вызова bdat-toolset)")
    ap.add_argument("--hashes", default="../work/namehashes.txt",
                    help="карта имён для проверочной распаковки (чтобы имена колонок совпадали)")
    args = ap.parse_args(argv)

    touched: list[str] = []
    if os.path.isfile(args.apply_report):
        with open(args.apply_report, encoding="utf-8") as f:
            for row in csv.reader(f, delimiter="\t"):
                if len(row) >= 3 and row[2] == "ok" and row[0]:
                    touched.append(row[0])
    if args.only:
        wanted = {p.strip() for p in args.only.split(",") if p.strip()}
        touched = [p for p in touched if p in wanted]
    touched = sorted(set(touched))
    print(f"к пакованию: {len(touched)} bdat-файлов")
    if not touched:
        return 0

    jobs = []
    for path in touched:
        stem = os.path.basename(path)
        jobs.append({
            "path": path, "stem": stem,
            "tables_src": os.path.join(args.json, path),
            "orig_tables": os.path.join(args.orig, path),
            "schema_src": os.path.join(os.path.dirname(os.path.join(args.json, path)), f"{stem}.bschema"),
            "tool": args.tool, "out": args.out, "hashes": args.hashes,
            "pack_unchanged": args.pack_unchanged,
        })

    failures: list[tuple[str, str]] = []
    packed = unchanged = 0
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for path, status, detail in pool.map(pack_one, jobs, chunksize=4):
            if status == "packed":
                packed += 1
                if packed % 200 == 0:
                    print(f"  упаковано: {packed}", flush=True)
            elif status == "unchanged":
                unchanged += 1
            else:
                failures.append((path, detail))

    report = os.path.join(os.path.dirname(args.apply_report), "pack_failures.tsv")
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8") as f:
        f.write("path\treason\n")
        for path, reason in failures:
            f.write(f"{path}\t{reason}\n")
    print(f"упаковано: {packed}, без изменений: {unchanged}, ошибок: {len(failures)} -> {report}")
    for path, reason in failures[:5]:
        print(f"  ! {path}: {reason[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
