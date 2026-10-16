"""Сборка мода: `dist/` (romfs-пакет для эмулятора, SD-пакет для консоли, zip, инструкция).

Вход:
  `work/bdat_packed/**`   — упакованные bdat (пути без префикса `bdat/`, напр. `gb/game/menu.bdat`)
  `work/font/patched/*.wifnt` — патченые шрифты (ascii/standard/talk)
  `work/bin/loader_xc3.zip`   — релиз xcnx-file-loader для XC3 (exefs + romfs/skyline)
  `work/ard_bdat/bdat/**`     — список путей исходного архива (для проверки)

Выход:
  dist/xc3-ru-emu/mods/contents/010074F013262000/xc3-ru/{exefs,romfs}
  dist/xc3-ru-switch/atmosphere/contents/010074F013262000/{exefs,romfs}
  dist/INSTALL.md, dist/xc3-ru-emu-v<версия>.zip, dist/xc3-ru-switch-v<версия>.zip

Версия берётся из файла `VERSION` в корне репозитория и попадает в имена архивов,
в `version.txt` внутри пакетов и в шапку `INSTALL.md`.

Запуск (из каталога `tools`):
    python -m xlat.dist --title 010074F013262000
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
import zipfile

FONTS = ("ascii.wifnt", "standard.wifnt", "talk.wifnt")


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_version() -> str:
    """Версия перевода — из файла `VERSION` в корне репозитория."""
    try:
        with open(os.path.join(repo_root(), "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def write_version_txt(path: str, version: str, package: str) -> None:
    """Пакет сам сообщает, какая версия перевода внутри."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"Xenoblade Chronicles 3 — русский перевод {version}\n")
        f.write(f"пакет: {package}\n")
        f.write(f"собран: {time.strftime('%Y-%m-%d')}\n")

INSTALL_MD = """# Русский перевод Xenoblade Chronicles 3 ({title})

**Версия перевода: {version}** ({date}).

Список известных недочётов и непереведённых строк — в репозитории проекта,
файл `docs/KNOWN-ISSUES.md`.

Перевод одевается как обычный LayeredFS-мод. Два режима установки — они не
взаимозаменяемы, потому что эмуляторы и консоль по-разному работают с архивом игры.

## Консоль (Atmosphère) — режим лоадера

Файлы перевода подменяются внутри `bf3.ard` на лету плагином `xcnx-file-loader`;
сам архив (≈10 ГБ) не заменяется, на SD уезжает ~16 МБ.

**Что нужно до установки:**

* прошивка Atmosphère (свежая) — мод работает как обычный LayeredFS;
* игра **с установленным апдейтом 2.2.1**: тексты взяты из него, на версии 1.0.0
  файлы не совпадут по содержимому;
* язык игры в системе — **English** (переведён именно английский набор `gb`).

**Установка:**

1. Выключить консоль, достать SD и подключить к компьютеру (или смонтировать по сети).
2. Скопировать содержимое папки `xc3-ru-switch/` в **корень** SD,
   (файлы лягут в `atmosphere/contents/{title}/...`), ничего не удаляя:

```
sd:/atmosphere/contents/{title}/exefs/{main.npdm,subsdk9}
sd:/atmosphere/contents/{title}/romfs/bdat/**.bdat
sd:/atmosphere/contents/{title}/romfs/menu/font/*.wifnt
sd:/atmosphere/contents/{title}/romfs/skyline/plugins/libxcnx_file_loader.nro
```

3. Вернуть SD, включить консоль, запустить игру.

**Проверка:** главное меню должно быть по-русски («НОВАЯ ИГРА», «НАСТРОЙКИ»,
«ИНФОРМАЦИЯ»), в бою подсказки вида «Не забывайте об автоатаке!».

**Если меню осталось английским:**

* проверить, что папка лежит именно в `atmosphere/contents/{title}/` (не внутри другой
  папки), и что внутри есть `exefs/subsdk9` и `romfs/skyline/plugins/`;
* проверить, что апдейт 2.2.1 установлен, а язык игры — English;
* посмотреть лог Atmosphère: плагин должен упомянуть загрузку
  `rom:/skyline/plugins/libxcnx_file_loader.nro`.

**Удаление:** удалить каталог `atmosphere/contents/{title}` целиком (папку `skyline`
внутри него — тоже, если туда ничего больше не ставилось).

## Эмулятор — режим архива (проверено в Eden)

В Eden/yuzu плагин-лоадер не срабатывает (хук встаёт, подмены нет), поэтому текст
берётся правкой самого архива:

1. Собрать мод-архив из своей копии игры (нужны `prod.keys`, ~10 ГБ свободного места):

   ```
   cd xc3-ru-archive
   python apply_patch.py --ard <mod>/bf3.ard --arh <mod>/bf3.arh --out-arh <mod>/bf3.arh
   ```

   Входной `--arh` — обязательно **текущий** (уже пропатченный) ARH, иначе ранее
   вкатанные записи теряются.
2. Положить `bf3.ard` + `bf3.arh` в `.../contents/{title}/romfs/`. Аргументы `--ard`/`--arh` —
   пути к архиву в этой папке; `bf3.ard` правится на месте, `bf3.arh` перезаписывается.
3. Положить в `.../contents/{title}/exefs/` файлы **апдейта** (`main`, `main.npdm`,
   `rtld`, `sdk`, `subsdk0`).
4. **Отключить апдейт для тайтла** в эмуляторе. Если оставить его включённым,
   патч-romfs апдейта наложится поверх уже слитого архива и часть данных испортится:
   текст при этом остаётся русским, но фон титульного экрана чернеет, а надписи
   `Ver.` / `INFORMATION` пропадают (`work/verify/14-sdf-title.png` против
   `work/verify/17-verify-mode.png`).

## Общее

* Текстовый язык игры должен быть **English** (набор `gb`) — переведён именно он;
  русский язык в игре не выбирается, перевода интерфейса системы это не касается.
* Шрифты `menu/font/*.wifnt` патчены кириллицей, отдельно ставить ничего не нужно.
* Озвучка остаётся оригинальной — перевод текстовый.

## Удаление

Удалить каталог `atmosphere/contents/{title}` (консоль) или папку мода (эмулятор).
"""


def copy_tree(src: str, dst: str, *, skip: tuple[str, ...] = ()) -> int:
    count = 0
    for dirpath, dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        if rel == ".":
            rel = ""
        if rel and rel.split(os.sep)[0] in skip:
            dirs[:] = []
            continue
        for name in files:
            out_dir = os.path.join(dst, rel) if rel else dst
            os.makedirs(out_dir, exist_ok=True)
            shutil.copy2(os.path.join(dirpath, name), os.path.join(out_dir, name))
            count += 1
    return count


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="xlat.dist")
    ap.add_argument("--packed", default="../work/bdat_packed")
    ap.add_argument("--fonts", default="../work/font/patched")
    ap.add_argument("--loader", default="../work/bin/loader_xc3.zip")
    ap.add_argument("--orig-bdat", default="../work/ard_bdat/bdat")
    ap.add_argument("--out", default="../dist")
    ap.add_argument("--title", default="010074F013262000")
    ap.add_argument("--name", default="xc3-ru")
    ap.add_argument("--version", default="", help="версия перевода; по умолчанию — из файла VERSION")
    args = ap.parse_args(argv)

    version = args.version.strip() or read_version()

    emu = os.path.join(args.out, "xc3-ru-emu")
    sd = os.path.join(args.out, "xc3-ru-switch")
    for d in (emu, sd):
        shutil.rmtree(d, ignore_errors=True)
    # эмулятор: Ryujinx-раскладка mods/contents/<title>/<name>/{exefs,romfs}
    emu_base = os.path.join(emu, "mods", "contents", args.title, args.name)
    # консоль: sd:/atmosphere/contents/<title>/{exefs,romfs}
    sd_base = os.path.join(sd, "atmosphere", "contents", args.title)
    roots_romfs = [os.path.join(emu_base, "romfs"), os.path.join(sd_base, "romfs")]

    # 1) romfs: bdat + шрифты
    files = 0
    unknown = []
    for dirpath, _dirs, fnames in os.walk(args.packed):
        for name in fnames:
            if not name.endswith(".bdat"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), args.packed)
            archive_rel = "bdat/" + rel.replace(os.sep, "/")
            if not os.path.isfile(os.path.join(args.orig_bdat, rel.replace(os.sep, "/"))):
                unknown.append(archive_rel)
            for root in roots_romfs:
                dst = os.path.join(root, archive_rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(dirpath, name), dst)
                files += 1

    font_files = []
    for f in FONTS:
        src = os.path.join(args.fonts, f)
        if not os.path.isfile(src):
            continue
        font_files.append(f)
        for root in roots_romfs:
            dst = os.path.join(root, "menu", "font", f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            files += 1

    # 2) лоадер (exefs + romfs/skyline)
    loader_files = 0
    if os.path.isfile(args.loader):
        with zipfile.ZipFile(args.loader) as z:
            seen = set()
            for member in z.namelist():
                if member.endswith("/"):
                    continue
                # в релизе пути вида atmosphere/contents/<title>/<exefs|romfs>/...
                parts = member.split("/")
                if len(parts) < 5 or parts[0] != "atmosphere" or member in seen:
                    continue
                seen.add(member)
                title = parts[2]
                inner = "/".join(parts[3:])
                for root in (emu_base, sd_base):
                    dst = os.path.join(root, inner.replace("/", os.sep))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with z.open(member) as fsrc, open(dst, "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst)
                    loader_files += 1

    # 3) инструкция и версия внутри пакетов
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "INSTALL.md"), "w", encoding="utf-8") as f:
        f.write(
            INSTALL_MD.replace("{title}", args.title)
            .replace("{version}", version)
            .replace("{date}", time.strftime("%d.%m.%Y"))
        )
    write_version_txt(os.path.join(emu_base, "version.txt"), version, "xc3-ru-emu (Ryujinx)")
    write_version_txt(os.path.join(sd_base, "version.txt"), version, "xc3-ru-switch (Atmosphère)")

    # 4) zip
    def make_zip(src_dir: str, zip_path: str) -> int:
        n = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for dirpath, _dirs, fnames in os.walk(src_dir):
                for name in fnames:
                    full = os.path.join(dirpath, name)
                    z.write(full, os.path.relpath(full, src_dir))
                    n += 1
        return n

    # 2b) пакет для режима архива: только наши файлы + сборщик
    arch = os.path.join(args.out, "xc3-ru-archive")
    shutil.rmtree(arch, ignore_errors=True)
    patch_root = os.path.join(arch, "bf3_patch")
    arch_files = 0
    for dirpath, _dirs, fnames in os.walk(args.packed):
        for name in fnames:
            if not name.endswith(".bdat"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), args.packed)
            dst = os.path.join(patch_root, "bdat", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(dirpath, name), dst)
            arch_files += 1
    for f in FONTS:
        src = os.path.join(args.fonts, f)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(patch_root, "menu", "font", f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        arch_files += 1
    for extra in ("archive_mod.py",):
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), extra)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(arch, "apply_patch.py"))
    ardpak = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "ardpack", "target", "release", "ardpack.exe")
    if os.path.isfile(ardpak):
        shutil.copy2(ardpak, os.path.join(arch, "ardpack.exe"))
    write_version_txt(os.path.join(arch, "version.txt"), version, "xc3-ru-archive (режим архива)")
    print(f"пакет режима архива: {arch_files} файлов + apply_patch.py + ardpak.exe")

    zip_emu = os.path.join(args.out, f"xc3-ru-emu-v{version}.zip")
    zip_sd = os.path.join(args.out, f"xc3-ru-switch-v{version}.zip")
    n_emu = make_zip(emu, zip_emu)
    n_sd = make_zip(sd, zip_sd)
    if os.path.isdir(arch):
        n_arch = make_zip(arch, os.path.join(args.out, f"xc3-ru-archive-v{version}.zip"))
        print(f"zip: archive {n_arch} файлов")

    print(f"bdat+шрифты: {files} файлов (шрифтов: {len(font_files)}: {', '.join(font_files)})")
    print(f"лоадер: {loader_files} файлов")
    print(f"версия перевода: {version}")
    print(f"zip: emu {n_emu} файлов ({os.path.basename(zip_emu)}), "
          f"switch {n_sd} файлов ({os.path.basename(zip_sd)})")
    if unknown:
        print(f"ВНИМАНИЕ: {len(unknown)} путей нет в исходном архиве (первые 5): {unknown[:5]}")
    print(f"готово: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
