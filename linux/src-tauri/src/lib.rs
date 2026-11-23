//! SyncLocal360 Galeria - samodzielna aplikacja na Linuksa.
//!
//! Część projektu SyncLocal360, opartego na dodatku NoCloud2 autorstwa
//! Kamila Sokalskiego (https://modelowanie3d.org), wydanym na licencji
//! otwartej z wymogiem uznania autorstwa.
//!
//! Interfejs to ten sam plik co w dodatku Fusion (`resources/gallery/index.html`).
//! W Fusion strona rozmawia z serwerem HTTP w Pythonie (`synclocal_gallery.py`),
//! tutaj z komendami Tauri poniżej. Format danych jest ten sam:
//!   * `<projekt>/config.json`: `tag` (kategoria, "unassigned" = brak), `tags`, `note`, `favorite`,
//!   * `<folder kopii>/synclocal_gallery.json`: lista kategorii (nazwa, kolor, skrót `key`).
//! Podkategorie oddziela `/` w nazwie (`Meble/Krzesła`), jak w `synclocal_gallery.py`.

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::UNIX_EPOCH;
use tauri::{Manager, State};
use tauri_plugin_dialog::DialogExt;

const NO_CATEGORY: &str = "unassigned";
const CAT_SEP: char = '/';
/// Skrót klawiszowy kategorii: "" = automatycznie (kolejna wolna cyfra), "-" = brak, albo jeden znak.
/// F i R są zajęte (ulubione, odświeżanie), 0 usuwa kategorię.
const HOTKEYS: &str = "123456789abcdeghijklmnopqstuvwxyz-";
const CATEGORIES_FILE: &str = "synclocal_gallery.json";
const MAX_RECENT: usize = 8;

// ---------------------------------------------------------------------------
// USTAWIENIA APLIKACJI (~/.config/org.synclocal360.galeria/settings.json)
// ---------------------------------------------------------------------------
#[derive(Serialize, Deserialize, Clone, Default)]
struct Settings {
    #[serde(default)]
    vault: String,
    #[serde(default)]
    recent: Vec<String>,
    #[serde(default)]
    apps: Vec<AppEntry>,
    #[serde(default)]
    apps_initialized: bool,
    /// Nazwy programów, które aplikacja już raz sama dopisała. Program usunięty
    /// przez użytkownika nie wraca, a nowo obsługiwany (np. MitCad) dochodzi do listy.
    #[serde(default)]
    apps_offered: Vec<String>,
    /// Program na dwuklik: indeks w `apps`, -1 = program domyślny systemu,
    /// brak = pierwszy program pasujący do rozszerzenia.
    #[serde(default)]
    dblclick: Option<i64>,
    /// Własne formaty (NATIVE_EXTS) zostały już raz dopisane do znanych programów.
    #[serde(default)]
    native_exts_added: bool,
}

#[derive(Serialize, Deserialize, Clone)]
struct AppEntry {
    name: String,
    /// Polecenie, `%f` = ścieżka pliku (gdy brak, ścieżka idzie na koniec).
    command: String,
    /// Rozszerzenia bez kropki, małymi literami; puste = wszystkie pliki.
    #[serde(default)]
    exts: Vec<String>,
}

struct AppState {
    settings: Mutex<Settings>,
    settings_path: PathBuf,
    write_lock: Mutex<()>,
}

impl AppState {
    fn vault(&self) -> Result<PathBuf, String> {
        let v = self.settings.lock().unwrap().vault.clone();
        let p = PathBuf::from(&v);
        if v.is_empty() || !p.is_dir() {
            return Err("Nie wybrano folderu kopii albo folder nie istnieje.".into());
        }
        Ok(p)
    }

    fn save(&self) -> Result<(), String> {
        let s = self.settings.lock().unwrap().clone();
        if let Some(dir) = self.settings_path.parent() {
            fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        let v = serde_json::to_value(&s).map_err(|e| e.to_string())?;
        write_json(&self.settings_path, &v)
    }

    fn set_vault(&self, path: &Path) -> Result<(), String> {
        if !path.is_dir() {
            return Err(format!("Folder nie istnieje: {}", path.display()));
        }
        let p = path.to_string_lossy().to_string();
        {
            let mut s = self.settings.lock().unwrap();
            s.vault = p.clone();
            s.recent.retain(|r| r != &p);
            s.recent.insert(0, p);
            s.recent.truncate(MAX_RECENT);
        }
        self.save()
    }
}

fn on_path(name: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|d| d.join(name).is_file()))
        .unwrap_or(false)
}

fn home_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default())
}

/// Wersja z nazwy pliku AppImage, np. `Mitcad-0.70.0-x86_64.AppImage` -> [0, 70, 0].
fn appimage_version(name: &str) -> Vec<u64> {
    name.split(|c: char| c == '-' || c == '_')
        .find(|part| part.starts_with(|c: char| c.is_ascii_digit()) && part.contains('.'))
        .map(|v| v.split('.').map(|n| n.parse().unwrap_or(0)).collect())
        .unwrap_or_default()
}

/// Najnowszy plik `<prefiks>*.AppImage` w katalogu (bez rozróżniania wielkości liter).
fn newest_appimage(dir: &Path, prefix: &str) -> Option<PathBuf> {
    let prefix = prefix.to_lowercase();
    fs::read_dir(dir).ok()?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let name = e.file_name().to_string_lossy().to_string();
            let lower = name.to_lowercase();
            if !lower.starts_with(&prefix) || !lower.ends_with(".appimage") || !e.path().is_file() {
                return None;
            }
            let mtime = e.metadata().ok()?.modified().ok()?;
            Some((appimage_version(&name), mtime, e.path()))
        })
        .max_by(|a, b| (&a.0, a.1).cmp(&(&b.0, b.1)))
        .map(|(_, _, p)| p)
}

/// Katalogi, w których ludzie zwykle trzymają pliki AppImage.
fn appimage_dirs() -> Vec<PathBuf> {
    let home = home_dir();
    ["Pulpit", "Desktop", "Aplikacje", "Applications", "Programy", ".local/bin", "bin",
     "Pobrane", "Downloads"]
        .iter().map(|d| home.join(d)).filter(|d| d.is_dir()).collect()
}

/// Polecenie z wersją w nazwie AppImage przestaje działać po aktualizacji programu
/// (`Mitcad-0.70.0-….AppImage` -> `Mitcad-0.71.0-….AppImage`). Gdy pliku nie ma,
/// szukamy najnowszego AppImage o tym samym początku nazwy w tym samym katalogu.
fn resolve_appimage(program: &str) -> Option<String> {
    let path = Path::new(program);
    let name = path.file_name()?.to_string_lossy().to_string();
    if path.exists() || !path.is_absolute() || !name.to_lowercase().ends_with(".appimage") {
        return None;
    }
    let stem = &name[..name.len() - ".appimage".len()];
    let prefix = stem.split(|c: char| c == '-' || c == '_' || c == '.').next().unwrap_or(stem);
    if prefix.is_empty() {
        return None;
    }
    newest_appimage(path.parent()?, prefix).map(|p| p.to_string_lossy().to_string())
}

/// Polecenie w cudzysłowie, jeśli ścieżka ma spacje itp.
fn quoted(path: &Path) -> String {
    shell_words::quote(&path.to_string_lossy()).to_string()
}

/// Rozszerzenia dla MitCad: otwiera `.f3d` z historią projektu, a do tego STEP i inne.
const MITCAD_EXTS: [&str; 9] = ["mitcad", "step", "stp", "iges", "igs", "stl", "3mf", "f3d", "f3z"];
/// Własne formaty programów (galeria otwiera je dwuklikiem zamiast STEP).
const NATIVE_EXTS: [(&str, &str); 3] = [("mitcad", "mitcad"), ("freecad", "fcstd"), ("solvespace", "slvs")];

/// Programy CAD znalezione w systemie.
fn detect_apps() -> Vec<AppEntry> {
    let entry = |name: &str, command: String, exts: &[&str]| AppEntry {
        name: name.into(),
        command,
        exts: exts.iter().map(|s| s.to_string()).collect(),
    };
    let cad_exts = ["step", "stp", "iges", "igs", "stl", "obj"];
    let mut apps = Vec::new();

    // MitCad: polecenie w PATH albo AppImage (np. na pulpicie).
    if on_path("mitcad") {
        apps.push(entry("MitCad", "mitcad %f".into(), &MITCAD_EXTS));
    } else if let Some(p) = appimage_dirs().iter().find_map(|d| newest_appimage(d, "mitcad")) {
        apps.push(entry("MitCad", format!("{} %f", quoted(&p)), &MITCAD_EXTS));
    }

    let mut freecad = None;
    for bin in ["freecad", "FreeCAD", "freecad-daily"] {
        if on_path(bin) {
            freecad = Some(format!("{bin} %f"));
            break;
        }
    }
    let home = home_dir();
    let flatpak_bins = [
        PathBuf::from("/var/lib/flatpak/exports/bin"),
        home.join(".local/share/flatpak/exports/bin"),
    ];
    let has_flatpak = |id: &str| flatpak_bins.iter().any(|d| d.join(id).exists());
    if freecad.is_none() && has_flatpak("org.freecad.FreeCAD") {
        freecad = Some("flatpak run org.freecad.FreeCAD %f".into());
    }
    if let Some(cmd) = freecad {
        let exts: Vec<&str> = std::iter::once("fcstd").chain(cad_exts).collect();
        apps.push(entry("FreeCAD", cmd, &exts));
    }
    if on_path("solvespace") {
        apps.push(entry("SolveSpace", "solvespace %f".into(), &["slvs"]));
    }
    apps
}

/// Dopisuje wykryte programy, których aplikacja jeszcze nie proponowała
/// i których nie ma już na liście (porównanie po nazwie). Zwraca true, gdy coś dodano.
/// Jednorazowo dopisuje własny format programu (np. `mitcad`, `fcstd`) na początek
/// jego rozszerzeń - w ustawieniach sprzed wersji 1.0.5 go nie było.
fn add_native_exts(settings: &mut Settings) -> bool {
    if settings.native_exts_added {
        return false;
    }
    settings.native_exts_added = true;
    for app in settings.apps.iter_mut() {
        let name = app.name.to_lowercase();
        if let Some((_, ext)) = NATIVE_EXTS.iter().find(|(n, _)| name.contains(n)) {
            if !app.exts.is_empty() && !app.exts.iter().any(|e| e == ext) {
                app.exts.insert(0, ext.to_string());
            }
        }
    }
    true
}

fn add_detected_apps(settings: &mut Settings) -> bool {
    if settings.apps_initialized && settings.apps_offered.is_empty() {
        // Ustawienia sprzed `apps_offered`: wtedy wykrywano tylko FreeCAD i SolveSpace.
        settings.apps_offered = vec!["FreeCAD".into(), "SolveSpace".into()];
    }
    settings.apps_initialized = true;
    let mut added = false;
    for app in detect_apps() {
        let key = app.name.to_lowercase();
        if settings.apps_offered.iter().any(|n| n.to_lowercase() == key) {
            continue;
        }
        settings.apps_offered.push(app.name.clone());
        if !settings.apps.iter().any(|a| a.name.to_lowercase() == key) {
            settings.apps.push(app);
            added = true;
        }
    }
    added
}

// ---------------------------------------------------------------------------
// DANE FOLDERU KOPII (to samo co synclocal_gallery.py)
// ---------------------------------------------------------------------------
fn read_json(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

/// Zapis przez plik tymczasowy, żeby przerwany zapis nie zepsuł pliku.
fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    let text = serde_json::to_string_pretty(value).map_err(|e| e.to_string())?;
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    fs::write(&tmp, text).map_err(|e| format!("Zapis {}: {e}", path.display()))?;
    fs::rename(&tmp, path).map_err(|e| format!("Zapis {}: {e}", path.display()))
}

/// Bezpieczna ścieżka do folderu projektu (tylko bezpośrednie podfoldery).
fn project_dir(vault: &Path, folder: &str) -> Result<PathBuf, String> {
    if folder.is_empty() || folder == "." || folder == ".." || folder.contains('/') || folder.contains('\\') {
        return Err("Nieprawidłowa nazwa folderu.".into());
    }
    let p = vault.join(folder);
    if !p.join("config.json").is_file() {
        return Err(format!("Nie ma projektu „{folder}”."));
    }
    Ok(p)
}

fn safe_file_name(name: &str) -> Result<&str, String> {
    if name.is_empty() || name == "." || name == ".." || name.contains('/') || name.contains('\\') {
        return Err("Nieprawidłowa nazwa pliku.".into());
    }
    Ok(name)
}

fn str_of(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

/// `" Meble / Krzesła "` -> `"Meble/Krzesła"` (podkategorie oddziela `/`).
fn clean_category(name: &str) -> String {
    let name = name.split(CAT_SEP).map(str::trim).filter(|p| !p.is_empty())
        .collect::<Vec<_>>().join("/");
    if name == NO_CATEGORY { String::new() } else { name }
}

/// Czy `cat` to `root` albo jedna z jej podkategorii.
fn in_subtree(cat: &str, root: &str) -> bool {
    cat == root || (cat.starts_with(root) && cat[root.len()..].starts_with(CAT_SEP))
}

fn category_of(meta: &Value) -> String {
    clean_category(&str_of(meta.get("tag")))
}

fn clean_tags(v: Option<&Value>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    if let Some(Value::Array(items)) = v {
        for t in items {
            let t = str_of(Some(t)).trim().to_string();
            if !t.is_empty() && !out.contains(&t) {
                out.push(t);
            }
        }
    }
    out
}

fn file_info(path: &Path, name: &str) -> Option<Value> {
    let md = fs::metadata(path).ok()?;
    if !md.is_file() {
        return None;
    }
    let mtime = md.modified().ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs()).unwrap_or(0);
    Some(json!({ "size": md.len(), "mtime": mtime, "name": name }))
}

fn base_name(s: &str) -> String {
    Path::new(s).file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()
}

fn project_item(vault: &Path, folder: &str, meta: &Value) -> Value {
    let dir = vault.join(folder);
    let mut files = Map::new();
    let pick = |key: &str, ext: &str| {
        let v = str_of(meta.get(key));
        if v.is_empty() { format!("{folder}.{ext}") } else { base_name(&v) }
    };
    for (kind, name) in [("f3d", pick("file", "f3d")), ("step", pick("step_file", "step")),
                         ("png", pick("thumbnail", "png"))] {
        if let Some(info) = file_info(&dir.join(&name), &name) {
            files.insert(kind.into(), info);
        }
    }
    // Pozostałe pliki projektu: zapisane przez programy na Linuksie (np. .mitcad, .FCStd)
    // i wersje z chmury czekające na decyzję („nazwa (chmura vN).f3d”).
    let known: Vec<String> = files.values().map(|v| str_of(v.get("name"))).collect();
    let mut other: Vec<Value> = fs::read_dir(&dir).map(|rd| rd.filter_map(|e| e.ok())
        .filter_map(|e| {
            let n = e.file_name().to_string_lossy().to_string();
            let skip = n == "config.json" || n.starts_with('.') || n.ends_with(".tmp") || known.contains(&n);
            if skip { None } else { file_info(&e.path(), &n) }
        }).collect()).unwrap_or_default();
    other.sort_by_key(|v| str_of(v.get("name")).to_lowercase());
    let versions = fs::read_dir(dir.join("versions"))
        .map(|rd| rd.filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().to_lowercase().ends_with(".f3d"))
            .count())
        .unwrap_or(0);
    let name = { let n = str_of(meta.get("displayname")); if n.is_empty() { folder.to_string() } else { n } };
    let version = meta.get("cloud_version").filter(|v| !v.is_null())
        .or_else(|| meta.get("version")).cloned().unwrap_or(Value::String(String::new()));
    json!({
        "folder": folder,
        "name": name,
        "category": category_of(meta),
        "tags": clean_tags(meta.get("tags")),
        "note": str_of(meta.get("note")),
        "favorite": meta.get("favorite").and_then(|v| v.as_bool()).unwrap_or(false),
        "cloud_project": str_of(meta.get("cloud_project")),
        "cloud_folder": str_of(meta.get("cloud_folder")),
        "version": version,
        "last_saved": str_of(meta.get("last_saved")),
        "reason": str_of(meta.get("last_save_reason")),
        "versions": versions,
        "files": files,
        "other": other,
        "conflict": meta.get("conflict").filter(|v| v.is_object()).cloned().unwrap_or(Value::Null),
    })
}

fn scan_projects(vault: &Path) -> Vec<Value> {
    let Ok(rd) = fs::read_dir(vault) else { return vec![] };
    let mut names: Vec<String> = rd.filter_map(|e| e.ok())
        .filter(|e| e.path().join("config.json").is_file())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    names.sort_by_key(|n| n.to_lowercase());
    names.iter()
        .filter_map(|n| {
            let meta = read_json(&vault.join(n).join("config.json"))?;
            meta.is_object().then(|| project_item(vault, n, &meta))
        })
        .collect()
}

fn clean_hotkey(key: &str) -> String {
    let key = key.trim().to_lowercase();
    if key.chars().count() == 1 && HOTKEYS.contains(key.as_str()) { key } else { String::new() }
}

/// Bez duplikatów; brakujące kategorie nadrzędne są dopisywane na końcu listy.
/// Skrót klawiszowy (key) może mieć tylko jedna kategoria.
fn clean_categories(list: Option<&Value>) -> Vec<Value> {
    // (nazwa, kolor, skrót) w kolejności z listy
    let mut cats: Vec<(String, String, String)> = Vec::new();
    if let Some(Value::Array(items)) = list {
        for c in items {
            let name = clean_category(&str_of(c.get("name")));
            if name.is_empty() { continue; }
            let (color, key) = (str_of(c.get("color")), clean_hotkey(&str_of(c.get("key"))));
            match cats.iter_mut().find(|x| x.0 == name) {
                Some(found) => {
                    if found.1.is_empty() { found.1 = color }
                    if found.2.is_empty() { found.2 = key }
                }
                None => cats.push((name, color, key)),
            }
        }
    }
    for k in 0..cats.len() {
        let parts: Vec<String> = cats[k].0.split(CAT_SEP).map(String::from).collect();
        for i in 1..parts.len() {
            let parent = parts[..i].join("/");
            if !cats.iter().any(|x| x.0 == parent) {
                cats.push((parent, String::new(), String::new()));
            }
        }
    }
    let mut used: Vec<String> = Vec::new();
    cats.into_iter().map(|(name, color, mut key)| {
        if !key.is_empty() && key != "-" {
            if used.contains(&key) { key.clear() } else { used.push(key.clone()) }
        }
        let mut item = json!({ "name": name, "color": color });
        if !key.is_empty() { item["key"] = Value::String(key); }
        item
    }).collect()
}

fn load_categories(vault: &Path) -> Vec<Value> {
    let data = read_json(&vault.join(CATEGORIES_FILE)).unwrap_or(json!({}));
    clean_categories(data.get("categories"))
}

fn save_categories(vault: &Path, cats: &Value) -> Result<Vec<Value>, String> {
    let clean = clean_categories(Some(cats));
    let path = vault.join(CATEGORIES_FILE);
    let mut data = read_json(&path).filter(|v| v.is_object()).unwrap_or(json!({}));
    data["categories"] = Value::Array(clean.clone());
    data["updated"] = Value::String(chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string());
    write_json(&path, &data)?;
    Ok(clean)
}

fn update_projects(vault: &Path, folders: &[String], ch: &Map<String, Value>) -> Result<Vec<Value>, String> {
    // Najpierw sprawdź wszystkie foldery, dopiero potem zapisuj.
    let dirs = folders.iter().map(|f| project_dir(vault, f)).collect::<Result<Vec<_>, _>>()?;
    let mut updated = Vec::new();
    for (folder, dir) in folders.iter().zip(dirs) {
        let cfg = dir.join("config.json");
        let mut meta = read_json(&cfg).filter(|v| v.is_object())
            .ok_or_else(|| format!("Nie da się odczytać config.json w „{folder}”."))?;
        if let Some(c) = ch.get("category") {
            let c = clean_category(&str_of(Some(c)));
            meta["tag"] = Value::String(if c.is_empty() { NO_CATEGORY.into() } else { c });
        }
        if let Some(t) = ch.get("tags") {
            meta["tags"] = json!(clean_tags(Some(t)));
        }
        let add = clean_tags(ch.get("add_tags"));
        let remove = clean_tags(ch.get("remove_tags"));
        if !add.is_empty() || !remove.is_empty() {
            let mut tags: Vec<String> = clean_tags(meta.get("tags")).into_iter()
                .filter(|t| !remove.contains(t)).collect();
            for t in add {
                if !tags.contains(&t) { tags.push(t); }
            }
            meta["tags"] = json!(tags);
        }
        if let Some(n) = ch.get("note") {
            meta["note"] = Value::String(str_of(Some(n)));
        }
        if let Some(f) = ch.get("favorite") {
            meta["favorite"] = Value::Bool(f.as_bool().unwrap_or(false));
        }
        write_json(&cfg, &meta)?;
        updated.push(project_item(vault, folder, &meta));
    }
    Ok(updated)
}

// ---------------------------------------------------------------------------
// KONFLIKT: plik zmieniony poza Fusion, a obok wersja z chmury (synclocal_files.py)
// ---------------------------------------------------------------------------
fn fingerprint(path: &Path) -> Option<Value> {
    let md = fs::metadata(path).ok()?;
    let mtime = md.modified().ok()?.duration_since(UNIX_EPOCH).ok()?.as_secs();
    Some(json!([md.len(), mtime]))
}

/// Wolna nazwa w `versions/` (z dopiskiem _1, _2… gdy zajęta).
fn versions_path(dir: &Path, base: &str) -> Result<PathBuf, String> {
    let versions = dir.join("versions");
    fs::create_dir_all(&versions).map_err(|e| e.to_string())?;
    let (stem, ext) = base.rsplit_once('.').unwrap_or((base, ""));
    let mut path = versions.join(base);
    let mut n = 1;
    while path.exists() {
        path = versions.join(format!("{stem}_{n}.{ext}"));
        n += 1;
    }
    Ok(path)
}

/// `use_cloud`: wersja z chmury staje się główną (plik zmieniony lokalnie idzie do versions/),
/// inaczej zostaje plik lokalny, a wersja z chmury idzie do versions/. Nic nie jest kasowane.
fn resolve_conflict(dir: &Path, use_cloud: bool) -> Result<Value, String> {
    let cfg = dir.join("config.json");
    let mut meta = read_json(&cfg).filter(|v| v.is_object()).ok_or("Nie da się odczytać config.json.")?;
    let conflict = meta.get("conflict").filter(|v| v.is_object()).cloned()
        .ok_or("Ten projekt nie ma konfliktu wersji.")?;
    let folder = base_name(&dir.to_string_lossy());
    let version = conflict.get("cloud_version").and_then(|v| v.as_i64()).unwrap_or(0);
    let stamp = chrono::Local::now().format("%Y%m%d_%H%M").to_string();
    let mut written = Vec::new();
    for name in conflict.get("files").and_then(|v| v.as_array()).cloned().unwrap_or_default() {
        let name = base_name(&str_of(Some(&name)));
        let src = dir.join(&name);
        if name.is_empty() || !src.is_file() { continue; }
        let ext = name.rsplit_once('.').map(|(_, e)| e.to_lowercase()).unwrap_or_default();
        let key = if ext == "f3d" { "file" } else { "step_file" };
        let main = { let m = base_name(&str_of(meta.get(key))); if m.is_empty() { format!("{folder}.{ext}") } else { m } };
        let main_path = dir.join(&main);
        if use_cloud {
            if main_path.is_file() {
                fs::rename(&main_path, versions_path(dir, &format!("{folder}_lokalna_{stamp}.{ext}"))?)
                    .map_err(|e| e.to_string())?;
            }
            fs::rename(&src, &main_path).map_err(|e| e.to_string())?;
            if ext == "step" { meta["step_file"] = Value::String(main.clone()); }
            written.push(main);
        } else {
            fs::rename(&src, versions_path(dir, &format!("{folder}_chmura_v{version}.{ext}"))?)
                .map_err(|e| e.to_string())?;
        }
    }
    let obj = meta.as_object_mut().ok_or("Nieprawidłowy config.json.")?;
    if use_cloud {
        let mut synced = obj.get("synced_files").and_then(|v| v.as_object()).cloned().unwrap_or_default();
        for name in written {
            if let Some(fp) = fingerprint(&dir.join(&name)) { synced.insert(name, fp); }
        }
        obj.insert("synced_files".into(), Value::Object(synced));
        obj.insert("cloud_version".into(), json!(version));
        let v = obj.get("version").and_then(|v| v.as_i64()).unwrap_or(0).max(version);
        obj.insert("version".into(), json!(v));
        obj.remove("conflict_kept_version");
    } else {
        obj.insert("conflict_kept_version".into(), json!(version));
    }
    obj.remove("conflict");
    write_json(&cfg, &meta)?;
    Ok(meta)
}

/// Zmienia nazwę (albo miejsce w drzewie) kategorii razem z jej podkategoriami.
/// Pusta nowa nazwa usuwa kategorię i jej podkategorie; ich projekty trafiają do kategorii
/// nadrzędnej (dla kategorii głównej: bez kategorii). Gdy nowa już istnieje, kategorie są łączone.
fn rename_category(vault: &Path, old: &str, new: &str) -> Result<usize, String> {
    let (old, new) = (clean_category(old), clean_category(new));
    if old.is_empty() {
        return Err("Brak nazwy kategorii.".into());
    }
    if new != old && !new.is_empty() && in_subtree(&new, &old) {
        return Err("Kategorii nie można przenieść do jej własnej podkategorii.".into());
    }
    let parent = old.rsplit_once(CAT_SEP).map(|(p, _)| p.to_string()).unwrap_or_default();
    let target = |cat: &str| -> String {
        if new.is_empty() { parent.clone() } else { format!("{}{}", new, &cat[old.len()..]) }
    };
    let mut changed = 0;
    for e in fs::read_dir(vault).map_err(|e| e.to_string())?.filter_map(|e| e.ok()) {
        let cfg = e.path().join("config.json");
        if !cfg.is_file() { continue; }
        let Some(mut meta) = read_json(&cfg).filter(|v| v.is_object()) else { continue };
        let cat = category_of(&meta);
        if !cat.is_empty() && in_subtree(&cat, &old) {
            let t = target(&cat);
            meta["tag"] = Value::String(if t.is_empty() { NO_CATEGORY.into() } else { t });
            write_json(&cfg, &meta)?;
            changed += 1;
        }
    }
    let cats = load_categories(vault);
    let keep: Vec<String> = cats.iter().map(|c| str_of(c.get("name")))
        .filter(|n| !in_subtree(n, &old)).collect();
    let mut out = Vec::new();
    for mut c in cats {
        let name = str_of(c.get("name"));
        if in_subtree(&name, &old) {
            let t = target(&name);
            if new.is_empty() || keep.contains(&t) { continue; }   // usunięcie albo scalenie
            c["name"] = Value::String(t);
        }
        out.push(c);
    }
    save_categories(vault, &Value::Array(out))?;
    Ok(changed)
}

// ---------------------------------------------------------------------------
// WPIS W MENU PROGRAMÓW
// ---------------------------------------------------------------------------
/// Nazwa pliku .desktop i ikony. Taka sama jak nazwa programu, bo pod nią okno zgłasza się
/// systemowi (WM_CLASS w X11, app_id w Waylandzie) - wtedy pasek zadań łączy okno z wpisem.
const DESKTOP_NAME: &str = "synclocal360-galeria";
const DESKTOP_MARK: &str = "X-SyncLocal360-Generated=true";
const MENU_ICONS: [(&str, &[u8]); 4] = [
    ("32x32", include_bytes!("../icons/32x32.png")),
    ("128x128", include_bytes!("../icons/128x128.png")),
    ("256x256", include_bytes!("../icons/128x128@2x.png")),
    ("512x512", include_bytes!("../icons/icon.png")),
];

fn data_home() -> PathBuf {
    std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).filter(|p| p.is_absolute())
        .unwrap_or_else(|| home_dir().join(".local/share"))
}

/// Ścieżka jako argument w Exec= (specyfikacja Desktop Entry: cudzysłów, \" \` \$ \\, %% ,
/// a potem jeszcze ogólne podwojenie ukośników wstecznych w wartościach tekstowych).
fn desktop_exec_arg(path: &str) -> String {
    let mut quoted = String::from("\"");
    for ch in path.chars() {
        match ch {
            '"' | '`' | '$' | '\\' => { quoted.push('\\'); quoted.push(ch); }
            '%' => quoted.push_str("%%"),
            _ => quoted.push(ch),
        }
    }
    quoted.push('"');
    quoted.replace('\\', "\\\\")
}

fn desktop_entry(appimage: &str) -> String {
    let exec = desktop_exec_arg(appimage);
    format!("[Desktop Entry]
Type=Application
Name=SyncLocal360 Galeria
GenericName=Galeria projektów CAD
Comment=Przeglądanie i porządkowanie lokalnych kopii projektów Fusion
Comment[en]=Browse and organize local copies of Fusion projects
Exec={exec} %f
TryExec={try_exec}
Icon={DESKTOP_NAME}
StartupWMClass={DESKTOP_NAME}
Terminal=false
Categories=Graphics;3DGraphics;Engineering;
Keywords=CAD;Fusion;STEP;f3d;galeria;kopia;backup;
{DESKTOP_MARK}
", try_exec = appimage.replace('\\', "\\\\"))
}

/// Uruchomiona z AppImage galeria dopisuje się do menu programów
/// (~/.local/share/applications + ikony w ~/.local/share/icons/hicolor). Przy każdym starcie
/// wpis jest aktualizowany, więc po pobraniu nowej wersji wskazuje nowy plik AppImage.
/// Pomija to, gdy ten sam plik AppImage zintegrowało już inne narzędzie (np. AppImageLauncher).
fn install_menu_entry(appimage: Option<PathBuf>, data: &Path) -> Result<bool, String> {
    let Some(appimage) = appimage.filter(|p| p.is_file()) else {
        return Ok(false);
    };
    let appimage = appimage.to_string_lossy().to_string();
    let apps = data.join("applications");
    let target = apps.join(format!("{DESKTOP_NAME}.desktop"));
    if let Ok(entries) = fs::read_dir(&apps) {
        for e in entries.filter_map(|e| e.ok()) {
            let p = e.path();
            if p == target || p.extension().map_or(true, |x| x != "desktop") { continue; }
            if fs::read_to_string(&p).is_ok_and(|t| t.lines().any(|l| l.starts_with("Exec=") && l.contains(&appimage))) {
                return Ok(false);
            }
        }
    }
    let content = desktop_entry(&appimage);
    if fs::read_to_string(&target).is_ok_and(|old| old == content) {
        return Ok(false);
    }
    for (size, png) in MENU_ICONS {
        let dir = data.join("icons/hicolor").join(size).join("apps");
        fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        fs::write(dir.join(format!("{DESKTOP_NAME}.png")), png).map_err(|e| e.to_string())?;
    }
    fs::create_dir_all(&apps).map_err(|e| e.to_string())?;
    let tmp = target.with_extension("desktop.tmp");
    fs::write(&tmp, content).map_err(|e| e.to_string())?;
    fs::rename(&tmp, &target).map_err(|e| e.to_string())?;
    // Odświeżenie menu (KDE i GNOME zwykle widzą zmianę same; brak narzędzi to nie błąd).
    for (prog, args) in [("update-desktop-database", vec![apps.to_string_lossy().to_string()]),
                         ("kbuildsycoca6", vec![]), ("kbuildsycoca5", vec![])] {
        if on_path(prog) {
            let mut cmd = Command::new(prog);
            cmd.args(&args).stdout(Stdio::null()).stderr(Stdio::null());
            clean_env(&mut cmd);
            let _ = cmd.spawn();
            if prog.starts_with("kbuildsycoca") { break; }
        }
    }
    Ok(true)
}

// ---------------------------------------------------------------------------
// URUCHAMIANIE PROGRAMÓW
// ---------------------------------------------------------------------------
/// AppImage dopisuje do zmiennych (LD_LIBRARY_PATH, PATH, XDG_DATA_DIRS…) swoje
/// katalogi. Uruchamiany program (np. FreeCAD) dostałby wtedy biblioteki z naszej
/// paczki i mógłby się wysypać, więc te wpisy usuwamy.
fn clean_env(cmd: &mut Command) {
    let Ok(appdir) = std::env::var("APPDIR") else { return };
    if appdir.is_empty() { return; }
    for (k, v) in std::env::vars() {
        if v.contains(&appdir) {
            let kept: Vec<&str> = v.split(':').filter(|p| !p.starts_with(&appdir)).collect();
            if kept.iter().all(|p| p.is_empty()) {
                cmd.env_remove(&k);
            } else {
                cmd.env(&k, kept.join(":"));
            }
        }
    }
    for k in ["APPDIR", "APPIMAGE", "ARGV0", "OWD"] {
        cmd.env_remove(k);
    }
}

fn spawn_detached(program: &str, args: &[String]) -> Result<(), String> {
    let mut cmd = Command::new(program);
    cmd.args(args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    clean_env(&mut cmd);
    match cmd.spawn() {
        Ok(mut child) => {
            std::thread::spawn(move || { let _ = child.wait(); });   // bez procesów-zombie
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound =>
            Err(format!("Nie znaleziono programu „{program}”. Sprawdź polecenie w ustawieniach.")),
        Err(e) => Err(format!("Nie udało się uruchomić „{program}”: {e}")),
    }
}

fn open_with_system(path: &Path) -> Result<(), String> {
    spawn_detached("xdg-open", &[path.to_string_lossy().to_string()])
}

/// FreeCAD otwierający plik z wiersza poleceń nie dopasowuje widoku: kamera zostaje
/// w punkcie (0,0,0) z kadrem ok. 4 mm, więc modele z Fusion (często daleko od zera)
/// są niewidoczne albo mocno przybliżone. Zamiast pliku podajemy mu więc jednorazowe
/// makro, które otwiera plik, ustawia widok izometryczny i „dopasuj wszystko”.
const FREECAD_FIT_EXTS: [&str; 6] = ["step", "stp", "iges", "igs", "brep", "brp"];

fn is_freecad(app: &AppEntry) -> bool {
    app.command.to_lowercase().contains("freecad")
}

fn freecad_open_macro(path: &Path) -> Result<PathBuf, String> {
    let home = std::env::var("HOME").map_err(|_| "Brak zmiennej HOME.".to_string())?;
    // Nie /tmp: FreeCAD z Flatpaka ma własny, prywatny /tmp.
    let dir = PathBuf::from(home).join(".cache/synclocal360");
    fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    // Sprzątanie makr, których FreeCAD nie usunął (np. zamknięty w trakcie startu).
    if let Ok(rd) = fs::read_dir(&dir) {
        for e in rd.filter_map(|e| e.ok()) {
            let old = e.metadata().ok().and_then(|m| m.modified().ok())
                .and_then(|t| t.elapsed().ok()).map(|d| d.as_secs() > 3600).unwrap_or(false);
            if old && e.file_name().to_string_lossy().starts_with("open_") {
                let _ = fs::remove_file(e.path());
            }
        }
    }
    let stamp = chrono::Local::now().format("%Y%m%d_%H%M%S_%f");
    let macro_path = dir.join(format!("open_{stamp}.py"));
    // Literał JSON jest też poprawnym napisem w Pythonie (\", \\, \uXXXX).
    let file_lit = serde_json::to_string(&path.to_string_lossy()).map_err(|e| e.to_string())?;
    let self_lit = serde_json::to_string(&macro_path.to_string_lossy()).map_err(|e| e.to_string())?;
    let label_lit = serde_json::to_string(
        &path.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default(),
    ).map_err(|e| e.to_string())?;
    let code = format!(r#"# -*- coding: utf-8 -*-
# SyncLocal360 Galeria: otwiera plik i dopasowuje widok (makro jednorazowe, usuwa się samo).
import os
import FreeCAD, FreeCADGui
try:
    os.remove({self_lit})
except OSError:
    pass
import ImportGui
ImportGui.open({file_lit})
_sl360_doc = FreeCAD.ActiveDocument
try:
    _sl360_doc.Label = {label_lit}
except Exception:
    pass

def _sl360_fit():
    try:
        view = FreeCADGui.getDocument(_sl360_doc.Name).activeView()
        view.viewIsometric()
        view.fitAll()
    except Exception as e:
        FreeCAD.Console.PrintWarning("SyncLocal360: nie udało się dopasować widoku: %s\n" % e)

from PySide import QtCore
QtCore.QTimer.singleShot(500, _sl360_fit)
"#);
    fs::write(&macro_path, code).map_err(|e| format!("{}: {e}", macro_path.display()))?;
    Ok(macro_path)
}

fn open_with_app(app: &AppEntry, path: &Path) -> Result<(), String> {
    let mut parts = shell_words::split(&app.command)
        .map_err(|e| format!("Błędne polecenie „{}”: {e}", app.command))?;
    if parts.is_empty() {
        return Err(format!("Program „{}” nie ma polecenia.", app.name));
    }
    let ext = path.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
    let macro_path;
    let path = if is_freecad(app) && FREECAD_FIT_EXTS.contains(&ext.as_str()) {
        macro_path = freecad_open_macro(path)?;
        macro_path.as_path()
    } else {
        path
    };
    let p = path.to_string_lossy().to_string();
    if parts.iter().any(|a| a.contains("%f")) {
        for a in parts.iter_mut() { *a = a.replace("%f", &p); }
    } else {
        parts.push(p);
    }
    let mut program = parts.remove(0);
    if let Some(newer) = resolve_appimage(&program) {
        program = newer;
    }
    spawn_detached(&program, &parts)
}

// ---------------------------------------------------------------------------
// KOMENDY DLA STRONY (odpowiedniki adresów /api/... z synclocal_gallery.py)
// ---------------------------------------------------------------------------
type Res = Result<Value, String>;

fn arg_str(args: &Value, key: &str) -> String { str_of(args.get(key)) }

#[tauri::command]
fn get_state(state: State<AppState>) -> Res {
    let s = state.settings.lock().unwrap().clone();
    let base = json!({
        "ok": true, "fusion": false, "app": "linux",
        "vault": s.vault, "recent": s.recent, "apps": s.apps, "dblclick": s.dblclick,
        "thumb_base": "thumb://localhost/",
    });
    let mut out = base;
    match state.vault() {
        Ok(v) => {
            out["categories"] = json!(load_categories(&v));
            out["projects"] = json!(scan_projects(&v));
        }
        Err(e) => {
            out["categories"] = json!([]);
            out["projects"] = json!([]);
            out["need_vault"] = json!(true);
            out["vault_error"] = json!(if s.vault.is_empty() { String::new() } else { e });
        }
    }
    Ok(out)
}

#[tauri::command]
fn update_projects_cmd(state: State<AppState>, args: Value) -> Res {
    let vault = state.vault()?;
    let folders: Vec<String> = match args.get("folders") {
        Some(Value::Array(a)) => a.iter().map(|v| str_of(Some(v))).collect(),
        _ => return Err("Nieprawidłowa lista projektów.".into()),
    };
    let changes = args.as_object().cloned().unwrap_or_default();
    let _g = state.write_lock.lock().unwrap();
    let items = update_projects(&vault, &folders, &changes)?;
    let cat = clean_category(&arg_str(&args, "category"));
    if args.get("category").is_some() && !cat.is_empty() {
        let mut cats = load_categories(&vault);
        if !cats.iter().any(|c| c["name"] == cat.as_str()) {
            cats.push(json!({ "name": cat, "color": "" }));
            save_categories(&vault, &Value::Array(cats))?;
        }
    }
    Ok(json!({ "ok": true, "projects": items, "categories": load_categories(&vault) }))
}

#[tauri::command]
fn save_categories_cmd(state: State<AppState>, args: Value) -> Res {
    let vault = state.vault()?;
    let _g = state.write_lock.lock().unwrap();
    let cats = save_categories(&vault, args.get("categories").unwrap_or(&Value::Null))?;
    Ok(json!({ "ok": true, "categories": cats }))
}

#[tauri::command]
fn rename_category_cmd(state: State<AppState>, args: Value) -> Res {
    let vault = state.vault()?;
    let _g = state.write_lock.lock().unwrap();
    let n = rename_category(&vault, &arg_str(&args, "old"), &arg_str(&args, "new"))?;
    Ok(json!({ "ok": true, "changed": n, "categories": load_categories(&vault) }))
}

#[tauri::command]
fn resolve_conflict_cmd(state: State<AppState>, args: Value) -> Res {
    let vault = state.vault()?;
    let folder = arg_str(&args, "folder");
    let dir = project_dir(&vault, &folder)?;
    let use_cloud = match arg_str(&args, "use").as_str() {
        "cloud" => true,
        "local" => false,
        _ => return Err("Nieznany wybór (cloud albo local).".into()),
    };
    let _g = state.write_lock.lock().unwrap();
    let meta = resolve_conflict(&dir, use_cloud)?;
    Ok(json!({ "ok": true, "projects": [project_item(&vault, &folder, &meta)] }))
}

#[tauri::command]
fn open_folder(state: State<AppState>, args: Value) -> Res {
    let vault = state.vault()?;
    let folder = arg_str(&args, "folder");
    let path = if folder.is_empty() { vault } else { project_dir(&vault, &folder)? };
    open_with_system(&path)?;
    Ok(json!({ "ok": true }))
}

#[tauri::command]
fn open_file(state: State<AppState>, args: Value) -> Res {
    let vault = state.vault()?;
    let dir = project_dir(&vault, &arg_str(&args, "folder"))?;
    let path = dir.join(safe_file_name(&arg_str(&args, "file"))?);
    if !path.is_file() {
        return Err("Plik nie istnieje.".into());
    }
    match args.get("app").and_then(|v| v.as_u64()) {
        Some(i) => {
            let app = state.settings.lock().unwrap().apps.get(i as usize).cloned()
                .ok_or("Nie ma takiego programu w ustawieniach.")?;
            open_with_app(&app, &path)?;
        }
        None => open_with_system(&path)?,
    }
    Ok(json!({ "ok": true }))
}

#[tauri::command]
async fn pick_vault(app: tauri::AppHandle, state: State<'_, AppState>) -> Res {
    let start = state.settings.lock().unwrap().vault.clone();
    let mut dialog = app.dialog().file().set_title("Wybierz folder z kopiami projektów");
    if !start.is_empty() && Path::new(&start).is_dir() {
        dialog = dialog.set_directory(&start);
    }
    let Some(picked) = dialog.blocking_pick_folder() else {
        return Ok(json!({ "ok": true, "cancelled": true }));
    };
    let path = picked.into_path().map_err(|e| e.to_string())?;
    state.set_vault(&path)?;
    Ok(json!({ "ok": true, "vault": path.to_string_lossy() }))
}

#[tauri::command]
fn set_vault(state: State<AppState>, args: Value) -> Res {
    let p = PathBuf::from(arg_str(&args, "path"));
    state.set_vault(&p)?;
    Ok(json!({ "ok": true, "vault": p.to_string_lossy() }))
}

#[tauri::command]
fn save_settings(state: State<AppState>, args: Value) -> Res {
    let mut apps: Vec<AppEntry> = Vec::new();
    if let Some(Value::Array(items)) = args.get("apps") {
        for a in items {
            let name = arg_str(a, "name").trim().to_string();
            let command = arg_str(a, "command").trim().to_string();
            if name.is_empty() || command.is_empty() { continue; }
            shell_words::split(&command).map_err(|e| format!("Błędne polecenie „{command}”: {e}"))?;
            let exts = match a.get("exts") {
                Some(Value::Array(x)) => x.iter()
                    .map(|e| str_of(Some(e)).trim().trim_start_matches('.').to_lowercase())
                    .filter(|e| !e.is_empty()).collect(),
                _ => vec![],
            };
            apps.push(AppEntry { name, command, exts });
        }
    }
    let dblclick = args.get("dblclick").and_then(|v| v.as_i64())
        .filter(|&i| i == -1 || (i >= 0 && (i as usize) < apps.len()));
    {
        let mut s = state.settings.lock().unwrap();
        s.apps = apps.clone();
        s.dblclick = dblclick;
    }
    state.save()?;
    Ok(json!({ "ok": true, "apps": apps, "dblclick": dblclick }))
}

// ---------------------------------------------------------------------------
// MINIATURY: thumb://localhost/<folder>/<plik>
// ---------------------------------------------------------------------------
fn thumb_response(state: &AppState, uri_path: &str) -> tauri::http::Response<Vec<u8>> {
    let not_found = || tauri::http::Response::builder().status(404).body(Vec::new()).unwrap();
    let decoded = percent_encoding::percent_decode_str(uri_path.trim_start_matches('/'))
        .decode_utf8_lossy().to_string();
    let mut parts = decoded.splitn(2, '/');
    let (Some(folder), Some(name)) = (parts.next(), parts.next()) else { return not_found() };
    let Ok(vault) = state.vault() else { return not_found() };
    let Ok(dir) = project_dir(&vault, folder) else { return not_found() };
    let Ok(name) = safe_file_name(name) else { return not_found() };
    let lower = name.to_lowercase();
    let ctype = if lower.ends_with(".png") { "image/png" }
        else if lower.ends_with(".jpg") || lower.ends_with(".jpeg") { "image/jpeg" }
        else { return not_found() };
    match fs::read(dir.join(name)) {
        Ok(bytes) => tauri::http::Response::builder()
            .status(200)
            .header("Content-Type", ctype)
            .header("Cache-Control", "max-age=3600")
            .body(bytes).unwrap(),
        Err(_) => not_found(),
    }
}

// ---------------------------------------------------------------------------
// START
// ---------------------------------------------------------------------------
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let settings_path = app.path().app_config_dir()?.join("settings.json");
            let mut settings: Settings = read_json(&settings_path)
                .and_then(|v| serde_json::from_value(v).ok())
                .unwrap_or_default();
            add_detected_apps(&mut settings);
            add_native_exts(&mut settings);
            if let Err(e) = install_menu_entry(std::env::var_os("APPIMAGE").map(PathBuf::from), &data_home()) {
                eprintln!("Nie udało się dodać galerii do menu programów: {e}");
            }
            let state = AppState { settings: Mutex::new(settings), settings_path, write_lock: Mutex::new(()) };
            // Folder podany w wierszu poleceń ma pierwszeństwo.
            if let Some(arg) = std::env::args().nth(1).filter(|a| Path::new(a).is_dir()) {
                let _ = state.set_vault(Path::new(&arg));
            } else {
                let _ = state.save();
            }
            app.manage(state);
            Ok(())
        })
        .register_asynchronous_uri_scheme_protocol("thumb", |ctx, request, responder| {
            let app = ctx.app_handle().clone();
            let path = request.uri().path().to_string();
            std::thread::spawn(move || {
                let state = app.state::<AppState>();
                responder.respond(thumb_response(&state, &path));
            });
        })
        .invoke_handler(tauri::generate_handler![
            get_state, update_projects_cmd, save_categories_cmd, rename_category_cmd, resolve_conflict_cmd,
            open_folder, open_file, pick_vault, set_vault, save_settings
        ])
        .run(tauri::generate_context!())
        .expect("błąd uruchomienia aplikacji SyncLocal360 Galeria");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn freecad_macro_is_valid_python() {
        let p = Path::new("/tmp/Ząb \"koła\" 'x' \\ test/Mała obudowa.step");
        let m = freecad_open_macro(p).unwrap();
        let code = fs::read_to_string(&m).unwrap();
        assert!(code.contains("Mała obudowa"));
        let ok = Command::new("python3").args(["-m", "py_compile"]).arg(&m).status().unwrap().success();
        let _ = fs::remove_file(&m);
        assert!(ok, "makro nie kompiluje się w Pythonie");
    }
}

#[cfg(test)]
mod category_tests {
    use super::*;

    fn vault_with(cats: &[&str], projects: &[(&str, &str)]) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("sl360_cat_{}_{}", std::process::id(),
            std::time::SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()));
        fs::create_dir_all(&dir).unwrap();
        let list: Vec<Value> = cats.iter().map(|c| json!({ "name": c, "color": "#111111" })).collect();
        save_categories(&dir, &Value::Array(list)).unwrap();
        for (folder, tag) in projects {
            fs::create_dir_all(dir.join(folder)).unwrap();
            write_json(&dir.join(folder).join("config.json"), &json!({ "tag": tag })).unwrap();
        }
        dir
    }
    fn tag(dir: &Path, folder: &str) -> String {
        str_of(read_json(&dir.join(folder).join("config.json")).unwrap().get("tag"))
    }
    fn names(dir: &Path) -> Vec<String> {
        load_categories(dir).iter().map(|c| str_of(c.get("name"))).collect()
    }

    #[test]
    fn clean_names_and_parents() {
        assert_eq!(clean_category(" Meble / Krzesła /"), "Meble/Krzesła");
        assert_eq!(clean_category("unassigned"), "");
        assert!(in_subtree("Meble/Krzesła", "Meble"));
        assert!(!in_subtree("Meblościanki", "Meble"));
        let c = clean_categories(Some(&json!([
            { "name": "A/B/C" }, { "name": "A", "color": "#1" }, { "name": "A/B/C", "color": "#2" }])));
        assert_eq!(c, vec![json!({ "name": "A/B/C", "color": "#2" }), json!({ "name": "A", "color": "#1" }),
            json!({ "name": "A/B", "color": "" })]);
        // skróty: małe litery, zajęte i niedozwolone (f, r, 0) wypadają, "-" = brak
        let c = clean_categories(Some(&json!([
            { "name": "A", "key": "X" }, { "name": "B", "key": "x" }, { "name": "C", "key": "f" },
            { "name": "D", "key": "-" }, { "name": "E", "key": "0" }, { "name": "F", "key": "7" }])));
        let keys: Vec<String> = c.iter().map(|x| str_of(x.get("key"))).collect();
        assert_eq!(keys, ["x", "", "", "-", "", "7"]);
    }

    #[test]
    fn rename_moves_subtree_and_delete_goes_to_parent() {
        let d = vault_with(&["Meble", "Meble/Krzesła", "Meble/Krzesła/Biurowe", "CNC"],
            &[("a", "Meble"), ("b", "Meble/Krzesła"), ("c", "Meble/Krzesła/Biurowe"), ("e", "Meblościanki")]);
        assert_eq!(rename_category(&d, "Meble", "Dom/Meble").unwrap(), 3);
        assert_eq!(tag(&d, "c"), "Dom/Meble/Krzesła/Biurowe");
        assert_eq!(tag(&d, "e"), "Meblościanki");
        assert_eq!(names(&d), ["Dom/Meble", "Dom/Meble/Krzesła", "Dom/Meble/Krzesła/Biurowe", "CNC", "Dom"]);
        assert!(rename_category(&d, "Dom", "Dom/X").is_err());
        assert_eq!(rename_category(&d, "Dom/Meble/Krzesła", "").unwrap(), 2);
        assert_eq!(tag(&d, "b"), "Dom/Meble");
        assert_eq!(tag(&d, "c"), "Dom/Meble");
        assert_eq!(names(&d), ["Dom/Meble", "CNC", "Dom"]);
        // przeniesienie pod kategorię z dalszej części listy nie gubi jej koloru
        assert_eq!(rename_category(&d, "Dom/Meble", "CNC/Meble").unwrap(), 3);
        let cnc = load_categories(&d).into_iter().find(|c| c["name"] == "CNC").unwrap();
        assert_eq!(cnc["color"], "#111111");
        assert_eq!(rename_category(&d, "CNC", "").unwrap(), 3);
        assert_eq!(tag(&d, "a"), NO_CATEGORY);
        assert_eq!(names(&d), ["Dom"]);
        let _ = fs::remove_dir_all(&d);
    }
}

#[cfg(test)]
mod conflict_tests {
    use super::*;

    fn setup(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("sl360_conf_{}_{name}", std::process::id())).join("Obudowa");
        let _ = fs::remove_dir_all(dir.parent().unwrap());
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("Obudowa.f3d"), b"lokalna-zmieniona").unwrap();
        fs::write(dir.join("Obudowa (chmura v7).f3d"), b"z-chmury").unwrap();
        fs::write(dir.join("Obudowa.mitcad"), b"m").unwrap();
        write_json(&dir.join("config.json"), &json!({
            "file": "Obudowa.f3d", "cloud_version": 6, "version": 6,
            "synced_files": { "Obudowa.f3d": [3, 0] },
            "conflict": { "cloud_version": 7, "files": ["Obudowa (chmura v7).f3d"] }
        })).unwrap();
        dir
    }

    #[test]
    fn use_cloud_and_keep_local() {
        let d = setup("cloud");
        let item = project_item(d.parent().unwrap(), "Obudowa", &read_json(&d.join("config.json")).unwrap());
        let other: Vec<String> = item["other"].as_array().unwrap().iter().map(|v| str_of(v.get("name"))).collect();
        assert_eq!(other, ["Obudowa (chmura v7).f3d", "Obudowa.mitcad"]);
        assert_eq!(item["conflict"]["cloud_version"], 7);
        let m = resolve_conflict(&d, true).unwrap();
        assert_eq!(fs::read(d.join("Obudowa.f3d")).unwrap(), b"z-chmury");
        assert!(!d.join("Obudowa (chmura v7).f3d").exists());
        let kept: Vec<_> = fs::read_dir(d.join("versions")).unwrap().filter_map(|e| e.ok()).collect();
        assert_eq!(kept.len(), 1);
        assert_eq!(fs::read(kept[0].path()).unwrap(), b"lokalna-zmieniona");
        assert_eq!(m["cloud_version"], 7);
        assert_eq!(m["synced_files"]["Obudowa.f3d"][0], 8);
        assert!(m.get("conflict").is_none());
        assert!(resolve_conflict(&d, true).is_err());

        let d = setup("local");
        let m = resolve_conflict(&d, false).unwrap();
        assert_eq!(fs::read(d.join("Obudowa.f3d")).unwrap(), b"lokalna-zmieniona");
        assert!(d.join("versions/Obudowa_chmura_v7.f3d").is_file());
        assert_eq!(m["conflict_kept_version"], 7);
        assert_eq!(m["cloud_version"], 6);
        let _ = fs::remove_dir_all(d.parent().unwrap());
    }

    #[test]
    fn native_exts_added_once() {
        let mut s = Settings {
            apps: vec![
                AppEntry { name: "MitCad".into(), command: "m %f".into(), exts: vec!["f3d".into(), "step".into()] },
                AppEntry { name: "FreeCAD".into(), command: "f %f".into(), exts: vec!["step".into()] },
                AppEntry { name: "Wszystko".into(), command: "x %f".into(), exts: vec![] },
            ],
            ..Default::default()
        };
        assert!(add_native_exts(&mut s));
        assert_eq!(s.apps[0].exts, ["mitcad", "f3d", "step"]);
        assert_eq!(s.apps[1].exts, ["fcstd", "step"]);
        assert!(s.apps[2].exts.is_empty());
        s.apps[0].exts.remove(0);
        assert!(!add_native_exts(&mut s));
        assert_eq!(s.apps[0].exts, ["f3d", "step"]);
    }
}

#[cfg(test)]
mod menu_tests {
    use super::*;

    #[test]
    fn exec_quoting() {
        assert_eq!(desktop_exec_arg("/home/a/Pulpit/Galeria 1.0.AppImage"), "\"/home/a/Pulpit/Galeria 1.0.AppImage\"");
        assert_eq!(desktop_exec_arg("/x/100%$a\"b.AppImage"), r#""/x/100%%\\$a\\"b.AppImage""#);
        let d = desktop_entry("/home/a/Pulpit/SyncLocal360-Galeria-1.0.2-x86_64.AppImage");
        assert!(d.contains("Exec=\"/home/a/Pulpit/SyncLocal360-Galeria-1.0.2-x86_64.AppImage\" %f\n"));
        assert!(d.contains("TryExec=/home/a/Pulpit/SyncLocal360-Galeria-1.0.2-x86_64.AppImage\n"));
    }

    #[test]
    fn installs_and_updates_entry() {
        let dir = std::env::temp_dir().join(format!("sl360_menu_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let data = dir.join("share");
        let (v1, v2) = (dir.join("Galeria-1.0.1.AppImage"), dir.join("Galeria-1.0.2.AppImage"));
        fs::create_dir_all(&dir).unwrap();
        fs::write(&v1, b"").unwrap();
        fs::write(&v2, b"").unwrap();
        let entry = data.join("applications/synclocal360-galeria.desktop");
        assert!(!install_menu_entry(None, &data).unwrap());
        assert!(install_menu_entry(Some(v1.clone()), &data).unwrap());
        assert!(fs::read_to_string(&entry).unwrap().contains("Galeria-1.0.1.AppImage"));
        assert!(data.join("icons/hicolor/256x256/apps/synclocal360-galeria.png").is_file());
        assert!(!install_menu_entry(Some(v1.clone()), &data).unwrap());          // bez zmian
        assert!(install_menu_entry(Some(v2.clone()), &data).unwrap());           // nowa wersja
        assert!(fs::read_to_string(&entry).unwrap().contains("Galeria-1.0.2.AppImage"));
        // ten sam plik zintegrowany już przez inne narzędzie: nie dubluj
        fs::write(data.join("applications/appimagekit_x.desktop"),
            format!("[Desktop Entry]\nExec={} %U\n", v1.display())).unwrap();
        assert!(!install_menu_entry(Some(v1), &data).unwrap());
        let _ = fs::remove_dir_all(&dir);
    }
}

#[cfg(test)]
mod appimage_tests {
    use super::*;

    #[test]
    fn version_from_name() {
        assert_eq!(appimage_version("Mitcad-0.70.0-x86_64.AppImage"), vec![0, 70, 0]);
        assert_eq!(appimage_version("solidcraft-0.45.0-linux-x86_64.AppImage"), vec![0, 45, 0]);
        assert!(appimage_version("solidcraft.AppImage").is_empty());
    }

    #[test]
    fn finds_newer_appimage() {
        let dir = std::env::temp_dir().join(format!("sl360_appimage_{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        for n in ["Mitcad-0.9.0-x86_64.AppImage", "Mitcad-0.70.0-x86_64.AppImage",
                  "solidcraft-0.45.0-linux-x86_64.AppImage", "Inny-1.0.AppImage"] {
            fs::write(dir.join(n), b"").unwrap();
        }
        let old = dir.join("Mitcad-0.69.0-x86_64.AppImage").to_string_lossy().to_string();
        let sc = dir.join("solidcraft.AppImage").to_string_lossy().to_string();
        let mc = resolve_appimage(&old).unwrap();
        let sc = resolve_appimage(&sc).unwrap();
        let existing = resolve_appimage(&mc);
        let _ = fs::remove_dir_all(&dir);
        assert!(mc.ends_with("Mitcad-0.70.0-x86_64.AppImage"), "{mc}");
        assert!(sc.ends_with("solidcraft-0.45.0-linux-x86_64.AppImage"), "{sc}");
        assert!(existing.is_none());
        assert!(resolve_appimage("mitcad").is_none());
    }

    #[test]
    fn detected_apps_added_once() {
        let mut s = Settings { apps_initialized: true, ..Default::default() };
        s.apps.push(AppEntry { name: "SolidCraft".into(), command: "x %f".into(), exts: vec![] });
        add_detected_apps(&mut s);
        assert!(!s.apps.iter().any(|a| a.name == "FreeCAD"), "FreeCAD był już kiedyś proponowany");
        let n = s.apps.len();
        s.apps.retain(|a| a.name != "MitCad");
        add_detected_apps(&mut s);
        assert!(s.apps.len() <= n && !s.apps.iter().any(|a| a.name == "MitCad"), "usunięty nie wraca");
    }
}

#[cfg(test)]
mod manual {
    /// Ręcznie: cargo test -- --ignored --nocapture detect
    #[test]
    #[ignore]
    fn detect() {
        for a in super::detect_apps() {
            println!("{} | {} | {:?}", a.name, a.command, a.exts);
        }
    }

    /// Ręcznie: SL360_FILE=... cargo test -- --ignored --nocapture make_macro
    #[test]
    #[ignore]
    fn make_macro() {
        let f = std::env::var("SL360_FILE").unwrap();
        println!("MACRO={}", super::freecad_open_macro(std::path::Path::new(&f)).unwrap().display());
    }
}
