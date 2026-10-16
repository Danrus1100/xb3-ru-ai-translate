//! Общий рендер текста глифами .wifnt — для CLI-предпросмотра и для GUI.
//!
//! Раскладка повторяет движок игры: шаг буквы = `left_x + width − global_width_reduction`,
//! базовая линия — на строке `line_height` внутри ячейки, значения SDF дают сглаживание.

use anyhow::{anyhow, Result};
use binrw::BinRead;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;
use xc3_lib::laft::Laft;
use xc3_lib::xbc1::MaybeXbc1;

pub struct Font {
    pub laft: Laft,
    data: Vec<u8>,
    w: usize,
    h: usize,
    bpp: usize,
    pub cell_w: usize,
    pub cell_h: usize,
    per_row: usize,
    pub reduction: usize,
}

impl Font {
    pub fn load(path: &Path) -> Result<Self> {
        let mut reader = BufReader::new(File::open(path)?);
        let file = MaybeXbc1::<Laft>::read_le(&mut reader)?;
        let laft = match file {
            MaybeXbc1::Uncompressed(l) => l,
            MaybeXbc1::Xbc1(x) => x.extract::<Laft>().map_err(|e| anyhow!("распаковка xbc1: {e:?}"))?,
        };
        let tex = laft.texture.as_ref().ok_or_else(|| anyhow!("в шрифте нет текстуры"))?;
        let surface = tex.to_surface().map_err(|e| anyhow!("deswizzle: {e:?}"))?;
        let w = surface.width as usize;
        let h = surface.height as usize;
        let bpp = surface.data.len() / (w * h).max(1);
        let cell_w = laft.settings.glyph_area_width as usize + 1;
        let cell_h = laft.settings.glyph_area_height as usize + 1;
        let per_row = laft.settings.glyphs_per_row as usize;
        Ok(Self {
            reduction: laft.global_width_reduction as usize,
            laft,
            data: surface.data,
            w,
            h,
            bpp,
            cell_w,
            cell_h,
            per_row,
        })
    }

    pub fn has(&self, ch: char) -> bool {
        self.laft.get_glyph(ch as u16).is_some()
    }

    /// Шаг буквы так, как его считает движок.
    pub fn advance(&self, ch: char) -> usize {
        match self.laft.get_glyph(ch as u16) {
            Some((_, info)) => (info.left_x as usize + info.width as usize)
                .saturating_sub(self.reduction)
                .max(1),
            None => 8,
        }
    }

    pub fn line_height(&self) -> usize {
        self.cell_h + 6
    }

    /// Нарисовать один глиф так, как это делает движок: колонка `pen`, верх ячейки
    /// `cell_top`. Возвращает шаг буквы для продолжения строки.
    fn blit(
        &self,
        ch: char,
        pen: usize,
        cell_top: usize,
        img: &mut image::GrayImage,
        sc: usize,
        boxes: bool,
    ) -> usize {
        let Some((grid, info)) = self.laft.get_glyph(ch as u16) else {
            return 8;
        };
        let (left_x, width) = (info.left_x as usize, info.width as usize);
        let cx = (grid % self.per_row) * self.cell_w;
        let cy = (grid / self.per_row) * self.cell_h;
        for y in 0..self.cell_h {
            for x in left_x..(left_x + width).min(self.cell_w) {
                let idx = ((cy + y) * self.w + (cx + x)) * self.bpp;
                let raw = self.data.get(idx).copied().unwrap_or(0) as f32;
                let cov = ((raw - 128.0) / 30.0 + 0.5).clamp(0.0, 1.0);
                if cov <= 0.02 {
                    continue;
                }
                let px = pen + (x - left_x);
                let py = cell_top + y;
                if px >= img.width() as usize || py >= img.height() as usize {
                    continue;
                }
                let val = (cov * 255.0) as u8;
                for sy in 0..sc {
                    for sx in 0..sc {
                        img.put_pixel((px * sc + sx) as u32, (py * sc + sy) as u32, image::Luma([val]));
                    }
                }
            }
        }
        if boxes {
            for y in 0..self.cell_h {
                let (px, py) = (pen, cell_top + y);
                if px < img.width() as usize && py < img.height() as usize {
                    img.put_pixel((px * sc) as u32, (py * sc) as u32, image::Luma([70]));
                }
            }
        }
        self.advance(ch)
    }

    /// Отрисовать пары «кириллица — латиница» в общих слотах: обе буквы каждой пары
    /// встают на одну и ту же позицию, поэтому наложение сравнивает сами буквы, а не
    /// накопленную разницу шагов. Возвращает две картинки: наша и игровая.
    pub fn render_pairs(
        ru: &Font,
        en: &Font,
        pairs: &[(char, char)],
        scale: u32,
        gap: usize,
    ) -> (image::GrayImage, image::GrayImage) {
        let sc = scale.max(1) as usize;
        let line_h = ru.line_height();
        let cell_top = 4;
        let mut slots = 0usize;
        for (cyr, lat) in pairs {
            slots += ru.advance(*cyr).max(en.advance(*lat)) + gap;
        }
        let img_w = slots + 16;
        let img_h = line_h + 8;
        let mut a = image::GrayImage::new((img_w * sc) as u32, (img_h * sc) as u32);
        let mut b = image::GrayImage::new((img_w * sc) as u32, (img_h * sc) as u32);
        let mut pen = 8usize;
        for (cyr, lat) in pairs {
            ru.blit(*cyr, pen, cell_top, &mut a, sc, false);
            en.blit(*lat, pen, cell_top, &mut b, sc, false);
            pen += ru.advance(*cyr).max(en.advance(*lat)) + gap;
        }
        (a, b)
    }

    /// Отрисовать многострочный текст (строки через `\n`).
    pub fn render(&self, text: &str, scale: u32, boxes: bool) -> image::GrayImage {
        let unescaped = text.replace("\\n", "\n");
        let lines: Vec<&str> = unescaped.split('\n').collect();
        let sc = scale.max(1) as usize;
        let line_h = self.line_height();
        let baseline = self.laft.line_height as usize;
        let mut width_px = 32usize;
        let mut pen_per_line: Vec<Vec<(usize, usize, usize, usize)>> = Vec::new();
        for line in &lines {
            let mut pen = 0usize;
            let mut row = Vec::new();
            for ch in line.chars() {
                if let Some((grid, info)) = self.laft.get_glyph(ch as u16) {
                    row.push((grid, pen, info.left_x as usize, info.width as usize));
                }
                pen += self.advance(ch);
            }
            width_px = width_px.max(pen);
            pen_per_line.push(row);
        }
        let img_w = width_px + 16;
        let img_h = lines.len() * line_h + 8;
        let mut img = image::GrayImage::new((img_w * sc) as u32, (img_h * sc) as u32);
        for (li, row) in pen_per_line.iter().enumerate() {
            let cell_top = 4 + li * line_h;
            for &(grid, pen, left_x, width) in row {
                let cx = (grid % self.per_row) * self.cell_w;
                let cy = (grid / self.per_row) * self.cell_h;
                for y in 0..self.cell_h {
                    for x in left_x..(left_x + width).min(self.cell_w) {
                        let idx = ((cy + y) * self.w + (cx + x)) * self.bpp;
                        let raw = self.data.get(idx).copied().unwrap_or(0) as f32;
                        let cov = ((raw - 128.0) / 30.0 + 0.5).clamp(0.0, 1.0);
                        if cov <= 0.02 {
                            continue;
                        }
                        let px = pen + (x - left_x);
                        let py = cell_top + y;
                        if px >= img_w || py >= img_h {
                            continue;
                        }
                        let val = (cov * 255.0) as u8;
                        for sy in 0..sc {
                            for sx in 0..sc {
                                img.put_pixel(
                                    (px * sc + sx) as u32,
                                    (py * sc + sy) as u32,
                                    image::Luma([val]),
                                );
                            }
                        }
                    }
                }
                if boxes {
                    let _ = y_box(&mut img, pen, cell_top, self.cell_h, sc, img_w, img_h);
                }
            }
        }
        let _ = baseline;
        img
    }

    /// Текст, которого в шрифте нет, — важно видеть дырки, а не пропускать молча.
    pub fn missing(&self, text: &str) -> Vec<char> {
        let unescaped = text.replace("\\n", "\n");
        let mut out: Vec<char> = unescaped
            .chars()
            .filter(|c| *c != '\n' && *c != ' ' && !self.has(*c))
            .collect();
        out.sort_unstable();
        out.dedup();
        out
    }
}

fn y_box(
    img: &mut image::GrayImage,
    pen: usize,
    cell_top: usize,
    cell_h: usize,
    sc: usize,
    img_w: usize,
    img_h: usize,
) -> bool {
    for y in 0..cell_h {
        let px = pen;
        let py = cell_top + y;
        if px < img_w && py < img_h {
            img.put_pixel((px * sc) as u32, (py * sc) as u32, image::Luma([70]));
        }
    }
    true
}
