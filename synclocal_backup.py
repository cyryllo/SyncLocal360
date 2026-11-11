# -*- coding: utf-8 -*-
"""
SyncLocal360 - Kopia zapasowa z chmury (wsadowy zapis wybranych projektów).

Część dodatku SyncLocal360, opartego na dodatku NoCloud2 autorstwa
Kamila Sokalskiego (https://modelowanie3d.org), wydanym na licencji
otwartej z wymogiem uznania autorstwa.

Co robi:
  * pokazuje okno z listą projektów z chmury Fusion (aktywny hub),
  * pozwala zaznaczyć całe projekty albo pojedyncze pliki w projekcie,
  * po zatwierdzeniu po kolei otwiera w tle każdy plik, eksportuje go do .f3d
    (opcjonalnie także .step) w folderze SyncLocal360 (ten sam układ co "Szybki
    Zapis": <nazwa>/config.json, <nazwa>.f3d, <nazwa>.png, versions/),
    pobiera miniaturę z chmury i zamyka plik,
  * zapisuje log z przebiegu kopii w folderze docelowym.
"""

import adsk.core, adsk.fusion, traceback
import os, json, datetime, time, base64

import synclocal_core

CMD_ID = 'SyncLocal360_CloudBackup'
EVENT_ID = 'SyncLocal360_CloudBackupEvent'

handlers = []          # referencje do handlerów (muszą żyć przez cały czas działania)
_state = {}            # stan aktualnie otwartego okna dialogowego
_pending_job = None    # zadanie przekazywane z okna do workera (custom event)
_backup_event = None


# ---------------------------------------------------------------------------
# POMOCNICZE
# ---------------------------------------------------------------------------
# Projekty-biblioteki, które Fusion pokazuje w hubie obok projektów użytkownika
# (gotowe części Autodesku). Kopia jest możliwa, ale zwykle niepotrzebna.
LIBRARY_PROJECT_NAMES = {'standard components'}
LIBRARY_SUFFIX = '  (biblioteka Autodesk – zwykle niepotrzebna)'


def _is_library_project(project):
    try:
        return project.name.strip().lower() in LIBRARY_PROJECT_NAMES
    except Exception:
        return False


def _error_text(exc):
    text = str(exc).strip() or exc.__class__.__name__
    return ' '.join(text.split())


def _list_project_files(project, errors=None):
    """Zwraca listę (DataFile, [ścieżka folderów]) dla wszystkich plików .f3d
    w projekcie, rekurencyjnie przez wszystkie podfoldery.

    Każdy folder jest czytany osobno: błąd w jednym (np. brak uprawnień)
    nie przerywa listowania reszty. Opisy błędów trafiają do listy `errors`."""
    result = []
    if errors is None:
        errors = []
    try:
        project_name = project.name
    except Exception:
        project_name = '?'

    def report(path, what, exc):
        where = '/'.join([project_name] + path)
        errors.append(f'{where}: nie udało się odczytać {what} - {_error_text(exc)}')

    def walk(folder, path):
        try:
            files = list(folder.dataFiles)
        except Exception as e:
            files = []
            report(path, 'listy plików', e)
        for f in files:
            try:
                if f.fileExtension == 'f3d':
                    result.append((f, path))
            except Exception as e:
                report(path, 'pliku', e)

        try:
            subfolders = list(folder.dataFolders)
        except Exception as e:
            subfolders = []
            report(path, 'listy podfolderów', e)
        for sub in subfolders:
            try:
                name = sub.name
            except Exception:
                name = '?'
            walk(sub, path + [name])
        adsk.doEvents()

    try:
        root = project.rootFolder
    except Exception as e:
        report([], 'folderu głównego projektu', e)
        return result
    if root is None:
        errors.append(f'{project_name}: Fusion nie zwrócił folderu głównego projektu (brak dostępu?)')
        return result

    walk(root, [])
    result.sort(key=lambda item: ('/'.join(item[1]) + '/' + item[0].name).lower())
    return result


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _build_folder_index(dest_dir):
    """Jednorazowy spis folderu kopii: id pliku w chmurze -> nazwa folderu.
    (cloud_id z tej kopii albo doc_id - tak zapisywał migrator NoCloud2.)"""
    index = {}
    if os.path.isdir(dest_dir):
        for entry in os.listdir(dest_dir):
            meta = _read_json(os.path.join(dest_dir, entry, 'config.json'))
            for key in ('doc_id', 'cloud_id'):   # cloud_id ma pierwszeństwo
                if meta.get(key):
                    index[meta[key]] = entry
    return index


def _find_folder_for_cloud_file(dest_dir, base_name, cloud_id, index=None):
    """Zwraca folder, który już należy do tego pliku z chmury. Jeśli go nie ma,
    zwraca nową, unikalną nazwę, która nie nadpisze innych projektów o tej samej nazwie."""
    if index is None:
        index = _build_folder_index(dest_dir)
    folder = index.get(cloud_id)
    if folder and os.path.isdir(os.path.join(dest_dir, folder)):
        return folder
    return synclocal_core.resolve_unique_folder(dest_dir, base_name, doc_id=cloud_id)


def _find_open_document(app, data_file):
    """Jeśli dana wersja pliku jest już otwarta w Fusion, zwraca ten dokument
    (żeby go nie otwierać drugi raz i - co ważniejsze - nie zamknąć go użytkownikowi)."""
    for doc in app.documents:
        try:
            df = doc.dataFile
            if df and df.id == data_file.id and df.versionNumber == data_file.versionNumber:
                return doc
        except Exception:
            continue
    return None


def _save_cloud_thumbnail(data_file, png_path, timeout=20):
    """Pobiera miniaturę wygenerowaną przez chmurę Fusion (nie wymaga
    pokazywania dokumentu na ekranie). Zwraca True, jeśli plik powstał.

    Najpierw jako dane (base64) zapisywane przez Pythona: DataObject.saveToFile
    po cichu nic nie zapisuje, gdy ścieżka ma polskie znaki (stąd brak miniatur
    m.in. dla „Bałwanek”, „Grzałka_400W”, „płyta na sciane…”)."""
    try:
        future = data_file.thumbnail
        start = time.time()
        while future.state == adsk.core.FutureStates.ProcessingFutureState:
            if time.time() - start > timeout:
                return False
            adsk.doEvents()
            time.sleep(0.05)
        if future.state != adsk.core.FutureStates.FinishedFutureState or not future.dataObject:
            return False
        data_obj = future.dataObject
        try:
            raw = base64.b64decode(data_obj.getAsBase64String() or '')
            if raw:
                with open(png_path, 'wb') as f:
                    f.write(raw)
                return True
        except Exception:
            pass
        return synclocal_core.save_via_ascii_path(png_path, data_obj.saveToFile)
    except Exception:
        return False


def _save_thumbnail(app, png_path, fit_view):
    """Zrzut z widoku 3D - tylko gdy dokument jest widoczny na ekranie."""
    try:
        viewport = app.activeViewport
        if fit_view:
            viewport.fit()
    except Exception:
        return

    def _capture(path):
        try:
            options = adsk.core.SaveImageFileOptions.create(path)
            options.width = 400
            options.height = 400
            options.isBackgroundTransparent = True
            if viewport.saveAsImageFileWithOptions(options):
                return
        except Exception:
            pass
        viewport.saveAsImageFile(path, 400, 400)

    synclocal_core.save_via_ascii_path(png_path, _capture)


def _open_in_background(app, version_file):
    """Otwiera plik z chmury bez pokazywania go (bez nowej karty). Gdyby Fusion
    odmówił otwarcia w tle, otwiera normalnie (widocznie)."""
    try:
        doc = app.documents.open(version_file, False)
        if doc is not None:
            return doc
    except Exception:
        pass
    doc = app.documents.open(version_file, True)
    if doc is None:
        raise RuntimeError('Nie udało się otworzyć pliku z chmury.')
    return doc


def _export_version(app, version_file, out_path, thumb_path=None, step_path=None):
    """Otwiera (w tle) wersję pliku z chmury, eksportuje ją do .f3d
    (i opcjonalnie .step) i zamyka."""
    doc = _find_open_document(app, version_file)
    opened_here = doc is None
    if opened_here:
        doc = _open_in_background(app, version_file)
    try:
        design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
        if design is None:
            raise RuntimeError('Plik nie zawiera modelu (Design).')

        export_mgr = design.exportManager
        if out_path:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            options = export_mgr.createFusionArchiveExportOptions(out_path, design.rootComponent)
            if not export_mgr.execute(options) and not os.path.isfile(out_path):
                raise RuntimeError('Eksport .f3d nie powiódł się.')

        if step_path:
            os.makedirs(os.path.dirname(step_path), exist_ok=True)
            step_options = export_mgr.createSTEPExportOptions(step_path, design.rootComponent)
            if not export_mgr.execute(step_options) and not os.path.isfile(step_path):
                raise RuntimeError('Eksport .step nie powiódł się.')

        if thumb_path and not _save_cloud_thumbnail(version_file, thumb_path):
            # Awaryjnie: zrzut z widoku, ale tylko jeśli dokument jest na ekranie.
            visible = False
            try:
                visible = doc.isVisible
            except Exception:
                pass
            if visible:
                try:
                    doc.activate()
                except Exception:
                    pass
                # Widok dopasowujemy tylko w plikach, które sami otworzyliśmy -
                # nie ruszamy kamery w dokumentach, na których użytkownik pracuje.
                _save_thumbnail(app, thumb_path, fit_view=opened_here)
    finally:
        if opened_here:
            try:
                doc.close(False)
            except Exception:
                pass


def backup_data_file(app, data_file, project_name, folder_path, job):
    """Zapisuje najnowszą wersję jednego pliku z chmury do folderu SyncLocal360.
    Zwraca (status, opis), status: 'ok' | 'skip'.

    Jeśli .f3d jest już aktualny, a brakuje tylko .step, eksportowany jest
    wyłącznie .step (plik i tak trzeba otworzyć w tle, ale bez ponownego
    zapisu .f3d i miniatury)."""
    dest_dir = job['dest']
    base_name = synclocal_core.sanitize_filename(data_file.name)
    index = job.get('folder_index')
    folder = _find_folder_for_cloud_file(dest_dir, base_name, data_file.id, index)
    target_dir = os.path.join(dest_dir, folder)
    config_path = os.path.join(target_dir, 'config.json')
    f3d_path = os.path.join(target_dir, f'{folder}.f3d')
    png_path = os.path.join(target_dir, f'{folder}.png')
    step_path = os.path.join(target_dir, f'{folder}.step')
    want_step = bool(job.get('step'))

    meta = _read_json(config_path)
    latest_version = data_file.versionNumber

    # Czy to, co leży na dysku, to już najnowsza wersja z chmury?
    # (cloud_id/cloud_version - z tej kopii; doc_id/version - z migratora NoCloud2)
    same_file = data_file.id in (meta.get('cloud_id'), meta.get('doc_id'))
    saved_version = int(meta.get('cloud_version') or (meta.get('version') if same_file else 0) or 0)
    up_to_date = job['skip_existing'] and same_file and saved_version >= latest_version
    f3d_ok = up_to_date and os.path.isfile(f3d_path)
    step_ok = (not want_step) or (up_to_date and os.path.isfile(step_path))

    if f3d_ok and step_ok:
        if os.path.isfile(png_path):
            return 'skip', f'aktualny (v{latest_version})'
        # Brakuje tylko miniatury (np. wcześniejszy błąd z polskimi znakami w nazwie):
        # pobieramy ją z chmury bez otwierania dokumentu.
        if _save_cloud_thumbnail(data_file, png_path):
            meta['thumbnail'] = f'{folder}.png'
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
            return 'ok', f'{folder} (v{latest_version}, tylko miniatura)'
        return 'skip', f'aktualny (v{latest_version}), chmura nie dała miniatury'

    os.makedirs(target_dir, exist_ok=True)
    try:
        _export_version(
            app, data_file,
            out_path=None if f3d_ok else f3d_path,
            thumb_path=None if (f3d_ok and os.path.isfile(png_path)) else png_path,
            step_path=None if step_ok else step_path,
        )
    except Exception:
        # Nie zostawiamy pustych folderów po nieudanym zapisie.
        try:
            if not os.listdir(target_dir):
                os.rmdir(target_dir)
        except OSError:
            pass
        raise

    if not os.path.isfile(f3d_path):
        raise RuntimeError('Nie powstał plik .f3d.')
    if want_step and not os.path.isfile(step_path):
        raise RuntimeError('Nie powstał plik .step.')

    meta.update({
        'folder': folder,
        'doc_id': meta.get('doc_id') or data_file.id,
        'displayname': data_file.name,
        'file': f'{folder}.f3d',
        'thumbnail': f'{folder}.png' if os.path.isfile(png_path) else '',
        'last_saved': datetime.datetime.now().isoformat(timespec='seconds'),
        'version': max(int(meta.get('version') or 0), latest_version),
        'last_save_reason': 'cloud_backup',
        'cloud_id': data_file.id,
        'cloud_project': project_name,
        'cloud_folder': '/'.join(folder_path),
        'cloud_version': latest_version,
    })
    meta.setdefault('versioning_enabled', False)
    if os.path.isfile(step_path):
        meta['step_file'] = f'{folder}.step'
    if not str(meta.get('tag') or '').strip():
        meta['tag'] = 'unassigned'
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    if index is not None:
        index[data_file.id] = folder

    what = 'tylko .step' if f3d_ok else ('.f3d + .step' if want_step else '.f3d')
    return 'ok', f'{folder} (v{latest_version}, {what})'


# ---------------------------------------------------------------------------
# WORKER - wykonywany poza komendą (custom event), bo otwieranie/zamykanie
# dokumentów wewnątrz execute komendy bywa w Fusion niestabilne.
# ---------------------------------------------------------------------------
class _LiveLog:
    """Log dopisywany na bieżąco - jeśli Fusion się zawiesi w trakcie dużej kopii,
    w pliku zostaje wszystko do ostatniego zapisanego pliku. Na końcu nagłówek
    jest uzupełniany o podsumowanie (plik zapisywany od nowa)."""

    def __init__(self, path, header):
        self.path = path
        self.header = header
        self.lines = []
        self.ok = True
        self._write_all(self.header + ['(KOPIA W TRAKCIE - jeśli ten napis został, kopia nie dobiegła końca)', '-' * 70])

    def _write_all(self, lines):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
        except Exception:
            self.ok = False

    def add(self, line):
        self.lines.append(line)
        try:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            self.ok = False

    def finish(self, summary_line):
        self._write_all(self.header + [summary_line, '-' * 70] + self.lines)


def run_backup(job):
    app = adsk.core.Application.get()
    ui = app.userInterface
    os.makedirs(job['dest'], exist_ok=True)

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = os.path.join(job['dest'], f'backup_log_{stamp}.txt')
    n = 1
    while os.path.exists(log_path):
        log_path = os.path.join(job['dest'], f'backup_log_{stamp}_{n}.txt')
        n += 1
    log = _LiveLog(log_path, [
        f'SyncLocal360 - kopia zapasowa z chmury, {datetime.datetime.now().isoformat(timespec="seconds")}',
        f'Folder docelowy: {job["dest"]}',
        'Format: .f3d' + (' + .step' if job.get('step') else '') + ' (najnowsza wersja)',
    ])

    original_doc = app.activeDocument
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.cancelButtonText = 'Przerwij'
    progress.isBackgroundTranslucent = False
    progress.show('SyncLocal360 - kopia zapasowa', 'Przygotowanie listy plików...', 0, 1, 0)
    job['cancel_check'] = lambda: progress.wasCancelled

    # 1. Zbieramy listę plików (całe projekty listujemy dopiero tutaj).
    entries = list(job['files'])
    listing_errors = []
    for project in job['projects']:
        if progress.wasCancelled:
            break
        progress.message = f'Wczytywanie listy plików: {project.name}'
        adsk.doEvents()
        try:
            for data_file, path in _list_project_files(project, listing_errors):
                entries.append((project.name, path, data_file))
        except Exception as e:
            listing_errors.append(f'{project.name}: nie udało się odczytać projektu - {_error_text(e)}')

    seen = set()
    unique = []
    for entry in entries:
        key = entry[2].id if entry[2] is not None else ('err', entry[0])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    entries = unique

    # 2. Zapis plików po kolei.
    total = len(entries)
    progress.maximumValue = max(total, 1)
    progress.message = 'Odczyt folderu kopii...'
    adsk.doEvents()
    job['folder_index'] = _build_folder_index(job['dest'])
    counts = {'ok': 0, 'skip': 0, 'err': len(listing_errors)}
    for msg in listing_errors:
        log.add(f'[BŁĄD] {msg}')
    cancelled = False

    for i, (project_name, path, data_file) in enumerate(entries):
        if progress.wasCancelled:
            cancelled = True
            break

        location = '/'.join([project_name] + path + [data_file.name])
        progress.progressValue = i
        progress.message = f'({i + 1}/{total}) {location}'
        adsk.doEvents()

        try:
            status, info = backup_data_file(app, data_file, project_name, path, job)
            counts[status] += 1
            log.add(f"[{'OK' if status == 'ok' else 'POMINIĘTO'}] {location} -> {info}")
        except Exception as e:
            counts['err'] += 1
            log.add(f'[BŁĄD] {location}: {e}\n        {traceback.format_exc(limit=2).strip()}')
        adsk.doEvents()

    if progress.wasCancelled:
        cancelled = True
    progress.hide()

    try:
        if original_doc and original_doc.isValid:
            original_doc.activate()
    except Exception:
        pass

    # 3. Log + podsumowanie.
    log.finish(f'Zapisano: {counts["ok"]}, pominięto: {counts["skip"]}, błędy: {counts["err"]}'
               + (' (PRZERWANO)' if cancelled else ''))
    log_path = log.path if log.ok else '(nie udało się zapisać logu)'

    summary = (
        ('Kopia przerwana przez użytkownika.\n\n' if cancelled else 'Kopia zakończona.\n\n')
        + f'Zapisano: {counts["ok"]}\nPominięto (już aktualne): {counts["skip"]}\nBłędy: {counts["err"]}\n\n'
        + f'Folder: {job["dest"]}\nLog: {log_path}'
    )
    ui.messageBox(summary, 'SyncLocal360 - kopia zapasowa')


class BackupEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _pending_job
        job, _pending_job = _pending_job, None
        if not job:
            return
        try:
            run_backup(job)
        except Exception:
            ui = adsk.core.Application.get().userInterface
            ui.messageBox('Błąd kopii zapasowej:\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# OKNO DIALOGOWE
# ---------------------------------------------------------------------------
def _load_files_into_group(pidx):
    """Dodaje do grupy projektu checkboxy z pojedynczymi plikami."""
    project = _state['projects'][pidx]
    group = _state['groups'][pidx]
    children = group.children

    # Pobieranie listy plików z chmury może chwilę trwać - pokazujemy wskaźnik.
    progress = None
    try:
        progress = adsk.core.Application.get().userInterface.createProgressDialog()
        progress.isCancelButtonShown = False
        progress.show('SyncLocal360', f'Wczytywanie listy plików: {project.name}...', 0, 0, 0)
        adsk.doEvents()
    except Exception:
        progress = None
    listing_errors = []
    try:
        files = _list_project_files(project, listing_errors)
    finally:
        if progress:
            progress.hide()

    if listing_errors:
        shown = listing_errors[:5] + ([f'... i {len(listing_errors) - 5} więcej'] if len(listing_errors) > 5 else [])
        children.addTextBoxCommandInput(f'bkErr_{pidx}', '', '<b>Błędy odczytu:</b><br>' + '<br>'.join(shown),
                                        min(len(shown) + 1, 6), True)

    entries = []
    if not files:
        children.addTextBoxCommandInput(f'bkEmpty_{pidx}', '', 'Brak plików Fusion (.f3d) w tym projekcie.', 1, True)
    else:
        children.addBoolValueInput(f'bkAll_{pidx}', 'Wszystkie pliki projektu', True, '', True)
        for fidx, (data_file, path) in enumerate(files):
            label = '/'.join(path + [data_file.name]) + f'  (v{data_file.versionNumber})'
            input_id = f'bkFile_{pidx}_{fidx}'
            children.addBoolValueInput(input_id, label, True, '', True)
            entries.append((input_id, data_file, path))
    _state['files'][pidx] = entries


class BackupInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            changed = args.input
            cid = changed.id
            inputs = args.firingEvent.sender.commandInputs
            ui = adsk.core.Application.get().userInterface

            if cid == 'bkBrowse':
                dialog = ui.createFolderDialog()
                dialog.title = 'Wybierz folder na kopię zapasową'
                if dialog.showDialog() == adsk.core.DialogResults.DialogOK:
                    inputs.itemById('bkDir').value = dialog.folder

            elif cid in ('bkSelectAll', 'bkSelectNone'):
                selected = (cid == 'bkSelectAll')
                for pidx, check in enumerate(_state.get('checks', [])):
                    # „Zaznacz wszystkie” pomija biblioteki Autodesk (można je zaznaczyć ręcznie).
                    value = selected and not _is_library_project(_state['projects'][pidx])
                    check.value = value
                    _state['groups'][pidx].isVisible = value

            elif cid.startswith('bkSel_'):
                # Zaznaczenie projektu pokazuje pod nim opcję wyboru pojedynczych plików.
                pidx = int(cid.split('_')[1])
                _state['groups'][pidx].isVisible = changed.value

            elif cid.startswith('bkLoad_'):
                pidx = int(cid.split('_')[1])
                changed.isVisible = False
                _load_files_into_group(pidx)
                _state['groups'][pidx].isExpanded = True

            elif cid.startswith('bkAll_'):
                pidx = int(cid.split('_')[1])
                group = _state['groups'][pidx]
                for input_id, _df, _path in _state['files'].get(pidx, []):
                    item = group.children.itemById(input_id)
                    if item:
                        item.value = changed.value
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                'Błąd okna kopii zapasowej:\n' + traceback.format_exc())


class BackupValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            args.areInputsValid = any(c.value for c in _state.get('checks', []))
        except Exception:
            args.areInputsValid = True


class BackupExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _pending_job
        app = adsk.core.Application.get()
        try:
            inputs = args.command.commandInputs
            dest = inputs.itemById('bkDir').value or synclocal_core.get_vault_dir()
            job = {
                'dest': dest,
                'skip_existing': inputs.itemById('bkSkip').value,
                'step': inputs.itemById('bkStep').value,
                'projects': [],   # całe projekty (lista plików pobierana w workerze)
                'files': [],      # pojedyncze pliki: (nazwa projektu, ścieżka, DataFile)
            }
            for pidx, project in enumerate(_state['projects']):
                group = _state['groups'][pidx]
                if not _state['checks'][pidx].value:
                    continue
                loaded = _state['files'].get(pidx)
                if loaded is None:
                    job['projects'].append(project)
                else:
                    for input_id, data_file, path in loaded:
                        item = group.children.itemById(input_id)
                        if item and item.value:
                            job['files'].append((project.name, path, data_file))

            if not job['projects'] and not job['files']:
                app.userInterface.messageBox('Nie zaznaczono żadnego pliku do zapisu.')
                return

            synclocal_core.save_settings({'backup_step': job['step']})
            _pending_job = job
            app.fireCustomEvent(EVENT_ID, '')
        except Exception:
            app.userInterface.messageBox('Błąd uruchamiania kopii:\n' + traceback.format_exc())


class BackupDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        # Zwalniamy referencje do wejść okna (zadanie ma już własne kopie danych).
        _state.clear()


class BackupCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            cmd = args.command
            cmd.isRepeatable = False
            cmd.okButtonText = 'Zapisz zaznaczone'
            cmd.setDialogMinimumSize(460, 520)
            inputs = cmd.commandInputs
            settings = synclocal_core.load_settings()

            _state.clear()
            _state['projects'] = []
            _state['groups'] = []
            _state['checks'] = []
            _state['files'] = {}

            hub_name = ''
            try:
                hub_name = app.data.activeHub.name
            except Exception:
                pass

            inputs.addTextBoxCommandInput(
                'bkInfo', '',
                f'<b>Hub:</b> {hub_name}<br>'
                'Zaznacz projekty do zapisania. Domyślnie zapisywane są wszystkie pliki '
                'projektu - aby wybrać tylko niektóre, rozwiń sekcję „Pliki”, która '
                'pojawi się pod zaznaczonym projektem, i kliknij „Wybierz pojedyncze pliki”.',
                4, True)

            dir_input = inputs.addStringValueInput('bkDir', 'Folder docelowy', settings.get('vault_dir', ''))
            dir_input.isReadOnly = True
            inputs.addBoolValueInput('bkBrowse', 'Zmień folder...', False, '', False)

            inputs.addBoolValueInput('bkStep', 'Dodatkowo eksportuj do STEP (.step)', True, '',
                                     bool(settings.get('backup_step', False)))
            inputs.addBoolValueInput('bkSkip', 'Pomiń pliki już zapisane (bez zmian w chmurze)', True, '', True)

            inputs.addBoolValueInput('bkSelectAll', 'Zaznacz wszystkie projekty', False, '', False)
            inputs.addBoolValueInput('bkSelectNone', 'Odznacz wszystkie projekty', False, '', False)

            # Biblioteki Autodesk (np. „Standard Components”) na końcu listy, z dopiskiem.
            projects = sorted(app.data.dataProjects,
                              key=lambda p: (_is_library_project(p), p.name.lower()))
            for pidx, project in enumerate(projects):
                label = project.name + (LIBRARY_SUFFIX if _is_library_project(project) else '')
                check = inputs.addBoolValueInput(f'bkSel_{pidx}', label, True, '', False)
                group = inputs.addGroupCommandInput(f'bkProj_{pidx}', f'Pliki: {project.name}')
                group.isExpanded = False
                group.isVisible = False   # pokazywana dopiero po zaznaczeniu projektu
                group.children.addBoolValueInput(f'bkLoad_{pidx}', 'Wybierz pojedyncze pliki...', False, '', False)
                _state['projects'].append(project)
                _state['checks'].append(check)
                _state['groups'].append(group)

            if not projects:
                inputs.addTextBoxCommandInput('bkNoProjects', '', 'Brak projektów w aktywnym hubie.', 1, True)

            for event, handler in (
                (cmd.execute, BackupExecuteHandler()),
                (cmd.inputChanged, BackupInputChangedHandler()),
                (cmd.validateInputs, BackupValidateHandler()),
                (cmd.destroy, BackupDestroyHandler()),
            ):
                event.add(handler)
                handlers.append(handler)
        except Exception:
            ui.messageBox('Błąd okna kopii zapasowej:\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# START / STOP (wołane z SyncLocal360.py)
# ---------------------------------------------------------------------------
def start():
    global _backup_event
    app = adsk.core.Application.get()
    try:
        app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _backup_event = app.registerCustomEvent(EVENT_ID)
    handler = BackupEventHandler()
    _backup_event.add(handler)
    handlers.append(handler)


def stop():
    global _backup_event
    try:
        adsk.core.Application.get().unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _backup_event = None
    handlers.clear()
    _state.clear()
