//! Замена файлов внутри архива `bf3.ard`/`bf3.arh` (ARH1, Xenoblade 3).
//!
//! Два режима:
//! - `--mode rewrite` — через `ardain` (`ArdFileAllocator::replace_file` + `sync`):
//!   **переписывает ARH целиком** (добавляет свою ext-секцию и меняет ключ шифрования
//!   строковой таблицы). Игрой такой ARH не принимается — годится только для проверок
//!   сторонними читателями.
//! - `--mode append` (по умолчанию) — данные дописываются в конец `.ard`, а в
//!   **оригинальном** ARH правится ровно одна запись таблицы метаданных
//!   (offset u64, compressed u32, uncompressed u32). Раскладка и ключ ARH не меняются.
//!
//! Пример:
//!   ardpak --arh bf3.arh --ard bf3.ard --out-arh bf3.new.arh --dir staged \
//!          /bdat/gb/game/menu.bdat /menu/font/standard.wifnt
//! Локальные файлы берутся как `<dir>/<путь без ведущего слэша>`; `--ard` правится
//! НА МЕСТЕ (read+write), поэтому сначала делайте копию.

use anyhow::{anyhow, Context, Result};
use ardain::{
    file_alloc::{ArdFileAllocator, CompressionStrategy},
    path::ArhPath,
    ArdWriter, Arh1FileSystem,
};
use clap::Parser;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use xc3_lib::xbc1::{CompressionType, Xbc1};

const META_ENTRY_SIZE: usize = 24;

#[derive(Parser)]
#[command(about = "Замена файлов в bf3.ard/bf3.arh (XC3)")]
struct Cli {
    /// Исходный .arh
    #[arg(long)]
    arh: PathBuf,
    /// .ard, который правится на месте (read+write)
    #[arg(long)]
    ard: PathBuf,
    /// Куда записать обновлённый .arh
    #[arg(long)]
    out_arh: PathBuf,
    /// Каталог с локальными файлами (зеркало путей архива)
    #[arg(long)]
    dir: PathBuf,
    /// append (по умолчанию, безопасный) | rewrite (через ardain)
    #[arg(long, default_value = "append")]
    mode: String,
    /// Сжатие новых данных: none | zlib | zstd
    #[arg(long, default_value = "zstd")]
    compress: String,
    /// Пути внутри архива, например /bdat/gb/game/menu.bdat
    paths: Vec<String>,
}

fn compression(cli: &Cli) -> Result<CompressionStrategy> {
    Ok(match cli.compress.as_str() {
        "none" => CompressionStrategy::None,
        "zlib" => CompressionStrategy::Standard(CompressionType::Zlib),
        "zstd" => CompressionStrategy::Standard(CompressionType::Zstd),
        other => return Err(anyhow!("неизвестное сжатие {other}")),
    })
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    if cli.paths.is_empty() {
        return Err(anyhow!("не заданы пути внутри архива"));
    }
    let strategy = compression(&cli)?;
    let mut arh = Arh1FileSystem::load(File::open(&cli.arh)?)
        .with_context(|| format!("чтение {}", cli.arh.display()))?;

    match cli.mode.as_str() {
        "rewrite" => {
            rewrite_mode(&cli, &mut arh, &strategy)?;
            arh.sync(BufWriter::new(File::create(&cli.out_arh)?))
                .context("sync arh")?;
        }
        "append" => append_mode(&cli, &mut arh, &strategy)?,
        other => return Err(anyhow!("неизвестный режим {other}")),
    }
    println!(
        "готово: {} обновлён, {} записан",
        cli.ard.display(),
        cli.out_arh.display()
    );
    Ok(())
}

/// Переписать ARH целиком (раскладка меняется — игра такой файл не принимает).
fn rewrite_mode(cli: &Cli, arh: &mut Arh1FileSystem, strategy: &CompressionStrategy) -> Result<()> {
    let mut jobs = Vec::new();
    for p in &cli.paths {
        let apath = ArhPath::normalize(p)?;
        let info = arh
            .get_file_info(&apath)
            .ok_or_else(|| anyhow!("в архиве нет {}", apath.as_str()))?;
        let file_id: u32 = info
            .unique_id
            .try_into()
            .map_err(|_| anyhow!("file_id не влезает в u32"))?;
        let local = local_path(&cli.dir, &apath);
        let data = std::fs::read(&local)
            .with_context(|| format!("чтение локального файла {}", local.display()))?;
        println!("  {} <- {} ({} байт)", apath.as_str(), local.display(), data.len());
        jobs.push((file_id, data));
    }
    let mut ard = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&cli.ard)
        .with_context(|| format!("открытие {} (нужен read+write)", cli.ard.display()))?;
    let mut writer = ArdWriter::new(&mut ard);
    let mut allocator = ArdFileAllocator::new(arh, &mut writer);
    for (file_id, data) in &jobs {
        allocator
            .replace_file(*file_id, data, clone_strategy(strategy)?)
            .context("replace_file")?;
    }
    Ok(())
}

/// Дописать данные в конец `.ard` и пропатчить одну запись в оригинальном ARH.
fn append_mode(cli: &Cli, arh: &mut Arh1FileSystem, strategy: &CompressionStrategy) -> Result<()> {
    let mut arh_bytes = std::fs::read(&cli.arh)?;
    if arh_bytes.len() < 48 || &arh_bytes[0..4] != b"arh1" {
        return Err(anyhow!("{} не похож на ARH1", cli.arh.display()));
    }
    let rd_u32 = |b: &[u8], o: usize| u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]);
    let meta_ptr = rd_u32(&arh_bytes, 28) as usize;
    let file_count = rd_u32(&arh_bytes, 32) as usize;
    let meta_end = meta_ptr + file_count * META_ENTRY_SIZE;
    if meta_end > arh_bytes.len() {
        return Err(anyhow!("таблица метаданных выходит за файл"));
    }

    let mut ard = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&cli.ard)
        .with_context(|| format!("открытие {}", cli.ard.display()))?;

    for p in &cli.paths {
        let apath = ArhPath::normalize(p)?;
        let info = arh
            .get_file_info(&apath)
            .ok_or_else(|| anyhow!("в архиве нет {}", apath.as_str()))?;
        let old_off = info.ard_offset;

        // ищем запись метаданных по совпадению offset (offset уникален)
        let mut entry = None;
        for i in 0..file_count {
            let e = meta_ptr + i * META_ENTRY_SIZE;
            let off = u64::from_le_bytes(arh_bytes[e..e + 8].try_into().unwrap());
            if off == old_off {
                entry = Some(e);
                break;
            }
        }
        let entry = entry.ok_or_else(|| {
            anyhow!(
                "не нашёл запись метаданных для {} (offset 0x{:X})",
                apath.as_str(),
                old_off
            )
        })?;

        let local = local_path(&cli.dir, &apath);
        let data = std::fs::read(&local)
            .with_context(|| format!("чтение локального файла {}", local.display()))?;
        let blob = match strategy {
            CompressionStrategy::None => data.clone(),
            CompressionStrategy::Standard(ty) => {
                let x = Xbc1::from_decompressed(String::new(), &data, *ty)
                    .map_err(|e| anyhow!("сжатие: {e:?}"))?;
                let mut cursor = std::io::Cursor::new(Vec::new());
                x_write(&x, &mut cursor)?;
                cursor.into_inner()
            }
            _ => return Err(anyhow!("режим append поддерживает только none|zlib|zstd")),
        };

        ard.seek(SeekFrom::End(0))?;
        let new_off = ard.stream_position()?;
        ard.write_all(&blob)?;

        arh_bytes[entry..entry + 8].copy_from_slice(&new_off.to_le_bytes());
        arh_bytes[entry + 8..entry + 12].copy_from_slice(&(blob.len() as u32).to_le_bytes());
        arh_bytes[entry + 12..entry + 16].copy_from_slice(&(data.len() as u32).to_le_bytes());

        println!(
            "  {} <- {} ({} байт -> blob {} байт @ 0x{:X}, было @ 0x{:X})",
            apath.as_str(),
            local.display(),
            data.len(),
            blob.len(),
            new_off,
            old_off
        );
    }
    ard.flush()?;
    std::fs::write(&cli.out_arh, &arh_bytes)?;
    Ok(())
}

fn x_write(x: &Xbc1, out: &mut std::io::Cursor<Vec<u8>>) -> Result<()> {
    use binrw::BinWrite;
    x.write_le(out)
        .map_err(|e| anyhow!("сериализация xbc1: {e:?}"))
}

fn local_path(dir: &Path, apath: &ArhPath) -> PathBuf {
    dir.join(apath.as_str().trim_start_matches('/'))
}

/// `CompressionStrategy` не реализует `Clone`/`Copy`: восстанавливаем по необходимости.
fn clone_strategy(s: &CompressionStrategy) -> Result<CompressionStrategy> {
    Ok(match s {
        CompressionStrategy::None => CompressionStrategy::None,
        CompressionStrategy::Standard(ty) => CompressionStrategy::Standard(*ty),
        _ => return Err(anyhow!("неизвестная стратегия сжатия")),
    })
}
