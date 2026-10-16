//! GUI-предпросмотр шрифта: оба языка сразу, обновление при сохранении файла.
//!
//! Запуск:
//!   fontui.exe --ru work\font\patched\standard.wifnt --en work\font\extracted\menu\font\standard.wifnt
//!
//! Окно можно перетащить на второй монитор. Каждые 0.7 с проверяется время правки
//! файлов: сохранили глиф (или пересобрали шрифт) — картинка обновилась сама.

#[path = "../render.rs"]
mod render;

use eframe::egui;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime};

/// Пары похожих букв — для наложения: одинаковые по начертанию кириллица и латиница.
/// Пары похожих букв — для наложения: кириллица с её латинским аналогом.
const PAIRS_UPPER: &[(char, char)] = &[
    ('А', 'A'), ('Б', 'B'), ('В', 'B'), ('Е', 'E'), ('К', 'K'), ('М', 'M'),
    ('Н', 'H'), ('О', 'O'), ('Р', 'P'), ('С', 'C'), ('Т', 'T'), ('Х', 'X'),
];
const PAIRS_LOWER: &[(char, char)] = &[
    ('а', 'a'), ('е', 'e'), ('о', 'o'), ('р', 'p'), ('с', 'c'), ('у', 'y'), ('х', 'x'),
];

const TEXT_RU: &str = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ\n\
абвгдеёжзийклмнопрстуфхцчшщъыьэюя\n\
Съешь ещё этих мягких французских булок, да выпей чаю\n\
«позиция…» — (5–7), 100 %!  Юни, Жак, Мы!";

const TEXT_EN: &str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ\n\
abcdefghijklmnopqrstuvwxyz\n\
The quick brown fox jumps over the lazy dog\n\
\"position...\" - (5-7), 100 %!  Eunie, Jack, We!";

struct Pane {
    title: String,
    path: PathBuf,
    text: &'static str,
    font: Option<render::Font>,
    texture: Option<egui::TextureHandle>,
    mtime: Option<SystemTime>,
    error: Option<String>,
}

impl Pane {
    fn new(title: &str, path: PathBuf, text: &'static str) -> Self {
        Self {
            title: title.to_string(),
            path,
            text,
            font: None,
            texture: None,
            mtime: None,
            error: None,
        }
    }

    fn reload(&mut self, ctx: &egui::Context, scale: u32, boxes: bool) {
        match render::Font::load(&self.path) {
            Ok(font) => {
                let img = font.render(self.text, scale, boxes);
                let size = [img.width() as usize, img.height() as usize];
                let pixels: Vec<egui::Color32> = img
                    .pixels()
                    .map(|p| egui::Color32::from_gray(p.0[0]))
                    .collect();
                let color = egui::ColorImage {
                    size,
                    pixels,
                };
                self.texture = Some(ctx.load_texture(
                    self.title.clone(),
                    color,
                    egui::TextureOptions::LINEAR,
                ));
                let missing = font.missing(self.text);
                self.error = if missing.is_empty() {
                    None
                } else {
                    Some(format!("нет глифов: {}", missing.iter().collect::<String>()))
                };
                self.font = Some(font);
            }
            Err(e) => self.error = Some(format!("{e}")),
        }
        self.mtime = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
    }

    fn watch(&self) -> Option<SystemTime> {
        std::fs::metadata(&self.path).and_then(|m| m.modified()).ok()
    }
}

struct App {
    ru: Pane,
    en: Pane,
    /// Масштаб показа: шаг 0.25, можно наводить на удобный размер.
    zoom: f32,
    /// Целый коэффициент, с которым реально отрисованы текстуры (чтобы было резко).
    render_scale: u32,
    /// Наложение вместо двух панелей: кириллица зелёным, латиница розовым.
    overlay: bool,
    overlay_texture: Option<egui::TextureHandle>,
    boxes: bool,
    last_check: Instant,
}

impl App {
    fn new(ru: PathBuf, en: PathBuf) -> Self {
        Self {
            ru: Pane::new("Русский (наш шрифт)", ru, TEXT_RU),
            en: Pane::new("Английский (оригинал)", en, TEXT_EN),
            zoom: 2.0,
            render_scale: 2,
            overlay: false,
            overlay_texture: None,
            boxes: false,
            last_check: Instant::now(),
        }
    }
}

impl App {
    /// Наложение: кириллица в зелёном канале, латиница в красном. Совпавшие буквы
    /// дают жёлтый — сразу видно, где размеры и базовая линия расходятся.
    fn build_overlay(&mut self, ctx: &egui::Context, scale: u32) {
        let (Some(ru), Some(en)) = (&self.ru.font, &self.en.font) else {
            self.overlay_texture = None;
            return;
        };
        let mut pairs: Vec<(char, char)> = PAIRS_UPPER.to_vec();
        pairs.extend_from_slice(PAIRS_LOWER);
        let (a, b) = render::Font::render_pairs(ru, en, &pairs, scale, 14);
        let (w, h) = (a.width().max(b.width()), a.height().max(b.height()));
        let mut img = egui::ColorImage {
            size: [w as usize, h as usize],
            pixels: Vec::with_capacity((w * h) as usize),
        };
        for y in 0..h {
            for x in 0..w {
                let g = if x < a.width() && y < a.height() { a.get_pixel(x, y).0[0] } else { 0 };
                let r = if x < b.width() && y < b.height() { b.get_pixel(x, y).0[0] } else { 0 };
                img.pixels.push(egui::Color32::from_rgb(r, g, 0));
            }
        }
        self.overlay_texture = Some(ctx.load_texture("overlay", img, egui::TextureOptions::LINEAR));
    }

    fn toggle_overlay(&mut self, ctx: &egui::Context) {
        self.overlay = !self.overlay;
        if self.overlay {
            let s = self.render_scale;
            self.build_overlay(ctx, s);
        }
    }

    /// Перерисовать текстуры, если текущий целый коэффициент больше не подходит.
    fn apply_zoom(&mut self, ctx: &egui::Context, zoom: f32) {
        let zoom = (zoom * 4.0).round() / 4.0; // шаг 0.25
        let zoom = zoom.clamp(0.25, 8.0);
        self.zoom = zoom;
        let want = (zoom.ceil() as u32).clamp(1, 8);
        if want != self.render_scale || self.ru.texture.is_none() {
            self.render_scale = want;
            let b = self.boxes;
            self.ru.reload(ctx, want, b);
            self.en.reload(ctx, want, b);
            if self.overlay {
                self.build_overlay(ctx, want);
            }
        }
    }
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // раз в 0.7 с смотрим время правки: сохранили файл — перерисовали
        if self.last_check.elapsed() > Duration::from_millis(700) {
            self.last_check = Instant::now();
            for pane in [&mut self.ru, &mut self.en] {
                if pane.texture.is_none() || pane.watch() != pane.mtime {
                    pane.reload(ctx, self.render_scale, self.boxes);
                }
            }
        }
        if ctx.input(|i| i.key_pressed(egui::Key::Tab)) {
            self.toggle_overlay(ctx);
        }
        // Ctrl+колесо — плавный зум шагом 0.25
        let (scroll, ctrl) = ctx.input(|i| (i.raw_scroll_delta.y, i.modifiers.ctrl));
        if ctrl && scroll.abs() > 0.0 {
            let dz = if scroll > 0.0 { 0.25 } else { -0.25 };
            self.apply_zoom(ctx, self.zoom + dz);
        }
        ctx.request_repaint_after(Duration::from_millis(700));

        egui::TopBottomPanel::top("toolbar").show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.label("Масштаб (шаг 0.25):");
                if ui.button("− 0.25").clicked() {
                    let z = self.zoom - 0.25;
                    self.apply_zoom(ctx, z);
                }
                ui.label(format!("×{}", trim_zoom(self.zoom)));
                if ui.button("+ 0.25").clicked() {
                    let z = self.zoom + 0.25;
                    self.apply_zoom(ctx, z);
                }
                if ui.button("Сброс").clicked() {
                    self.apply_zoom(ctx, 2.0);
                }
                ui.separator();
                let label = if self.overlay { "режим: наложение (Tab)" } else { "режим: рядом (Tab)" };
                if ui.button(label).clicked() {
                    self.toggle_overlay(ctx);
                }
                if ui.checkbox(&mut self.boxes, "рамки шага").changed() {
                    let (s, b) = (self.render_scale, self.boxes);
                    self.ru.reload(ctx, s, b);
                    self.en.reload(ctx, s, b);
                }
                if ui.button("Обновить").clicked() {
                    let (s, b) = (self.render_scale, self.boxes);
                    self.ru.reload(ctx, s, b);
                    self.en.reload(ctx, s, b);
                }
                ui.separator();
                ui.label(format!("рус: {}", self.ru.path.display()));
            });
            ui.horizontal(|ui| {
                ui.label(format!("англ: {}", self.en.path.display()));
                ui.label("— файлы перечитываются автоматически, как только их сохранили");
            });
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::both().show(ui, |ui| {
                if self.overlay {
                    ui.add_space(6.0);
                    ui.heading("Наложение: зелёный — наша кириллица, розовый — латиница игры, жёлтый — совпадение");
                    ui.label("Пары похожих букв: если буквы совпали по размеру и строке, они становятся жёлтыми.");
                    match &self.overlay_texture {
                        Some(tex) => {
                            let size = tex.size_vec2() * (self.zoom / self.render_scale as f32);
                            ui.image((tex.id(), size));
                        }
                        None => {
                            ui.label("ещё не собрано — нажмите «Обновить»");
                        }
                    }
                    return;
                }
                for pane in [&self.ru, &self.en] {
                    ui.add_space(6.0);
                    ui.heading(&pane.title);
                    if let Some(err) = &pane.error {
                        ui.colored_label(egui::Color32::from_rgb(220, 120, 120), err);
                    }
                    match &pane.texture {
                        Some(tex) => {
                            let size = tex.size_vec2() * (self.zoom / self.render_scale as f32);
                            ui.image((tex.id(), size));
                        }
                        None => {
                            ui.label("ещё не загружено");
                        }
                    }
                    ui.add_space(10.0);
                }
            });
        });
    }
}

/// Аргумент командной строки. PowerShell при передаче массива добавляет кавычки,
/// а путь в кавычках на Windows даёт «os error 123», поэтому снимаем их.
fn trim_zoom(z: f32) -> String {
    let s = format!("{:.2}", z);
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

fn arg(name: &str, default: &str) -> PathBuf {
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        if a == name {
            if let Some(v) = it.next() {
                let clean = v.trim().trim_matches('"').trim_matches('\'').to_string();
                if !clean.is_empty() {
                    return PathBuf::from(clean);
                }
            }
        }
    }
    PathBuf::from(default)
}

fn main() -> eframe::Result<()> {
    let ru = arg("--ru", "work/font/patched/standard.wifnt");
    let en = arg("--en", "work/font/extracted/menu/font/standard.wifnt");
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1280.0, 900.0])
            .with_title("XC3 шрифт: предпросмотр"),
        ..Default::default()
    };
    eframe::run_native(
        "XC3 шрифт: предпросмотр",
        options,
        Box::new(move |_cc| Ok(Box::new(App::new(ru.clone(), en.clone())))),
    )
}
