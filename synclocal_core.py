# -*- coding: utf-8 -*-
"""
SyncLocal360 - rdzeń: ustawienia, zapis lokalny, autozapis.

Na bazie dodatku NoCloud2 autorstwa Kamila Sokalskiego
(https://modelowanie3d.org), wydanego na licencji otwartej z wymogiem
uznania autorstwa. Funkcje zapisu (save_current_document_to_vault,
resolve_unique_folder itd.) pochodzą z oryginalnego nocloud_core.py.
"""

import adsk.core, adsk.fusion, traceback
import os, json, shutil, threading, datetime, uuid, tempfile

ADDIN_NAME = "SyncLocal360"
ADDIN_PATH = os.path.dirname(os.path.realpath(__file__))
SETTINGS_PATH = os.path.join(ADDIN_PATH, "synclocal_settings.json")

DEFAULT_SETTINGS = {
    "vault_dir": os.path.join(os.path.expanduser("~"), "SyncLocal360"),
    "autosave_enabled": False,
    "autosave_interval_min": 10,
    "versioning_enabled": False,
    "ask_name_enabled": False,
    "notify_on_save": True,
    "backup_step": False,
}

UNNAMED_DOC_NAMES = ("Unnamed", "Bez nazwy", "Bez tytułu", "Untitled")

# Grupa atrybutu z identyfikatorem projektu zapisywanym w pliku Fusion.
# Celowo zostaje "NoCloud", żeby projekty zapisane wcześniej przez NoCloud2
# trafiały do tych samych folderów (bez duplikatów).
DOC_ATTR_GROUP = "NoCloud"
DOC_ATTR_NAME = "doc_id"


# ---------------------------------------------------------------------------
# NARZĘDZIA
# ---------------------------------------------------------------------------
def sanitize_filename(name, keep="._- "):
    return "".join(c for c in name.strip() if c.isalnum() or c in keep) or "Unnamed"


def _ascii_temp_root():
    """Katalog tymczasowy, którego ścieżka ma tylko znaki ASCII (albo None)."""
    for base in (tempfile.gettempdir(), os.environ.get("PUBLIC", ""), "C:\\Users\\Public"):
        if base and base.isascii() and os.path.isdir(base):
            return base
    return None


def save_via_ascii_path(final_path, save_func):
    """Zapisuje plik funkcją API Fusion, omijając problem polskich znaków w ścieżce.

    Część funkcji Fusion (np. DataObject.saveToFile dla miniatur z chmury) po cichu
    nic nie zapisuje, gdy ścieżka zawiera znaki spoza ASCII (ł, ę, ó, Å, cyrylica…).
    Wtedy zapisujemy pod tymczasową nazwą ASCII i przenosimy plik na miejsce.
    save_func(path) ma zapisać plik pod podaną ścieżką. Zwraca True, jeśli plik powstał."""
    if final_path.isascii():
        try:
            save_func(final_path)
        except Exception:
            pass
        return os.path.isfile(final_path)

    root = _ascii_temp_root()
    if root:
        tmp = os.path.join(root, f"synclocal360_{uuid.uuid4().hex}{os.path.splitext(final_path)[1]}")
        try:
            save_func(tmp)
            if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                shutil.move(tmp, final_path)
                return True
        except Exception:
            pass
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    try:
        save_func(final_path)          # ostatnia próba wprost
    except Exception:
        pass
    return os.path.isfile(final_path)


def resolve_unique_folder(vault_dir, safe_name, doc_id=None):
    """Zwraca nazwę folderu dla dokumentu. Używa istniejącego folderu, jeśli
    należy do tego samego dokumentu (doc_id w config.json). Jeśli folder o tej
    nazwie należy do INNEGO dokumentu, dokleja _1, _2, ..."""
    candidate = safe_name
    counter = 1
    while True:
        folder_path = os.path.join(vault_dir, candidate)
        config_path = os.path.join(folder_path, "config.json")
        if not os.path.isdir(folder_path):
            return candidate
        try:
            # Pusty folder (np. po przerwanym zapisie) nie należy do nikogo - używamy go.
            if not os.listdir(folder_path):
                return candidate
        except OSError:
            pass
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                if doc_id and meta.get("doc_id") == doc_id:
                    return candidate
            except Exception:
                pass
        candidate = f"{safe_name}_{counter}"
        counter += 1


# ---------------------------------------------------------------------------
# USTAWIENIA
# ---------------------------------------------------------------------------
def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    return settings


def save_settings(settings):
    try:
        merged = load_settings()
        merged.update(settings)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def get_vault_dir():
    vault_dir = load_settings().get("vault_dir") or DEFAULT_SETTINGS["vault_dir"]
    os.makedirs(vault_dir, exist_ok=True)
    return vault_dir


# ---------------------------------------------------------------------------
# ZAPIS AKTYWNEGO DOKUMENTU
# ---------------------------------------------------------------------------
def get_or_create_doc_id(design):
    attrs = design.attributes
    existing = attrs.itemByName(DOC_ATTR_GROUP, DOC_ATTR_NAME)
    if existing:
        return existing.value
    new_id = str(uuid.uuid4())
    attrs.add(DOC_ATTR_GROUP, DOC_ATTR_NAME, new_id)
    return new_id


def save_current_document_to_vault(reason="manual"):
    try:
        app = adsk.core.Application.get()
        settings = load_settings()
        design = adsk.fusion.Design.cast(app.activeProduct)
        doc = design.parentDocument if design else None
        if doc is None:
            return False, "Brak aktywnego projektu Fusion (Design)."

        raw_name = doc.name or "Unnamed"
        if raw_name.lower().endswith(".f3d"):
            raw_name = raw_name[:-4]
        base_safe_name = sanitize_filename(raw_name)
        vault_dir = get_vault_dir()
        doc_id = get_or_create_doc_id(design)
        safe_name = resolve_unique_folder(vault_dir, base_safe_name, doc_id=doc_id)
        target_dir = os.path.join(vault_dir, safe_name)
        os.makedirs(target_dir, exist_ok=True)

        config_path = os.path.join(target_dir, "config.json")
        meta = {}
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}

        version = meta.get("version", 0) + 1
        versioning = settings.get("versioning_enabled", False)

        if versioning:
            versions_dir = os.path.join(target_dir, "versions")
            os.makedirs(versions_dir, exist_ok=True)
            f3d_path = os.path.join(versions_dir, f"{safe_name}_v{version}.f3d")
            latest_path = os.path.join(target_dir, f"{safe_name}.f3d")
        else:
            f3d_path = os.path.join(target_dir, f"{safe_name}.f3d")
            latest_path = f3d_path

        export_mgr = design.exportManager
        archive_options = export_mgr.createFusionArchiveExportOptions(f3d_path, design.rootComponent)
        export_mgr.execute(archive_options)

        if versioning and f3d_path != latest_path:
            shutil.copyfile(f3d_path, latest_path)

        png_path = os.path.join(target_dir, f"{safe_name}.png")

        def _capture(path):
            try:
                save_options = adsk.core.SaveImageFileOptions.create(path)
                save_options.width = 400
                save_options.height = 400
                save_options.isBackgroundTransparent = True
                if app.activeViewport.saveAsImageFileWithOptions(save_options):
                    return
            except Exception:
                pass
            app.activeViewport.saveAsImageFile(path, 400, 400)

        save_via_ascii_path(png_path, _capture)

        meta.update({
            "folder": safe_name,
            "doc_id": doc_id,
            "displayname": raw_name,
            "file": f"{safe_name}.f3d",
            "thumbnail": f"{safe_name}.png",
            "last_saved": datetime.datetime.now().isoformat(timespec="seconds"),
            "version": version,
            "versioning_enabled": versioning,
            "last_save_reason": reason,
        })
        if not str(meta.get("tag") or "").strip():
            meta["tag"] = "unassigned"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        return True, safe_name
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# ODCZYT FOLDERU (przyda się np. do przyszłej galerii)
# ---------------------------------------------------------------------------
def list_versions(target_dir, safe_name):
    """Lista {"version": n, "path": ...} z podfolderu versions/, najnowsze pierwsze."""
    versions_dir = os.path.join(target_dir, "versions")
    result = []
    if os.path.isdir(versions_dir):
        prefix = f"{safe_name}_v"
        for fname in os.listdir(versions_dir):
            if fname.startswith(prefix) and fname.endswith(".f3d"):
                try:
                    version_num = int(fname[len(prefix):-4])
                except ValueError:
                    continue
                result.append({"version": version_num, "path": os.path.join(versions_dir, fname)})
    result.sort(key=lambda v: v["version"], reverse=True)
    return result


def scan_vault():
    """Wszystkie zapisane projekty: zawartość config.json + lista wersji."""
    vault_dir = get_vault_dir()
    items = []
    for entry in sorted(os.listdir(vault_dir)):
        folder_path = os.path.join(vault_dir, entry)
        config_path = os.path.join(folder_path, "config.json")
        if not os.path.isdir(folder_path) or not os.path.isfile(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        item = {"folder": entry, **meta}
        item["versions"] = list_versions(folder_path, entry)
        items.append(item)
    return items


def import_from_vault(folder_name):
    """Wstawia zapisany projekt jako komponent do aktywnego projektu."""
    try:
        app = adsk.core.Application.get()
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            return False, "Brak aktywnego projektu."
        root = design.rootComponent
        f3d_path = os.path.join(get_vault_dir(), folder_name, f"{folder_name}.f3d")
        if not os.path.isfile(f3d_path):
            return False, "Plik nie istnieje na dysku."
        import_mgr = app.importManager
        options = import_mgr.createFusionArchiveImportOptions(f3d_path)
        occ_before = list(root.occurrences)
        import_mgr.importToTarget2(options, root)
        new_occ = next((o for o in root.occurrences if o not in occ_before), None)
        if new_occ:
            new_occ.component.name = folder_name
        return True, folder_name
    except Exception as e:
        return False, str(e)


def open_from_vault(folder_name):
    """Otwiera zapisany projekt jako nowy dokument (bez wiązania z chmurą)."""
    try:
        app = adsk.core.Application.get()
        f3d_path = os.path.join(get_vault_dir(), folder_name, f"{folder_name}.f3d")
        if not os.path.isfile(f3d_path):
            return False, "Plik nie istnieje na dysku."
        import_mgr = app.importManager
        options = import_mgr.createFusionArchiveImportOptions(f3d_path)
        import_mgr.importToNewDocument(options)
        return True, folder_name
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# AUTOZAPIS
# Timer działa w osobnym wątku, więc sam zapis wykonujemy w wątku głównym
# Fusion przez custom event (API Fusion nie jest bezpieczne wątkowo).
# ---------------------------------------------------------------------------
AUTOSAVE_EVENT_ID = "SyncLocal360_AutosaveEvent"
_autosave_timer = None
_autosave_event = None
_handlers = []


def _fire_autosave_event():
    try:
        adsk.core.Application.get().fireCustomEvent(AUTOSAVE_EVENT_ID, "")
    except Exception:
        pass


def _schedule_next_tick(interval_min):
    global _autosave_timer
    if _autosave_timer:
        _autosave_timer.cancel()
    _autosave_timer = threading.Timer(max(1, float(interval_min)) * 60, _fire_autosave_event)
    _autosave_timer.daemon = True
    _autosave_timer.start()


class _AutosaveEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        settings = load_settings()
        try:
            if settings.get("autosave_enabled"):
                app = adsk.core.Application.get()
                if adsk.fusion.Design.cast(app.activeProduct) is not None:
                    save_current_document_to_vault(reason="autosave")
        except Exception:
            pass
        _schedule_next_tick(settings.get("autosave_interval_min", 10))


def restart_autosave_timer():
    _schedule_next_tick(load_settings().get("autosave_interval_min", 10))


def start_autosave():
    global _autosave_event
    app = adsk.core.Application.get()
    try:
        app.unregisterCustomEvent(AUTOSAVE_EVENT_ID)
    except Exception:
        pass
    _autosave_event = app.registerCustomEvent(AUTOSAVE_EVENT_ID)
    handler = _AutosaveEventHandler()
    _autosave_event.add(handler)
    _handlers.append(handler)
    restart_autosave_timer()


def stop_autosave():
    global _autosave_timer, _autosave_event
    if _autosave_timer:
        _autosave_timer.cancel()
        _autosave_timer = None
    try:
        adsk.core.Application.get().unregisterCustomEvent(AUTOSAVE_EVENT_ID)
    except Exception:
        pass
    _autosave_event = None
    _handlers.clear()
