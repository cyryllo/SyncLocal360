# -*- coding: utf-8 -*-
"""
SyncLocal360 - ochrona plików zmienionych poza Fusion.

Część dodatku SyncLocal360 (Cyryl Sochacki), opartego na dodatku NoCloud2
autorstwa Kamila Sokalskiego (https://modelowanie3d.org), wydanym na licencji
otwartej z wymogiem uznania autorstwa.

Po każdym zapisie kopii dodatek zapamiętuje w config.json rozmiar i datę
zapisanych plików (pole "synced_files"). Jeśli przy następnej aktualizacji
plik na dysku wygląda inaczej, to znaczy, że ktoś go zmienił poza Fusion
(np. na Linuksie w MitCad) - wtedy kopia NIE jest nadpisywana, a nowa wersja
z chmury trafia obok jako "<nazwa> (chmura vN).f3d" (pole "conflict").
Użytkownik wybiera w galerii, którą wersję zostawić (resolve_conflict).

Moduł nie importuje adsk - używa go też galeria uruchamiana bez Fusion.
Ten sam format obsługuje aplikacja na Linuksa (linux/src-tauri/src/lib.rs).
"""

import os
import json
import shutil
import datetime

SYNCED_KEY = 'synced_files'
CONFLICT_KEY = 'conflict'
KEPT_KEY = 'conflict_kept_version'   # wersja z chmury, przy której użytkownik zostawił swoją
MTIME_TOLERANCE = 2                  # s (różne systemy plików zaokrąglają czas)
LEGACY_MARGIN = 120                  # s - kopie bez odcisków: plik nowszy niż last_saved + margines


def fingerprint(path):
    st = os.stat(path)
    return [int(st.st_size), int(st.st_mtime)]


def record_synced(target_dir, meta, names):
    """Zapamiętuje odciski plików, które właśnie zapisał dodatek."""
    synced = meta.get(SYNCED_KEY)
    if not isinstance(synced, dict):
        synced = {}
    for name in names:
        path = os.path.join(target_dir, name)
        if os.path.isfile(path):
            synced[name] = fingerprint(path)
    meta[SYNCED_KEY] = synced


def _parse_time(value):
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return None


def changed_locally(target_dir, meta, names):
    """Lista plików (z names), które zmieniono poza dodatkiem od jego ostatniego zapisu."""
    synced = meta.get(SYNCED_KEY)
    legacy_time = _parse_time(meta.get('last_saved'))
    out = []
    for name in names:
        path = os.path.join(target_dir, name)
        if not os.path.isfile(path):
            continue
        size, mtime = fingerprint(path)
        old = synced.get(name) if isinstance(synced, dict) else None
        if isinstance(old, list) and len(old) == 2:
            if old[0] != size or abs(int(old[1]) - mtime) > MTIME_TOLERANCE:
                out.append(name)
        elif isinstance(synced, dict) and synced:
            # Ten plik nie był zapisany przez dodatek (np. ktoś go dołożył) - nie nadpisujemy.
            out.append(name)
        elif legacy_time is not None and mtime > legacy_time + LEGACY_MARGIN:
            # Starsza kopia bez odcisków: plik zmieniony wyraźnie po ostatnim zapisie.
            out.append(name)
    return out


def conflict_name(folder, version, ext):
    return f'{folder} (chmura v{version}).{ext}'


def main_file(meta, folder, ext):
    """Nazwa głównego pliku danego typu w folderze projektu."""
    key = 'file' if ext == 'f3d' else 'step_file'
    name = os.path.basename(str(meta.get(key) or ''))
    return name or f'{folder}.{ext}'


def _versions_path(project_dir, base):
    versions = os.path.join(project_dir, 'versions')
    os.makedirs(versions, exist_ok=True)
    stem, ext = os.path.splitext(base)
    path = os.path.join(versions, base)
    n = 1
    while os.path.exists(path):
        path = os.path.join(versions, f'{stem}_{n}{ext}')
        n += 1
    return path


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def resolve_conflict(project_dir, use):
    """Rozwiązuje konflikt w folderze projektu i zwraca nowy config.json.

    use == 'cloud': wersja z chmury staje się główną, zmieniony lokalnie plik
                    trafia do versions/<folder>_lokalna_<data>.<ext>,
    use == 'local': zostaje plik zmieniony lokalnie, wersja z chmury trafia do
                    versions/<folder>_chmura_v<N>.<ext>; kolejne aktualizacje z chmury
                    nadal go nie nadpiszą (nowa wersja z chmury znów pojawi się obok).
    Nic nie jest kasowane."""
    if use not in ('cloud', 'local'):
        raise ValueError('Nieznany wybór (cloud albo local).')
    config_path = os.path.join(project_dir, 'config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    conflict = meta.get(CONFLICT_KEY)
    if not isinstance(conflict, dict):
        raise ValueError('Ten projekt nie ma konfliktu wersji.')
    folder = os.path.basename(project_dir.rstrip('/\\'))
    version = int(conflict.get('cloud_version') or 0)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M')
    written = []
    for name in conflict.get('files') or []:
        name = os.path.basename(str(name))
        src = os.path.join(project_dir, name)
        if not name or not os.path.isfile(src):
            continue
        ext = name.rsplit('.', 1)[-1].lower()
        main = main_file(meta, folder, ext)
        main_path = os.path.join(project_dir, main)
        if use == 'cloud':
            if os.path.isfile(main_path):
                shutil.move(main_path, _versions_path(project_dir, f'{folder}_lokalna_{stamp}.{ext}'))
            os.replace(src, main_path)
            written.append(main)
            if ext == 'step':
                meta['step_file'] = main
        else:
            shutil.move(src, _versions_path(project_dir, f'{folder}_chmura_v{version}.{ext}'))
    if use == 'cloud':
        record_synced(project_dir, meta, written)
        meta['cloud_version'] = version
        meta['version'] = max(int(meta.get('version') or 0), version)
        meta.pop(KEPT_KEY, None)
    else:
        meta[KEPT_KEY] = version
    meta.pop(CONFLICT_KEY, None)
    _write_json(config_path, meta)
    return meta
