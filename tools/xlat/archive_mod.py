"""Сборка мод-архива XC3: патч `bf3.arh`/`bf3.ard` утилитой `ardpack`.

Зачем: `xcnx-file-loader` (подмена файлов в romfs) в Eden/yuzu не срабатывает — там
перевод ставится заменой архива в mod-romfs. Скрипт берёт архив пользователя (слитый
base+update), раскладывает локальные файлы в staging-каталог, зеркалящий пути архива,
и правит записи ARH + дописывает данные в ARD (режим `append`, сжатие zstd).

ВАЖНО (грабли): входной `--arh` должен быть текущим (уже пропатченным) ARH. Если подать
чистый, ранее вкатанные записи теряются и игра вернётся к оригинальному тексту.

Запуск (из каталога tools):
    python -m xlat.archive_mod --ard ../work/romfs_update/1/bf3.ard \
        --arh ../work/romfs_update/1/bf3.arh --out-arh ../work/mod/bf3.arh \
        --ard-out ../work/mod/bf3.ard --packed ../work/bdat_packed \
        --fonts ../work/font/patched
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

FONTS = ("ascii.wifnt", "standard.wifnt", "talk.wifnt")
HERE = os.path.dirname(os.path.abspath(__file__))


def _default(name: str, rel: str) -> str:
    """Путь по умолчанию: раскладка репозитория, иначе — рядом со скриптом.

    Скрипт живёт в `tools/xlat`, поэтому корень репозитория — на два уровня выше;
    в релизном пакете `archive_mod.py` лежит рядом с `bf3_patch/` и `ardpack.exe`,
    и там `work/**` просто нет.
    """
    # `rel` задан в стиле соседних модулей (`../work/...`) — это путь от `tools/`,
    # поэтому для корня репозитория ведущие `../` надо снять.
    from_root = rel.lstrip("./")
    for cand in (
        os.path.join(HERE, "..", "..", from_root),   # python -m xlat.archive_mod
        rel,                                         # запуск из корня репозитория
    ):
        if os.path.exists(cand):
            return os.path.normpath(cand)
    return os.path.normpath(os.path.join(HERE, name))


def build_stage(packed: str, fonts: str, stage: str) -> list[str]:
    """Раскладывает файлы в staging и возвращает пути внутри архива."""
    shutil.rmtree(stage, ignore_errors=True)
    paths: list[str] = []
    for dirpath, _dirs, fnames in os.walk(packed):
        for name in fnames:
            if not name.endswith(".bdat"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), packed).replace(os.sep, "/")
            archive_path = "/bdat/" + rel
            dst = os.path.join(stage, "bdat", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, name), dst)
            paths.append(archive_path)
    for name in FONTS:
        src = os.path.join(fonts, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(stage, "menu", "font", name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        paths.append(f"/menu/font/{name}")
    return sorted(paths)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.archive_mod")
    ap.add_argument("--ard", required=True, help="входной bf3.ard (слитый base+update)")
    ap.add_argument("--arh", required=True, help="входной bf3.arh (текущий, уже пропатченный)")
    ap.add_argument("--ard-out", default="", help="куда положить исправленный ARD (по умолчанию — править --ard на месте)")
    ap.add_argument("--out-arh", required=True, help="куда записать обновлённый ARH")
    ap.add_argument("--packed", default=_default("bf3_patch", "../work/bdat_packed"))
    ap.add_argument("--fonts", default=_default("bf3_patch", "../work/font/patched"))
    ap.add_argument("--stage", default=_default("_patch_stage", "../work/patch_stage"))
    ap.add_argument("--ardpack", default=_default("ardpack.exe", "../tools/ardpack/target/release/ardpack.exe"))
    ap.add_argument("--compress", default="zstd", choices=["none", "zlib", "zstd"])
    args = ap.parse_args(argv)

    if not os.path.isfile(args.ardpack):
        print(f"нет ardpak: {args.ardpack} (собрать: cargo build --release в tools/ardpack)")
        return 2

    paths = build_stage(args.packed, args.fonts, args.stage)
    print(f"в staging: {len(paths)} файлов")
    if not paths:
        return 1

    ard_target = args.ard_out or args.ard
    if args.ard_out and os.path.abspath(args.ard_out) != os.path.abspath(args.ard):
        print(f"копирую ARD: {args.ard} -> {args.ard_out}")
        shutil.copy2(args.ard, args.ard_out)

    # В командную строку Windows влезает ~32 КБ, а путей тысячи, поэтому режем на партии.
    # Каждая следующая партия берёт на вход ARH, полученный предыдущей: ardpak правит
    # записи поверх текущего состояния, поэтому порядок обязателен.
    batch_size = 150
    cur_arh = args.arh
    total = 0
    for start in range(0, len(paths), batch_size):
        chunk = paths[start : start + batch_size]
        tmp_arh = args.out_arh + ".tmp"
        cmd = [
            args.ardpack,
            "--arh", cur_arh,
            "--ard", ard_target,
            "--out-arh", tmp_arh,
            "--dir", args.stage,
            "--mode", "append",
            "--compress", args.compress,
            *chunk,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print("ошибка ardpak:", (res.stderr or res.stdout or "")[-400:])
            return 1
        shutil.move(tmp_arh, args.out_arh)
        cur_arh = args.out_arh
        total += len(chunk)
        print(f"  партия {start//batch_size + 1}: +{len(chunk)} путей (всего {total})", flush=True)
    print(f"готово: {args.out_arh}, {ard_target} ({os.path.getsize(ard_target)/2**30:.2f} GiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
