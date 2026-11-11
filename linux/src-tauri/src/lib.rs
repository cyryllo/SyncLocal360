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
//!   * `<folder kopii>/synclocal_gallery.json`: lista kategorii (nazwa, kolor).

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
    /// Program na dwuklik: indeks w `apps`, -1 = program domyślny systemu,
    /// brak = pierwszy program pasujący do rozszerzenia.
    #[serde(default)]
    dblclick: Option<i64>,
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

/// Przy pierwszym uruchomieniu dopisuje znalezione programy CAD.
fn detect_apps() -> Vec<AppEntry> {
    let cad = |name: &str, command: String| AppEntry {
        name: name.into(),
        command,
        exts: ["step", "stp", "iges", "igs", "stl", "obj"].iter().map(|s| s.to_string()).collect(),
    };
    let mut apps = Vec::new();
    for bin in ["freecad", "FreeCAD", "freecad-daily"] {
        if on_path(bin) {
            apps.push(cad("FreeCAD", format!("{bin} %f")));
            break;
        }
    }
    let home = std::env::var("HOME").unwrap_or_default();
    let flatpak_bins = [
        PathBuf::from("/var/lib/flatpak/exports/bin"),
        PathBuf::from(&home).join(".local/share/flatpak/exports/bin"),
    ];
    let has_flatpak = |id: &str| flatpak_bins.iter().any(|d| d.join(id).exists());
    if apps.is_empty() && has_flatpak("org.freecad.FreeCAD") {
        apps.push(cad("FreeCAD", "flatpak run org.freecad.FreeCAD %f".into()));
    }
    if on_path("solvespace") {
        apps.push(AppEntry { name: "SolveSpace".into(), command: "solvespace %f".into(),
                             exts: vec!["slvs".into()] });
    }
    apps
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

fn category_of(meta: &Value) -> String {
    let tag = str_of(meta.get("tag")).trim().to_string();
    if tag == NO_CATEGORY { String::new() } else { tag }
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

fn clean_categories(list: Option<&Value>) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::new();
    if let Some(Value::Array(items)) = list {
        for c in items {
            let name = str_of(c.get("name")).trim().to_string();
            if name.is_empty() || name == NO_CATEGORY || out.iter().any(|o| o["name"] == name.as_str()) {
                continue;
            }
            out.push(json!({ "name": name, "color": str_of(c.get("color")) }));
        }
    }
    out
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
            let c = str_of(Some(c)).trim().to_string();
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

/// Zmienia nazwę kategorii we wszystkich projektach; pusta nowa nazwa usuwa kategorię.
fn rename_category(vault: &Path, old: &str, new: &str) -> Result<usize, String> {
    let (old, new) = (old.trim(), new.trim());
    if old.is_empty() {
        return Err("Brak nazwy kategorii.".into());
    }
    let mut changed = 0;
    for e in fs::read_dir(vault).map_err(|e| e.to_string())?.filter_map(|e| e.ok()) {
        let cfg = e.path().join("config.json");
        if !cfg.is_file() { continue; }
        let Some(mut meta) = read_json(&cfg).filter(|v| v.is_object()) else { continue };
        if category_of(&meta) == old {
            meta["tag"] = Value::String(if new.is_empty() { NO_CATEGORY.into() } else { new.into() });
            write_json(&cfg, &meta)?;
            changed += 1;
        }
    }
    let mut cats = load_categories(vault);
    if !new.is_empty() && cats.iter().any(|c| c["name"] == new) {
        cats.retain(|c| c["name"] != old);                  // scalenie z istniejącą
    } else {
        for c in cats.iter_mut() {
            if c["name"] == old { c["name"] = Value::String(new.into()); }
        }
        cats.retain(|c| !str_of(c.get("name")).is_empty());
    }
    save_categories(vault, &Value::Array(cats))?;
    Ok(changed)
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
    let program = parts.remove(0);
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
    let cat = arg_str(&args, "category").trim().to_string();
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
            if !settings.apps_initialized {
                settings.apps = detect_apps();
                settings.apps_initialized = true;
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
            get_state, update_projects_cmd, save_categories_cmd, rename_category_cmd,
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
mod manual {
    /// Ręcznie: SL360_FILE=... cargo test -- --ignored --nocapture make_macro
    #[test]
    #[ignore]
    fn make_macro() {
        let f = std::env::var("SL360_FILE").unwrap();
        println!("MACRO={}", super::freecad_open_macro(std::path::Path::new(&f)).unwrap().display());
    }
}
