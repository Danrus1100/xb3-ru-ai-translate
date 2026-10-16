//! Аудит и сборка кириллических шрифтов Xenoblade Chronicles 3 (`.wifnt`, формат LAFT).
//!
//! `audit` — проверяет, какие codepoint'ы реально доступны через `Laft::get_glyph`,
//! печатает метрики, формат текстуры и отсутствующие символы.
//! `build` — добавляет отсутствующие символы (по умолчанию кириллицу) в существующий
//! атлас, пересобирает файл и проверяет результат через `get_glyph`.

use ab_glyph::Font;
use anyhow::{anyhow, Context, Result};
use binrw::BinRead;
use clap::{Parser, Subcommand};
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use xc3_lib::laft::{GlyphFontInfo, Laft};
use xc3_lib::xbc1::MaybeXbc1;

mod render;
use xc3_write::WriteFull;

/// Обязательные символы: (начало, конец, метка)
const REQUIRED: &[(u32, u32, &str)] = &[
    (0x0020, 0x007E, "ASCII"),
    (0x0410, 0x044F, "А-я"),
    (0x0401, 0x0401, "Ё"),
    (0x0451, 0x0451, "ё"),
    (0x00AB, 0x00AB, "«"),
    (0x00BB, 0x00BB, "»"),
    (0x2014, 0x2014, "—"),
    (0x2026, 0x2026, "…"),
    (0x2019, 0x2019, "’"),
    (0x00A0, 0x00A0, "nbsp"),
];

fn required_codepoints() -> Vec<u32> {
    let mut v = Vec::new();
    for (a, b, _label) in REQUIRED {
        v.extend(*a..=*b);
    }
    v
}

fn load_laft(path: &Path) -> Result<Laft> {
    let mut reader = BufReader::new(File::open(path)?);
    let file = MaybeXbc1::<Laft>::read_le(&mut reader)
        .with_context(|| format!("чтение LAFT из {}", path.display()))?;
    match file {
        MaybeXbc1::Uncompressed(l) => Ok(l),
        MaybeXbc1::Xbc1(x) => x
            .extract::<Laft>()
            .map_err(|e| anyhow!("распаковка xbc1: {e:?}")),
    }
}

fn write_laft(path: &Path, laft: &Laft) -> Result<()> {
    let mut writer = BufWriter::new(File::create(path)?);
    laft.write_full(&mut writer, 0, &mut 0, xc3_write::Endian::Little, ())
        .map_err(|e| anyhow!("запись LAFT: {e:?}"))?;
    Ok(())
}

#[derive(Parser)]
#[command(author, version, about = "XC3 .wifnt (LAFT): аудит и сборка кириллицы")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Метрики шрифта и отсутствующие обязательные символы
    Audit {
        files: Vec<PathBuf>,
        #[arg(long)]
        verbose: bool,
    },
    /// Добавить отсутствующие символы
    Build {
        src: PathBuf,
        /// TTF, из которого берутся глифы (можно подсунуть любой свой, напр. SemiBold)
        ttf: PathBuf,
        out: PathBuf,
        /// Утолщение глифов на N пикселей (подгонка под вес игрового шрифта)
        #[arg(long, default_value_t = 0.0)]
        dilate: f32,
        /// Утолщение ТОЛЬКО по горизонтали: толстеют вертикальные стойки, тонкие
        /// горизонтали остаются тонкими — вес растёт без потери контраста
        #[arg(long)]
        dilate_x: Option<f32>,
        /// Утолщение только по вертикали
        #[arg(long)]
        dilate_y: Option<f32>,
        /// Растянуть буквы по горизонтали (1.0 — как есть, 1.3 — на треть шире)
        #[arg(long, default_value_t = 1.0)]
        stretch_x: f32,
        /// Смещение порога SDF: плавная подстройка веса (30 единиц = 1 пиксель).
        /// Минус — тоньше, плюс — жирнее.
        #[arg(long, default_value_t = 0.0)]
        sdf_bias: f32,
        /// Сдвиг глифов по вертикали, пиксели (положительный — вниз)
        #[arg(long, default_value_t = 0)]
        y_shift: i32,
        /// Сдвиг глифов по горизонтали, пиксели
        #[arg(long, default_value_t = 0)]
        x_shift: i32,
        /// Яркость новых глифов: auto = под пик латинских глифов, либо 1..255
        #[arg(long, default_value = "auto")]
        peak: String,
        /// Формат атласа: auto = SDF с наклоном из игровых глифов (по умолчанию),
        /// off = покрытие, либо число — наклон (единиц на пиксель)
        #[arg(long, default_value = "auto")]
        sdf: String,
        /// Кегль по высоте прописной; без флага — замер по телу глифа 'H'
        #[arg(long)]
        cap_h: Option<usize>,
        /// Минимальный левый отступ буквы внутри её шага (у игрового шрифта ~7-8 px)
        #[arg(long, default_value_t = 0)]
        min_bearing: i32,
        /// Кодовые точки, которые надо перерисовать даже если они уже есть в шрифте
        #[arg(long)]
        force_glyphs: Option<String>,
    },
    /// Влить поправленные пиксели из PNG (весь атлас или одна ячейка)
    Import {
        src: PathBuf,
        /// PNG: либо весь атлас (те же размеры), либо одна ячейка (кратная ей, напр. x6)
        png: PathBuf,
        out: PathBuf,
        /// Какую ячейку вливать (например U+0410); без флага PNG должен совпадать с атласом
        #[arg(long)]
        glyph: Option<String>,
        /// PNG — это покрытие (0..255), сконвертировать в SDF: auto (наклон из игровых глифов) или число
        #[arg(long)]
        coverage_to_sdf: Option<String>,
    },
    /// Нарезать шрифт по глифам в PNG (для ручной правки в редакторе)
    Split {
        src: PathBuf,
        out: PathBuf,
        /// Увеличение при выгрузке (глиф видно крупнее и его можно дорисовать)
        #[arg(long, default_value_t = 6)]
        scale: u32,
        /// Кодовые точки через запятую; по умолчанию ASCII + А-я + обязательные знаки
        #[arg(long)]
        glyphs: Option<String>,
    },
    /// Отрисовать строку глифами шрифта — предпросмотр того, что увидит игрок
    Preview {
        src: PathBuf,
        out: PathBuf,
        /// Текст; можно несколько строк через \n
        #[arg(long, default_value = "Съешь ещё этих мягких французских булок, да выпей чаю")]
        text: String,
        #[arg(long, default_value_t = 2)]
        scale: u32,
        /// Показать рамки окон сэмплирования (left_x..left_x+width)
        #[arg(long)]
        boxes: bool,
    },
    /// Метрики глифов: left_x/width против фактических чернил в ячейке
    Metrics {
        src: PathBuf,
        /// Кодовые точки, например U+0048,U+0410 (по умолчанию ASCII + А-я)
        #[arg(long)]
        glyphs: Option<String>,
        /// Порог «чернил» для SDF (значения выше порога считаются телом глифа)
        #[arg(long, default_value_t = 160u8)]
        threshold: u8,
    },
    /// Выгрузить атлас (или одну ячейку) в PNG — посмотреть/дорисовать глифы
    Dump {
        src: PathBuf,
        out: PathBuf,
        /// Ячейка конкретного символа, например U+0410 (иначе весь атлас)
        #[arg(long)]
        glyph: Option<String>,
    },
}

fn main() -> ExitCode {
    match Cli::parse().cmd {
        Cmd::Split { src, out, scale, glyphs } => {
            match split(&src, &out, scale, glyphs.as_deref()) {
                Ok(n) => {
                    println!("выгружено глифов: {n} -> {}", out.display());
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("ошибка: {e:?}");
                    ExitCode::FAILURE
                }
            }
        }
        Cmd::Preview { src, out, text, scale, boxes } => {
            match preview(&src, &out, &text, scale, boxes) {
                Ok(n) => {
                    println!("предпросмотр: {n} глифов -> {}", out.display());
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("ошибка: {e:?}");
                    ExitCode::FAILURE
                }
            }
        }
        Cmd::Metrics { src, glyphs, threshold } => {
            match metrics(&src, glyphs.as_deref(), threshold) {
                Ok(n) => {
                    println!("глифов измерено: {n}");
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("ошибка: {e:?}");
                    ExitCode::FAILURE
                }
            }
        }
        Cmd::Audit { files, verbose } => {
            // PowerShell не раскрывает маски: принимаем каталоги и шаблоны вида *.wifnt сами
            let files = expand_paths(&files);
            if files.is_empty() {
                eprintln!("не найдено ни одного .wifnt (передайте файл, каталог или маску)");
                return ExitCode::FAILURE;
            }
            let mut missing = 0usize;
            let mut failed = 0usize;
            for f in &files {
                match audit_one(f, verbose) {
                    Ok(miss) => missing += miss,
                    Err(e) => {
                        failed += 1;
                        eprintln!("ОШИБКА {}: {e:#}", f.display());
                    }
                }
            }
            println!(
                "\nфайлов: {}, ошибок: {}, отсутствующих символов: {}",
                files.len(),
                failed,
                missing
            );
            if failed > 0 {
                ExitCode::FAILURE
            } else {
                ExitCode::SUCCESS
            }
        }
        Cmd::Build {
            src,
            ttf,
            out,
            dilate,
            y_shift,
            x_shift,
            peak,
            sdf,
            cap_h,
            min_bearing,
            force_glyphs,
            dilate_x,
            dilate_y,
            stretch_x,
            sdf_bias,
        } => match build(
            &src, &ttf, &out, dilate, dilate_x, dilate_y, y_shift, x_shift, &peak, &sdf, cap_h,
            min_bearing, force_glyphs.as_deref(), stretch_x, sdf_bias,
        ) {
            Ok(()) => {
                println!("готово: {}", out.display());
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("ОШИБКА: {e:#}");
                ExitCode::FAILURE
            }
        },
        Cmd::Import {
            src,
            png,
            out,
            glyph,
            coverage_to_sdf,
        } => match import_png(&src, &png, &out, glyph.as_deref(), coverage_to_sdf.as_deref()) {
            Ok(()) => {
                println!("готово: {}", out.display());
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("ОШИБКА: {e:#}");
                ExitCode::FAILURE
            }
        },
        Cmd::Dump { src, out, glyph } => match dump(&src, &out, glyph.as_deref()) {
            Ok(()) => {
                println!("готово: {}", out.display());
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("ОШИБКА: {e:#}");
                ExitCode::FAILURE
            }
        },
    }
}

/// Влить пиксели из PNG: либо весь атлас (размеры совпадают), либо одна ячейка
/// (PNG кратен ячейке — удобно править ячейку, выгруженную `dump --glyph`).
fn import_png(
    src: &Path,
    png: &Path,
    out: &Path,
    glyph: Option<&str>,
    coverage_to_sdf: Option<&str>,
) -> Result<()> {
    let mut laft = load_laft(src)?;
    let tex = laft
        .texture
        .take()
        .ok_or_else(|| anyhow!("в шрифте нет текстуры"))?;
    let mut surface = tex.to_surface().map_err(|e| anyhow!("deswizzle: {e:?}"))?;
    let w = surface.width as usize;
    let h = surface.height as usize;
    let bpp = surface.data.len() / (w * h);
    let s = &laft.settings;
    let cell_w = s.glyph_area_width as usize + 1;
    let cell_h = s.glyph_area_height as usize + 1;
    let per_row = s.glyphs_per_row as usize;

    let img = image::open(png)
        .map_err(|e| anyhow!("чтение PNG {}: {e}", png.display()))?
        .to_luma8();
    let (iw, ih) = (img.width() as usize, img.height() as usize);

    let write_pixel = |surface: &mut image_dds::Surface<Vec<u8>>, x: usize, y: usize, v: u8| {
        let idx = (y * w + x) * bpp;
        for k in 0..bpp {
            surface.data[idx + k] = v;
        }
    };

    match glyph {
        None => {
            if coverage_to_sdf.is_some() {
                return Err(anyhow!(
                    "--coverage-to-sdf работает только с --glyph (одна ячейка)"
                ));
            }
            if iw != w || ih != h {
                return Err(anyhow!(
                    "PNG {iw}x{ih} не совпадает с атласом {w}x{h} (для одной ячейки используйте --glyph)"
                ));
            }
            for y in 0..h {
                for x in 0..w {
                    let v = img.get_pixel(x as u32, y as u32)[0];
                    write_pixel(&mut surface, x, y, v);
                }
            }
            println!("атлас {w}x{h} обновлён целиком");
        }
        Some(g) => {
            let cp = u32::from_str_radix(g.trim_start_matches("U+").trim_start_matches("0x"), 16)
                .map_err(|_| anyhow!("не понял codepoint {g}"))?;
            let (grid, _) =
                laft.get_glyph(cp as u16).ok_or_else(|| anyhow!("в шрифте нет U+{cp:04X}"))?;
            if iw % cell_w != 0 || ih % cell_h != 0 {
                return Err(anyhow!("PNG {iw}x{ih} не кратен ячейке {cell_w}x{cell_h}"));
            }
            let kx = iw / cell_w;
            let ky = ih / cell_h;
            if kx != ky {
                return Err(anyhow!("масштаб по осям разный ({kx} и {ky})"));
            }
            // покрытие ячейки из PNG
            let mut cov = vec![0u8; cell_w * cell_h];
            for y in 0..cell_h {
                for x in 0..cell_w {
                    cov[y * cell_w + x] = img.get_pixel((x * kx) as u32, (y * ky) as u32)[0];
                }
            }
            // при необходимости переводим покрытие в SDF (край на 128)
            let (buf, bw, bh, pad) = match coverage_to_sdf {
                Some(arg) => {
                    let slope = match arg {
                        "auto" => measure_sdf_slope(
                            &surface.data, w, per_row, cell_w, cell_h, bpp, &laft,
                            &[0x48, 0x41, 0x6F],
                        )
                        .unwrap_or(30.0),
                        other => other.parse::<f32>().map_err(|_| {
                            anyhow!("--coverage-to-sdf: ожидалось auto|число, получено {other}")
                        })?,
                    };
                    let pad = ((128.0 / slope).ceil() as usize) + 1;
                    let (b, bw, bh) = sdf_from_coverage(&cov, cell_w, cell_h, pad, slope);
                    println!(
                        "  покрытие → SDF (наклон {slope:.1}, запас {pad} px): {cell_w}x{cell_h} → {bw}x{bh}"
                    );
                    (b, bw, bh, pad)
                }
                None => (cov.clone(), cell_w, cell_h, 0),
            };
            let cx = (grid % per_row) * cell_w;
            let cy = (grid / per_row) * cell_h;
            // вставляем «ядро» буфера, соответствующее исходной ячейке
            let mut max = 0u8;
            for y in 0..cell_h {
                for x in 0..cell_w {
                    let v = buf[(y + pad) * bw + (x + pad)];
                    max = max.max(v);
                    if cx + x < w && cy + y < h {
                        write_pixel(&mut surface, cx + x, cy + y, v);
                    }
                }
            }
            println!(
                "ячейка U+{cp:04X} (grid {grid}, x{cx},y{cy}) обновлена, максимум {max}, масштаб x{kx}"
            );
        }
    }

    let new_tex =
        xc3_lib::mibl::Mibl::from_surface(surface).map_err(|e| anyhow!("swizzle обратно: {e:?}"))?;
    laft.texture = Some(new_tex);
    write_laft(out, &laft)?;
    Ok(())
}

/// Кодовые точки по умолчанию: ASCII, кириллица, обязательные знаки.
fn default_codes() -> Vec<u16> {
    let mut v: Vec<u16> = (0x20u16..=0x7E).collect();
    v.extend(0x410..=0x44F);
    v.extend([0x401, 0x451, 0x00AB, 0x00BB, 0x2014, 0x2026, 0x2019, 0x00A0]);
    v
}

fn parse_codes(glyphs: Option<&str>) -> Vec<u16> {
    match glyphs {
        Some(list) => list
            .split(',')
            .filter_map(|t| {
                u16::from_str_radix(t.trim().trim_start_matches("U+").trim_start_matches("u+"), 16).ok()
            })
            .collect(),
        None => default_codes(),
    }
}

/// Нарезает шрифт в каталог PNG: `<U+XXXX>.png` (ячейка, увеличенная) + `metrics.tsv`.
fn split(src: &Path, out: &Path, scale: u32, glyphs: Option<&str>) -> Result<usize> {
    let laft = load_laft(src)?;
    let tex = laft.texture.as_ref().ok_or_else(|| anyhow!("в шрифте нет текстуры"))?;
    let surface = tex.to_surface().map_err(|e| anyhow!("deswizzle: {e:?}"))?;
    let (w, h) = (surface.width as usize, surface.height as usize);
    let bpp = surface.data.len() / (w * h);
    let st = &laft.settings;
    let cell_w = st.glyph_area_width as usize + 1;
    let cell_h = st.glyph_area_height as usize + 1;
    let per_row = st.glyphs_per_row as usize;
    let sc = scale.max(1) as usize;
    std::fs::create_dir_all(out)?;
    let mut map = String::from("codepoint\tchar\tleft_x\twidth\tadvance\tink_left\tink_right\tink_w\n");
    let mut n = 0usize;
    for cp in parse_codes(glyphs) {
        let Some((grid, info)) = laft.get_glyph(cp) else { continue };
        let cx = (grid % per_row) * cell_w;
        let cy = (grid / per_row) * cell_h;
        let (mut left, mut right) = (usize::MAX, 0usize);
        let mut img = image::GrayImage::new((cell_w * sc) as u32, (cell_h * sc) as u32);
        for y in 0..cell_h {
            for x in 0..cell_w {
                let idx = ((cy + y) * w + (cx + x)) * bpp;
                let v = surface.data[idx];
                if v > 160 {
                    left = left.min(x);
                    right = right.max(x);
                }
                for sy in 0..sc {
                    for sx in 0..sc {
                        img.put_pixel(((x * sc + sx) as u32), ((y * sc + sy) as u32), image::Luma([v]));
                    }
                }
            }
        }
        img.save(out.join(format!("U+{cp:04X}.png")))?;
        let ch = char::from_u32(cp as u32).unwrap_or('?');
        let (il, ir) = if left == usize::MAX { (0, 0) } else { (left, right) };
        map.push_str(&format!(
            "U+{cp:04X}\t{ch}\t{}\t{}\t{}\t{il}\t{ir}\t{}\n",
            info.left_x,
            info.width,
            info.left_x as usize + info.width as usize,
            if ir >= il { ir - il + 1 } else { 0 }
        ));
        n += 1;
    }
    std::fs::write(out.join("metrics.tsv"), map)?;
    Ok(n)
}

/// Рисует строки глифами шрифта так, как их разложит игра (см. `render::Font`).
fn preview(src: &Path, out: &Path, text: &str, scale: u32, boxes: bool) -> Result<usize> {
    let font = render::Font::load(src)?;
    let img = font.render(text, scale, boxes);
    img.save(out)?;
    let missing = font.missing(text);
    if !missing.is_empty() {
        println!("  нет глифов: {}", missing.iter().collect::<String>());
    }
    Ok(text.replace("\\n", "\n").chars().filter(|c| *c != '\n').count())
}

/// Печатает left_x/width из таблицы и фактические границы чернил внутри ячейки.
fn metrics(src: &Path, glyphs: Option<&str>, threshold: u8) -> Result<usize> {
    let laft = load_laft(src)?;
    let tex = laft.texture.as_ref().ok_or_else(|| anyhow!("в шрифте нет текстуры"))?;
    let surface = tex.to_surface().map_err(|e| anyhow!("deswizzle: {e:?}"))?;
    let (w, h) = (surface.width as usize, surface.height as usize);
    let bpp = surface.data.len() / (w * h);
    let s = &laft.settings;
    let cell_w = s.glyph_area_width as usize + 1;
    let cell_h = s.glyph_area_height as usize + 1;
    let per_row = s.glyphs_per_row as usize;

    let codes: Vec<u16> = match glyphs {
        Some(list) => list
            .split(',')
            .filter_map(|t| u16::from_str_radix(t.trim().trim_start_matches("U+").trim_start_matches("u+"), 16).ok())
            .collect(),
        None => (0x20u16..=0x7E).chain(0x410..=0x44F).chain([0x401, 0x451]).collect(),
    };
    println!("U+XXXX  left_x width  ink_left ink_right  ink_w  advance(left_x+width)  glyph_w");
    let mut n = 0usize;
    for cp in codes {
        let Some((grid, info)) = laft.get_glyph(cp) else { continue };
        let cx = (grid % per_row) * cell_w;
        let cy = (grid / per_row) * cell_h;
        let (mut left, mut right) = (usize::MAX, 0usize);
        for y in 0..cell_h {
            for x in 0..cell_w {
                let idx = ((cy + y) * w + (cx + x)) * bpp;
                if surface.data[idx] > threshold {
                    left = left.min(x);
                    right = right.max(x);
                }
            }
        }
        if left == usize::MAX {
            continue;
        }
        println!(
            "U+{cp:04X}  {:6} {:5}  {:8} {:9}  {:5}  {:8}  {}",
            info.left_x,
            info.width,
            left,
            right,
            right - left + 1,
            info.left_x as usize + info.width as usize,
            char::from_u32(cp as u32).unwrap_or('?')
        );
        n += 1;
    }
    Ok(n)
}

/// Сохранить атлас (или ячейку глифа, увеличенную) в PNG.
fn dump(src: &Path, out: &Path, glyph: Option<&str>) -> Result<()> {
    let laft = load_laft(src)?;
    let tex = laft
        .texture
        .as_ref()
        .ok_or_else(|| anyhow!("в шрифте нет текстуры"))?;
    let surface = tex.to_surface().map_err(|e| anyhow!("deswizzle: {e:?}"))?;
    let w = surface.width as usize;
    let h = surface.height as usize;
    let bpp = surface.data.len() / (w * h);
    let s = &laft.settings;
    let cell_w = s.glyph_area_width as usize + 1;
    let cell_h = s.glyph_area_height as usize + 1;
    let per_row = s.glyphs_per_row as usize;

    let (x0, y0, cw, ch, scale) = match glyph {
        Some(g) => {
            let cp = u32::from_str_radix(g.trim_start_matches("U+").trim_start_matches("0x"), 16)
                .map_err(|_| anyhow!("не понял codepoint {g}"))?;
            let (grid, _) = laft
                .get_glyph(cp as u16)
                .ok_or_else(|| anyhow!("в шрифте нет U+{cp:04X}"))?;
            (
                (grid % per_row) * cell_w,
                (grid / per_row) * cell_h,
                cell_w,
                cell_h,
                6,
            )
        }
        None => (0, 0, w, h, 1),
    };

    let mut img = image::GrayImage::new((cw * scale) as u32, (ch * scale) as u32);
    for y in 0..ch {
        for x in 0..cw {
            let idx = ((y0 + y) * w + (x0 + x)) * bpp;
            let v = surface.data[idx];
            for dy in 0..scale {
                for dx in 0..scale {
                    img.put_pixel(
                        ((x * scale + dx) as u32), ((y * scale + dy) as u32),
                        image::Luma([v]),
                    );
                }
            }
        }
    }
    img.save(out).map_err(|e| anyhow!("запись PNG: {e}"))?;
    Ok(())
}

/// Раскрывает аргументы: каталоги → все `*.wifnt` внутри, строки с `*`/`?` → сопоставление в каталоге.
fn expand_paths(args: &[PathBuf]) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for a in args {
        let text = a.to_string_lossy().to_string();
        if a.is_dir() {
            if let Ok(rd) = std::fs::read_dir(a) {
                for e in rd.flatten() {
                    let p = e.path();
                    if p.extension().map(|x| x.eq_ignore_ascii_case("wifnt")).unwrap_or(false) {
                        out.push(p);
                    }
                }
            }
            continue;
        }
        if text.contains('*') || text.contains('?') {
            let (dir, pat) = match text.rfind(['/', '\\']) {
                Some(i) => {
                    let (d, p) = text.split_at(i);
                    (if d.is_empty() { ".".to_string() } else { d.to_string() }, p[1..].to_string())
                }
                None => (".".to_string(), text.clone()),
            };
            // regex_lite — это glob-матчер: передаём шаблон как есть
            let re = regex_lite(&pat);
            if let Ok(rd) = std::fs::read_dir(&dir) {
                for e in rd.flatten() {
                    let name = e.file_name().to_string_lossy().to_string();
                    if re(&name) {
                        out.push(e.path());
                    }
                }
            }
            continue;
        }
        out.push(a.clone());
    }
    out.sort();
    out
}

/// Минимальный матчер без внешних зависимостей (шаблон уже переведён в «regex-подобный» вид).
fn regex_lite(pattern: &str) -> impl Fn(&str) -> bool + '_ {
    move |s: &str| {
        let (p, t): (Vec<char>, Vec<char>) = (pattern.chars().collect(), s.chars().collect());
        let (mut pi, mut ti, mut star, mut mark) = (0usize, 0usize, None::<usize>, 0usize);
        while ti < t.len() {
            if pi < p.len() && (p[pi] == '?' || p[pi] == t[ti]) {
                pi += 1;
                ti += 1;
            } else if pi < p.len() && p[pi] == '*' {
                star = Some(pi);
                mark = ti;
                pi += 1;
            } else if let Some(sp) = star {
                pi = sp + 1;
                mark += 1;
                ti = mark;
            } else {
                return false;
            }
        }
        while pi < p.len() && p[pi] == '*' {
            pi += 1;
        }
        pi == p.len()
    }
}

fn audit_one(path: &Path, verbose: bool) -> Result<usize> {
    let laft = load_laft(path)?;
    println!("=== {}", path.display());
    println!(
        "  глифов {}  width_reduction {}  line_height {}",
        laft.font_info.len(),
        laft.global_width_reduction,
        laft.line_height
    );
    let s = &laft.settings;
    println!(
        "  текстура {}x{}  ячейка {}x{}  в строке {}  строк {}",
        s.texture_width,
        s.texture_height,
        s.glyph_area_width as u32 + 1,
        s.glyph_area_height as u32 + 1,
        s.glyphs_per_row,
        s.num_rows
    );
    match &laft.texture {
        Some(t) => println!(
            "  формат {:?}  {}x{}  data {} байт  mips {}",
            t.footer.image_format,
            t.footer.width,
            t.footer.height,
            t.image_data.len(),
            t.footer.mipmap_count
        ),
        None => println!("  текстуры нет"),
    }
    let mut total = 0usize;
    for (a, b, label) in REQUIRED {
        let miss: Vec<u32> = (*a..=*b)
            .filter(|cp| laft.get_glyph(*cp as u16).is_none())
            .collect();
        if miss.is_empty() {
            println!("  {label}: OK");
        } else {
            println!("  {label}: отсутствует {}", miss.len());
            if verbose {
                for cp in &miss {
                    println!("      U+{cp:04X}");
                }
            }
        }
        total += miss.len();
    }
    Ok(total)
}

/// Метрики чернил глифа внутри его ячейки: (высота, левая колонка, правая колонка, нижняя строка).
fn cell_ink_metrics(
    data: &[u8],
    width: usize,
    per_row: usize,
    cell_w: usize,
    cell_h: usize,
    grid: usize,
    bpp: usize,
    threshold: u8,
) -> Result<(usize, usize, usize, usize)> {
    let cx = (grid % per_row) * cell_w;
    let cy = (grid / per_row) * cell_h;
    let (mut left, mut right, mut top, mut bottom) = (usize::MAX, 0usize, usize::MAX, 0usize);
    for y in 0..cell_h {
        for x in 0..cell_w {
            let idx = ((cy + y) * width + (cx + x)) * bpp;
            // SDF: рампа вокруг глифа занимает несколько пикселей, поэтому «любой
            // ненулевой» завышает чернила. Тело глифа — значения выше порога.
            let solid = data[idx..idx + bpp].iter().any(|b| *b > threshold);
            if solid {
                left = left.min(x);
                right = right.max(x);
                top = top.min(y);
                bottom = bottom.max(y);
            }
        }
    }
    if left == usize::MAX {
        return Err(anyhow!("ячейка {grid} пустая"));
    }
    Ok((bottom - top + 1, left, right, bottom))
}

/// Наклон SDF у игровых глифов: сколько единиц яркости приходится на пиксель
/// (атлас хранит знаковое поле расстояний: край глифа ≈128, шаг ~29/px).
fn measure_sdf_slope(
    data: &[u8],
    width: usize,
    per_row: usize,
    cell_w: usize,
    cell_h: usize,
    bpp: usize,
    laft: &Laft,
    codepoints: &[u32],
) -> Option<f32> {
    let mut slopes = Vec::new();
    for cp in codepoints {
        if let Some((grid, _)) = laft.get_glyph(*cp as u16) {
            let cx = (grid % per_row) * cell_w;
            let cy = (grid / per_row) * cell_h;
            for y in 1..cell_h - 1 {
                let mut best = 0i32;
                for x in 1..cell_w - 1 {
                    let a = data[((cy + y) * width + (cx + x - 1)) * bpp] as i32;
                    let b = data[((cy + y) * width + (cx + x)) * bpp] as i32;
                    if (a == 0 && b == 0) || (a > 200 && b > 200) {
                        continue;
                    }
                    best = best.max((b - a).abs());
                }
                if best > 5 {
                    slopes.push(best as f32);
                }
            }
        }
    }
    if slopes.is_empty() {
        return None;
    }
    slopes.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Some(slopes[slopes.len() / 2]) // медиана: устойчива к выбросам хинтинга
}

/// 1D-преобразование расстояний (Felzenszwalb & Huttenlocher, квадраты расстояний).
fn edt_1d(f: &[f64]) -> Vec<f64> {
    let n = f.len();
    let mut d = vec![0.0f64; n];
    let mut v = vec![0usize; n];
    let mut z = vec![0.0f64; n + 1];
    let mut k = 0usize;
    v[0] = 0;
    z[0] = f64::NEG_INFINITY;
    z[1] = f64::INFINITY;
    for q in 1..n {
        let mut s = ((f[q] + (q * q) as f64) - (f[v[k]] + (v[k] * v[k]) as f64))
            / (2.0 * (q as f64 - v[k] as f64));
        while s <= z[k] && k > 0 {
            k -= 1;
            s = ((f[q] + (q * q) as f64) - (f[v[k]] + (v[k] * v[k]) as f64))
                / (2.0 * (q as f64 - v[k] as f64));
        }
        k += 1;
        v[k] = q;
        z[k] = s;
        z[k + 1] = f64::INFINITY;
    }
    k = 0;
    for q in 0..n {
        while z[k + 1] < q as f64 {
            k += 1;
        }
        let dq = q as f64 - v[k] as f64;
        d[q] = dq * dq + f[v[k]];
    }
    d
}

/// Евклидово расстояние (в пикселях) до ближайшего пикселя, где `mask == target`.
fn edt(mask: &[u8], w: usize, h: usize, target: u8) -> Vec<f32> {
    // ВАЖНО: не INFINITY — арифметика парабол с бесконечностью даёт NaN,
    // и весь SDF уходит в ноль. Берём большой конечный сентинел.
    let big = (w * h) as f64 + 1.0;
    let mut f: Vec<f64> = mask
        .iter()
        .map(|&m| if m == target { 0.0 } else { big })
        .collect();
    for x in 0..w {
        let col: Vec<f64> = (0..h).map(|y| f[y * w + x]).collect();
        let d = edt_1d(&col);
        for y in 0..h {
            f[y * w + x] = d[y];
        }
    }
    for y in 0..h {
        let row: Vec<f64> = (0..w).map(|x| f[y * w + x]).collect();
        let d = edt_1d(&row);
        for x in 0..w {
            f[y * w + x] = d[x];
        }
    }
    f.iter().map(|v| v.sqrt() as f32).collect()
}

/// Покрытие (0..255) → SDF в байтах: `value = 128 + d * slope`, d — знаковое расстояние в px.
/// `pad` — сколько пикселей поля нужно вокруг, чтобы рампа уместилась.
fn sdf_from_coverage(cov: &[u8], w: usize, h: usize, pad: usize, slope: f32) -> (Vec<u8>, usize, usize) {
    let (w2, h2) = (w + 2 * pad, h + 2 * pad);
    let mut padded = vec![0u8; w2 * h2];
    for y in 0..h {
        for x in 0..w {
            padded[(y + pad) * w2 + (x + pad)] = cov[y * w + x];
        }
    }
    let mask: Vec<u8> = padded.iter().map(|&v| if v >= 128 { 1 } else { 0 }).collect();
    let din = edt(&mask, w2, h2, 0); // расстояние до «снаружи»
    let outside: Vec<u8> = mask.iter().map(|&v| 1 - v).collect();
    let dout = edt(&outside, w2, h2, 0); // расстояние до «внутри»
    let out = (0..w2 * h2)
        .map(|i| {
            let d = din[i] - dout[i]; // >0 внутри, <0 снаружи
            (128.0 + d * slope).round().clamp(0.0, 255.0) as u8
        })
        .collect();
    (out, w2, h2)
}

/// SDF с суперсэмплингом: расстояние считается на растре, увеличенном в `ss` раз, и
/// усредняется обратно. Без этого дистанции квантуются целыми пикселями, в глифе остаётся
/// ~15 различимых уровней вместо сотни, и кириллица выглядит не сглаженной рядом с латиницей.
fn sdf_from_coverage_ss(
    cov: &[u8],
    w: usize,
    h: usize,
    pad: usize,
    slope: f32,
    ss: usize,
) -> (Vec<u8>, usize, usize) {
    sdf_from_coverage_ss_bias(cov, w, h, pad, slope, ss, 0.0)
}

/// `bias` сдвигает порог: отрицательный — штрих тоньше, положительный — жирнее.
/// 30 единиц ≈ 1 пиксель, поэтому шаг подстройки получается плавным.
fn sdf_from_coverage_ss_bias(
    cov: &[u8],
    w: usize,
    h: usize,
    pad: usize,
    slope: f32,
    ss: usize,
    bias: f32,
) -> (Vec<u8>, usize, usize) {
    let (w2, h2) = (w + 2 * pad, h + 2 * pad);
    let mut padded = vec![0u8; w2 * h2];
    for y in 0..h {
        for x in 0..w {
            padded[(y + pad) * w2 + (x + pad)] = cov[y * w + x];
        }
    }
    let mask: Vec<u8> = padded.iter().map(|&v| if v >= 128 { 1 } else { 0 }).collect();
    let din = edt(&mask, w2, h2, 0);
    let outside: Vec<u8> = mask.iter().map(|&v| 1 - v).collect();
    let dout = edt(&outside, w2, h2, 0);

    // уменьшаем обратно: усредняем блок ss×ss значений SDF
    let (bw, bh) = (w2 / ss, h2 / ss);
    let mut out = vec![0u8; bw * bh];
    for by in 0..bh {
        for bx in 0..bw {
            let mut acc = 0f32;
            let mut n = 0f32;
            for dy in 0..ss {
                for dx in 0..ss {
                    let i = (by * ss + dy) * w2 + (bx * ss + dx);
                    // расстояние в пикселях базового масштаба
                    let d = (din[i] as f32 - dout[i] as f32) / ss as f32;
                    acc += 128.0 + bias + d * slope;
                    n += 1.0;
                }
            }
            out[by * bw + bx] = (acc / n).round().clamp(0.0, 255.0) as u8;
        }
    }
    (out, bw, bh)
}

/// Максимальная яркость существующих глифов (по умолчанию 'H', 'A', 'o') — эталон веса.
fn measure_peak(
    data: &[u8],
    width: usize,
    per_row: usize,
    cell_w: usize,
    cell_h: usize,
    bpp: usize,
    laft: &Laft,
    codepoints: &[u32],
) -> Option<u8> {
    let mut max = 0u8;
    for cp in codepoints {
        if let Some((grid, _)) = laft.get_glyph(*cp as u16) {
            let cx = (grid % per_row) * cell_w;
            let cy = (grid / per_row) * cell_h;
            for y in 0..cell_h {
                for x in 0..cell_w {
                    let idx = ((cy + y) * width + (cx + x)) * bpp;
                    max = max.max(data[idx]);
                }
            }
        }
    }
    (max > 0).then_some(max)
}

/// Утолщение покрытия глифа: max-фильтр в окне (2d+1)×(2d+1).
/// Утолщение с разными радиусами по осям: `dx` растит вертикальные стойки, не
/// утолщая горизонтальные штрихи (сохраняет контраст), `dy` — наоборот.
fn dilate_buf_xy(src: &[u8], w: usize, h: usize, dx: usize, dy: usize) -> (Vec<u8>, usize, usize) {
    let (w2, h2) = (w + 2 * dx, h + 2 * dy);
    let mut out = vec![0u8; w2 * h2];
    for y in 0..h {
        for x in 0..w {
            let v = src[y * w + x];
            if v == 0 {
                continue;
            }
            for ddy in 0..=2 * dy {
                for ddx in 0..=2 * dx {
                    let (nx, ny) = (x + ddx, y + ddy);
                    let idx2 = ny * w2 + nx;
                    if v > out[idx2] {
                        out[idx2] = v;
                    }
                }
            }
        }
    }
    (out, w2, h2)
}

/// Подобрать кегль TTF так, чтобы высота чернил 'H' совпала с эталоном.
/// Ширина шага буквы в пикселях. Важно: `h_advance_unscaled` возвращает метрику в
/// ЮНИТАХ шрифта, а не в пикселях — нужен масштабированный шрифт (`as_scaled`).
fn adv_px(font: &ab_glyph::FontArc, ch: char, scale: f32) -> f32 {
    use ab_glyph::ScaleFont;
    font.as_scaled(scale).h_advance(font.glyph_id(ch))
}

fn fit_scale(font: &ab_glyph::FontArc, target_h: usize) -> f32 {
    let mut best = (1.0f32, f32::MAX);
    for size in 4..(target_h as u32 * 3 + 8) {
        let scale = size as f32;
        if let Some(o) = font.outline_glyph(font.glyph_id('H').with_scale(scale)) {
            let b = o.px_bounds();
            let h = (b.max.y - b.min.y).abs();
            let d = (h - target_h as f32).abs();
            if d < best.1 {
                best = (scale, d);
            }
        }
    }
    best.0
}

fn build(
    src: &Path,
    ttf: &Path,
    out: &Path,
    dilate: f32,
    dilate_x: Option<f32>,
    dilate_y: Option<f32>,
    y_shift: i32,
    x_shift: i32,
    peak_arg: &str,
    sdf_arg: &str,
    cap_h_arg: Option<usize>,
    min_bearing: i32,
    force_glyphs: Option<&str>,
    stretch_x: f32,
    sdf_bias: f32,
) -> Result<()> {
    let mut laft = load_laft(src)?;
    let mut need: Vec<u32> = required_codepoints()
        .into_iter()
        .filter(|cp| laft.get_glyph(*cp as u16).is_none())
        .collect();
    // Принудительная перерисовка: глиф в шрифте есть, но он нам не подходит
    // (например собственное многоточие игры приподнято над строкой).
    if let Some(list) = force_glyphs {
        for cp in parse_codes(Some(list)) {
            let cp = cp as u32;
            if !need.contains(&cp) {
                need.push(cp);
            }
        }
    }
    if need.is_empty() {
        println!("все обязательные символы уже есть — файл не меняю");
        return Ok(());
    }
    println!("добавляю {} символов", need.len());

    let s = laft.settings;
    let cell_w = s.glyph_area_width as usize + 1;
    let cell_h = s.glyph_area_height as usize + 1;
    let per_row = s.glyphs_per_row as usize;
    let old_count = laft.font_info.len();

    // --- текстура: десвизлим, расширяем вниз под новые ячейки
    let tex = laft
        .texture
        .take()
        .ok_or_else(|| anyhow!("в шрифте нет текстуры"))?;
    let mut surface = tex.to_surface().map_err(|e| anyhow!("deswizzle: {e:?}"))?;
    if surface.mipmaps != 1 || surface.layers != 1 || surface.depth != 1 {
        return Err(anyhow!(
            "неожиданная текстура: mips={} layers={} depth={}",
            surface.mipmaps,
            surface.layers,
            surface.depth
        ));
    }
    let w = surface.width as usize;
    let h = surface.height as usize;
    let bpp = surface.data.len() / (w * h);
    if bpp == 0 || surface.data.len() != w * h * bpp {
        return Err(anyhow!(
            "не удалось определить bpp: data={} w={} h={}",
            surface.data.len(),
            w,
            h
        ));
    }
    let new_num_rows = (old_count + need.len()).div_ceil(per_row);
    let new_h = new_num_rows * cell_h;
    if new_h < h {
        return Err(anyhow!("новая высота {new_h} меньше исходной {h}"));
    }
    let mut data = vec![0u8; w * new_h * bpp];
    data[..w * h * bpp].copy_from_slice(&surface.data);

    // --- опорные метрики по 'H'
    let (h_grid, h_info) = laft
        .get_glyph(0x48)
        .ok_or_else(|| anyhow!("в шрифте нет глифа 'H' — не с чего снимать метрики"))?;
    // Две мерки: по «любому ненулевому» берём высоту и левый отступ (рампа входит в
    // габарит буквы), по телу глифа (порог) — правый край, чтобы не завышать шаг.
    let (_cap_any, ink_left_ref, _ink_right_any, ink_bottom_ref) =
        cell_ink_metrics(&data, w, per_row, cell_w, cell_h, h_grid, bpp, 0)?;
    // Высоту берём по ТЕЛУ глифа: замер по рампам SDF завышал её на треть, и кириллица
    // выходила крупнее латиницы игры. Можно переопределить флагом --cap-h.
    let (cap_h, _l, _r, _b) = cell_ink_metrics(&data, w, per_row, cell_w, cell_h, h_grid, bpp, 160)?;
    let cap_h = cap_h_arg.unwrap_or(cap_h);
    let (_h, _il, ink_right_ref, _ib) =
        cell_ink_metrics(&data, w, per_row, cell_w, cell_h, h_grid, bpp, 160)?;
    let pad_left = (ink_left_ref as i32 - h_info.left_x as i32).max(0) as usize;
    // Шаг глифа в LAFT = left_x + width. Правый отступ считаем от шага, а не от width,
    // иначе шаг выходит на ~10 px уже игрового и буквы наезжают друг на друга.
    let pad_right =
        (h_info.left_x as i32 + h_info.width as i32 - ink_right_ref as i32).max(0) as usize;
    let baseline = ink_bottom_ref + 1;

    // Яркость: у игровых латинских глифов пик не 255, а ~196 — новые глифы масштабируем
    // к тому же пику, иначе кириллица выглядит жирнее и ярче латиницы.
    let peak: u8 = match peak_arg {
        "auto" => measure_peak(&data, w, per_row, cell_w, cell_h, bpp, &laft, &[0x48, 0x41, 0x6F])
            .unwrap_or(196),
        other => other
            .parse::<u8>()
            .map_err(|_| anyhow!("--peak: ожидалось auto или 1..255, получено {other}"))?,
    };
    println!("пик яркости новых глифов: {peak} (scale {:.2})", peak as f32 / 255.0);
    println!(
        "опора 'H': cap_h={cap_h} left_x={} width={} pad_left={pad_left} pad_right={pad_right} baseline_row={baseline}",
        h_info.left_x, h_info.width
    );

    // --- растеризация
    let font = ab_glyph::FontArc::try_from_vec(std::fs::read(ttf)?)
        .map_err(|e| anyhow!("не смог прочитать TTF: {e:?}"))?;
    let scale = fit_scale(&font, cap_h);
    println!("кегль TTF для cap_h={cap_h}: {scale}");

    // Формат атласа: игра хранит SDF (край≈128, шаг ~29 единиц/пиксель). Если писать
    // покрытие, кириллица выглядит резче/жирнее латиницы и хуже масштабируется.
    let sdf_slope: Option<f32> = match sdf_arg {
        "off" => None,
        "auto" => measure_sdf_slope(&data, w, per_row, cell_w, cell_h, bpp, &laft, &[0x48, 0x41, 0x6F, 0x6F]),
        other => Some(
            other
                .parse::<f32>()
                .map_err(|_| anyhow!("--sdf: ожидалось auto|off|число, получено {other}"))?,
        ),
    };
    match sdf_slope {
        Some(k) => println!("формат атласа: SDF, наклон {k:.1} ед/px (край на 128)"),
        None => println!("формат атласа: покрытие (--sdf off)"),
    }

    let reduction = laft.global_width_reduction as i32;
    let mut new_infos: Vec<(u32, Option<usize>, GlyphFontInfo)> = Vec::with_capacity(need.len());
    let mut new_seq = 0usize;
    for cp in need.iter() {
        // Принудительная перерисовка: глиф уже есть — берём его же ячейку и его же
        // запись в таблице, чтобы не ломать сетку и не паниковать на повторной регистрации.
        let existing = laft.get_glyph(*cp as u16).map(|(g, _)| g);
        let grid = match existing {
            Some(g) => g,
            None => {
                let g = old_count + new_seq;
                new_seq += 1;
                g
            }
        };
        let ch = char::from_u32(*cp).ok_or_else(|| anyhow!("плохой codepoint U+{cp:04X}"))?;
        let cx0 = (grid % per_row) * cell_w;
        let cy0 = (grid / per_row) * cell_h;

        let left_x = pad_left.min(cell_w.saturating_sub(1)) as u8;
        // Часть кириллицы шире, чем допускает ячейка атласа (например «Ю»: чернила
        // 36 px при доступных 34). Такие глифы рисуем с уменьшенным кеглем, иначе
        // движок обрежет их по окну сэмплирования — «Ю» превращается в «К»-образный
        // обрубок, и это видно в игре.
        let avail = (cell_w as i32 - left_x as i32).max(1);
        // Сколько пикселей занимает рампа SDF за краем рисунка: столько же нужно оставить
        // полей слева и справа, иначе игра обрезает свечение вокруг буквы.
        let sdf_pad = match sdf_slope {
            Some(k) => ((128.0 / k).ceil() as i32) + 1,
            None => 1,
        };
        // Утолщение расширяет рисунок — учитываем его и в подгонке, и в ширине окна.
        let dxf = dilate_x.unwrap_or(dilate);
        let dyf = dilate_y.unwrap_or(dilate);
        let dx = dxf.round() as i32;
        let dy = dyf.round() as i32;
        let _ = dy;
        // Порядок такой: пока буква влезает по ширине чернил — просто убираем отступ.
        // Кегль поджимаем ТОЛЬКО когда и без отступа не влезает: иначе «Ж Ш Щ Ю Я»
        // становятся мельче остальных прописных и перестают выглядеть заглавными.
        let mut scale = scale;
        let mut gap_wanted = min_bearing;
        if let Some(o0) = font.outline_glyph(font.glyph_id(ch).with_scale(scale)) {
            let b0 = o0.px_bounds();
            let ink0 = (b0.max.x - b0.min.x).ceil().max(1.0) as i32;
            gap_wanted = gap_wanted.min(avail - ink0 - pad_right as i32);
            // Подгоняем кегль ТОЛЬКО если буква физически не влезает, и только ВНИЗ:
            // коэффициент больше единицы раньше раздувал мелкие буквы (×1.4…×2.1).
            if ink0 + 2 * sdf_pad + 2 * dx > avail {
                let need = (ink0 + 2 + pad_right as i32) as f32;
                let k = (avail as f32 / need).min(1.0).max(0.35);
                scale *= k;
                println!("  узкий кегль для U+{cp:04X}: чернила {ink0} в ячейке {avail} -> x{k:.2}");
            }
        }
        let outline = font.outline_glyph(font.glyph_id(ch).with_scale(scale));
        let sx = if stretch_x > 0.1 { stretch_x } else { 1.0 };
        let (bearing, ink_w, top_y) = match &outline {
            Some(o) => {
                let b = o.px_bounds();
                (
                    (b.min.x.max(0.0) * sx) as i32,
                    (((b.max.x - b.min.x).ceil().max(1.0)) * sx) as i32,
                    b.min.y.round() as i32,
                )
            }
            None => (0, 1, 0),
        };
        // Шаг буквы — родной из TTF (движок считает left_x + width − global_width_reduction).
        // Если шаг шире ячейки, окно урезается: буква чуть теснее, но остаётся того же роста.
        // Шаг — ТОЛЬКО из TTF. Раньше сюда подмешивались боковые отступы
        // (`.max(gap + ink + pad)`), шаг раздувался на ~15 px, буквы разъезжались,
        // а сама буква оказывалась прижатой к левому краю своего шага.
        let adv = if outline.is_some() {
            ((adv_px(&font, ch, scale) * sx).ceil() as i32 + reduction - left_x as i32).max(ink_w + 2)
        } else {
            ink_w + pad_right as i32
        };
        // Шаг буквы — родной из TTF, но окно обязано вмещать сам рисунок И SDF-рампу
        // с обеих сторон: иначе свечение вокруг буквы срезается краем окна. Буква
        // центрируется внутри окна, поля слева и справа получаются равными.
        let need_win = ink_w + 2 * (sdf_pad + dx);
        let width_px = adv.max(need_win).clamp(1, avail);
        let gap = ((width_px - ink_w) / 2).max(1);
        let _ = bearing;
        let width = width_px as u8;
        if let Some(o) = outline {
            // Принудительная перерисовка: в ячейке остались пиксели прежнего глифа —
            // без очистки на экране будет и старый, и новый рисунок (многоточие «двоится»).
            if existing.is_some() {
                for y in cy0..(cy0 + cell_h).min(new_h) {
                    for x in cx0..(cx0 + cell_w).min(w) {
                        let base = (y * w + x) * bpp;
                        for k in 0..bpp {
                            data[base + k] = 0;
                        }
                    }
                }
            }
            // покрытие в отдельный буфер, чтобы можно было утолщить (dilate)
            let b = o.px_bounds();
            let iw = ((b.max.x - b.min.x).ceil().max(1.0)) as usize;
            let ih = ((b.max.y - b.min.y).ceil().max(1.0)) as usize;
            let mut cov = vec![0u8; iw * ih];
            o.draw(|x, y, c| {
                let (x, y) = (x as usize, y as usize);
                if x < iw && y < ih {
                    cov[y * iw + x] = (c.clamp(0.0, 1.0) * 255.0).round() as u8;
                }
            });
            let (dxf, dyf) = (dxf.max(0.0), dyf.max(0.0));
            let cov_max = cov.iter().max().copied().unwrap_or(0);
            let (cov, iw2, ih2) = if dxf > 0.01 || dyf > 0.01 {
                dilate_buf_xy(&cov, iw, ih, dxf.round() as usize, dyf.round() as usize)
            } else {
                (cov, iw, ih)
            };
            // SDF: нужен запас поля, чтобы рампа уместилась
            let (buf, bw, bh, off_x, off_y) = match sdf_slope {
                Some(slope) => {
                    let pad = ((128.0 / slope).ceil() as usize) + 1;
                    // Растр увеличиваем вчетверо: иначе расстояние квантуется пикселями
                    // и сглаживание пропадает (у игровых глифов ~150 уровней, у наших было 15).
                    let ss = 4usize;
                    let (hres_w, hres_h) = (iw2 * ss, ih2 * ss);
                    let mut hres = vec![0u8; hres_w * hres_h];
                    if let Some(o_hi) =
                        font.outline_glyph(font.glyph_id(ch).with_scale(scale * ss as f32))
                    {
                        o_hi.draw(|x, y, c| {
                            let (x, y) = (x as usize, y as usize);
                            if x < hres_w && y < hres_h {
                                hres[y * hres_w + x] = (c.clamp(0.0, 1.0) * 255.0).round() as u8;
                            }
                        });
                    }
                    // Растяжение и утолщение применяем УЖЕ на этом растре: раньше ветка SDF
                    // рисовала контур заново и флаги --stretch-x/--dilate-x просто терялись.
                    let (hres, hres_w, hres_h) =
                        if (stretch_x - 1.0).abs() > 0.001 && stretch_x > 0.1 {
                            let target =
                                (((hres_w as f32 * stretch_x) as usize + ss - 1) / ss) * ss;
                            let mut st = vec![0u8; target * hres_h];
                            for y in 0..hres_h {
                                for x in 0..target {
                                    let sx = ((x as f32 / stretch_x).round() as usize)
                                        .min(hres_w - 1);
                                    st[y * target + x] = hres[y * hres_w + sx];
                                }
                            }
                            (st, target, hres_h)
                        } else {
                            (hres, hres_w, hres_h)
                        };
                    let r_hi_x = (dxf * ss as f32).round() as usize;
                    let r_hi_y = (dyf * ss as f32).round() as usize;
                    let (hres, hres_w, hres_h) = if dxf > 0.01 || dyf > 0.01 {
                        // радиус задаём в пикселях увеличенного растра — шаг 0.25 px
                        let (b, w2, h2) = dilate_buf_xy(
                            &hres, hres_w, hres_h, r_hi_x, r_hi_y,
                        );
                        (b, w2, h2)
                    } else {
                        (hres, hres_w, hres_h)
                    };
                    let (b, bw, bh) = sdf_from_coverage_ss_bias(
                        &hres, hres_w, hres_h, pad * ss, slope, ss, sdf_bias,
                    );
                    // смещение содержимого в буфере: утолщение и рампа, переведённые в пиксели ячейки
                    let off_x = ((r_hi_x + pad * ss) / ss) as i32;
                    let off_y = ((r_hi_y + pad * ss) / ss) as i32;
                    (b, bw, bh, off_x, off_y)
                }
                None => {
                    let r = dxf.round() as i32;
                    let r2 = dyf.round() as i32;
                    (cov, iw2, ih2, r, r2)
                }
            };
            if new_infos.is_empty() {
                let cmin = buf.iter().min().copied().unwrap_or(0);
                let cmax = buf.iter().max().copied().unwrap_or(0);
                println!(
                    "  отладка глифа U+{cp:04X}: cov {}x{} (max {cov_max}), буфер {}x{}, значения {cmin}..{cmax}",
                    iw2, ih2, bw, bh
                );
            }
            let dst_x = cx0 as i32 + left_x as i32 + gap - off_x + x_shift;
            let dst_y = cy0 as i32 + baseline as i32 + top_y - off_y + y_shift;
            for y in 0..bh {
                for x in 0..bw {
                    let v = buf[y * bw + x];
                    if v == 0 {
                        continue;
                    }
                    let px = dst_x + x as i32;
                    let py = dst_y + y as i32;
                    if px < 0 || py < 0 || px >= w as i32 || py >= new_h as i32 {
                        continue;
                    }
                    let v = if sdf_slope.is_some() {
                        v
                    } else {
                        ((v as f32) * (peak as f32 / 255.0)).round().clamp(0.0, 255.0) as u8
                    };
                    let base = (py as usize * w + px as usize) * bpp;
                    for k in 0..bpp {
                        data[base + k] = v;
                    }
                }
            }
        }
        new_infos.push((
            *cp,
            existing,
            GlyphFontInfo {
                codepoint: *cp as u16,
                left_x,
                width,
            },
        ));
    }

    // --- собираем файл
    surface.height = new_h as u32;
    surface.data = data;
    let new_tex = xc3_lib::mibl::Mibl::from_surface(surface)
        .map_err(|e| anyhow!("swizzle обратно: {e:?}"))?;
    laft.texture = Some(new_tex);
    laft.settings.texture_height = new_h as u32;
    laft.settings.num_rows = new_num_rows as u32;
    for (cp, existing, info) in new_infos {
        match existing {
            // ячейка своя, но содержимое перерисовано — правим только метрики
            Some(grid) => laft.font_info[grid] = info,
            None => {
                let _ = cp;
                laft.register_glyph(info);
            }
        }
    }
    write_laft(out, &laft)?;

    // --- самопроверка метрик: однообразные шаги или разная высота прописных — признак бага
    {
        use ab_glyph::ScaleFont;
        let probe = "НАОЖШЩЮЯиоmn";
        let mut advs: Vec<(char, i32)> = Vec::new();
        for ch in probe.chars() {
            let a = adv_px(&font, ch, scale).ceil() as i32;
            advs.push((ch, a));
        }
        let vals: Vec<i32> = advs.iter().map(|(_, a)| *a).collect();
        let (mn, mx) = (vals.iter().min().copied().unwrap_or(0), vals.iter().max().copied().unwrap_or(0));
        println!(
            "  самопроверка шагов: {}",
            advs.iter().map(|(c, a)| format!("{c}:{a}")).collect::<Vec<_>>().join(" ")
        );
        if mx - mn < 3 {
            println!("  ВНИМАНИЕ: шаги почти одинаковые ({mn}..{mx}) — вероятно, метрика взята не в пикселях");
        }
    }
    // --- самопроверка
    let check = load_laft(out)?;
    let mut still: Vec<u32> = required_codepoints()
        .into_iter()
        .filter(|cp| check.get_glyph(*cp as u16).is_none())
        .collect();
    still.sort_unstable();
    if !still.is_empty() {
        return Err(anyhow!(
            "после сборки всё ещё отсутствуют {} символов (первый U+{:04X})",
            still.len(),
            still[0]
        ));
    }
    println!(
        "итог: глифов {} -> {}, текстура {}x{} -> {}x{}",
        old_count,
        check.font_info.len(),
        s.texture_width,
        s.texture_height,
        check.settings.texture_width,
        check.settings.texture_height
    );
    Ok(())
}
