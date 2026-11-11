# -*- coding: utf-8 -*-
"""
SyncLocal360 - Przywracanie projektów do chmury (wsadowe wgrywanie .f3d).

Część dodatku SyncLocal360, opartego na dodatku NoCloud2 autorstwa
Kamila Sokalskiego (https://modelowanie3d.org), wydanym na licencji
otwartej z wymogiem uznania autorstwa.

Co robi:
  * skanuje wskazany katalog w poszukiwaniu plików .f3d (folder SyncLocal360
    z config.json albo dowolny katalog z plikami .f3d),
  * grupuje je według projektów z chmury zapisanych przez "Kopię z chmury"
    (config.json: cloud_project / cloud_folder); pliki bez tej informacji
    trafiają do projektu nazwanego od podkatalogu albo do projektu domyślnego,
  * pozwala zaznaczyć projekty / pojedyncze pliki,
  * w aktywnym hubie Fusion zakłada brakujące projekty i podfoldery
    i wgrywa pliki pod ich oryginalnymi nazwami,
  * zapisuje log z przebiegu w katalogu źródłowym.
"""

import adsk.core, adsk.fusion, traceback
import os, re, json, shutil, tempfile, datetime, time

import synclocal_core

CMD_ID = 'SyncLocal360_CloudRestore'
EVENT_ID = 'SyncLocal360_CloudRestoreEvent'
DEFAULT_PROJECT = 'SyncLocal360 - przywrócone'

handlers = []
_state = {}
_pending_job = None
_restore_event = None


# ---------------------------------------------------------------------------
# SKANOWANIE KATALOGU
# ---------------------------------------------------------------------------
def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def scan_source(src):
    """Zwraca listę plików do przywrócenia:
    {'path', 'name', 'project', 'folder': [..]}"""
    items = []
    src = os.path.normpath(src)
    if not os.path.isdir(src):
        return items

    for root, dirs, files in os.walk(src):
        # Stare wersje pomijamy - przywracamy tylko główny (najnowszy) plik.
        dirs[:] = sorted(d for d in dirs if d.lower() != 'versions')
        rel = os.path.relpath(root, src)
        parts = [] if rel == '.' else rel.split(os.sep)

        if 'config.json' in files:
            meta = _read_json(os.path.join(root, 'config.json'))
            f3d = os.path.join(root, meta.get('file') or '') if meta.get('file') else ''
            if meta and os.path.isfile(f3d):
                folder = [p for p in str(meta.get('cloud_folder') or '').split('/') if p]
                items.append({
                    'path': f3d,
                    'name': str(meta.get('displayname') or os.path.splitext(meta['file'])[0]),
                    'project': str(meta.get('cloud_project') or DEFAULT_PROJECT),
                    'folder': folder,
                })
                dirs[:] = []
                continue

        for fname in sorted(files):
            if not fname.lower().endswith('.f3d'):
                continue
            stem = fname[:-4]
            if len(parts) == 1 and parts[0] == stem:
                # Układ SyncLocal360/NoCloud bez config.json: <nazwa>/<nazwa>.f3d
                project, folder = DEFAULT_PROJECT, []
            elif parts:
                # Zwykły katalog: pierwszy podkatalog = projekt, reszta = foldery.
                project, folder = parts[0], parts[1:]
            else:
                project, folder = os.path.basename(src) or DEFAULT_PROJECT, []
            items.append({'path': os.path.join(root, fname), 'name': stem,
                          'project': project, 'folder': folder})

    for item in items:
        if item['name'].lower().endswith('.f3d'):
            item['name'] = item['name'][:-4]
    return items


def _group_by_project(items):
    groups = {}
    for item in items:
        groups.setdefault(item['project'], []).append(item)
    result = []
    for name in sorted(groups, key=str.lower):
        files = sorted(groups[name], key=lambda i: ('/'.join(i['folder']) + '/' + i['name']).lower())
        result.append((name, files))
    return result


# ---------------------------------------------------------------------------
# CHMURA: projekty, foldery, wysyłanie
# ---------------------------------------------------------------------------
class _CloudCache:
    def __init__(self, app):
        self.app = app
        self.projects = {}   # nazwa -> DataProject
        self.folders = {}    # (projekt, ścieżka) -> DataFolder
        self.names = {}      # id folderu -> zbiór nazw plików
        for p in app.data.dataProjects:
            self.projects.setdefault(p.name, p)

    def project(self, name):
        proj = self.projects.get(name)
        if proj is None:
            proj = self.app.data.dataProjects.add(name)
            if proj is None:
                raise RuntimeError(f'Nie udało się utworzyć projektu "{name}".')
            self.projects[name] = proj
        return proj

    def folder(self, project_name, path):
        key = (project_name, tuple(path))
        if key in self.folders:
            return self.folders[key]
        if not path:
            folder = self.project(project_name).rootFolder
        else:
            parent = self.folder(project_name, path[:-1])
            folder = parent.dataFolders.itemByName(path[-1])
            if folder is None:
                folder = parent.dataFolders.add(path[-1])
                if folder is None:
                    raise RuntimeError(f'Nie udało się utworzyć folderu "{"/".join(path)}".')
        self.folders[key] = folder
        return folder

    def file_names(self, folder):
        fid = folder.id
        if fid not in self.names:
            names = set()
            for f in folder.dataFiles:
                try:
                    names.add(f.name)
                except Exception:
                    pass
            self.names[fid] = names
        return self.names[fid]


def _safe_upload_name(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'Unnamed'


def _wait_for_upload(future, timeout=1800):
    start = time.time()
    while future.uploadState == adsk.core.UploadStates.UploadProcessing:
        if time.time() - start > timeout:
            raise RuntimeError('Przekroczono czas wysyłania.')
        adsk.doEvents()
        time.sleep(0.1)
    if future.uploadState != adsk.core.UploadStates.UploadFinished:
        raise RuntimeError('Fusion zgłosił błąd wysyłania pliku.')
    return future.dataFile


def restore_item(item, target_project, target_folder, cache, tmp_root, idx, skip_existing):
    folder = cache.folder(target_project, target_folder)
    names = cache.file_names(folder)
    upload_name = _safe_upload_name(item['name'])

    if skip_existing and (item['name'] in names or upload_name in names):
        return 'skip', 'już jest w chmurze'

    # Nazwa pliku w chmurze = nazwa wysyłanego pliku, więc wysyłamy kopię
    # o nazwie projektu (np. folder kopii mógł mieć inną, "bezpieczną" nazwę).
    tmp_dir = os.path.join(tmp_root, str(idx))
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, upload_name + '.f3d')
    shutil.copyfile(item['path'], tmp_path)

    future = folder.uploadFile(tmp_path)
    if future is None:
        raise RuntimeError('Fusion odrzucił plik do wysłania.')
    data_file = _wait_for_upload(future)

    if data_file is not None and upload_name != item['name']:
        try:
            data_file.name = item['name']
        except Exception:
            pass
    names.add(item['name'])
    names.add(upload_name)
    return 'ok', '/'.join([target_project] + target_folder + [item['name']])


def run_restore(job):
    app = adsk.core.Application.get()
    ui = app.userInterface

    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.cancelButtonText = 'Przerwij'
    progress.isBackgroundTranslucent = False
    progress.show('SyncLocal360 - przywracanie do chmury', 'Odczytywanie projektów w chmurze...', 0, max(len(job['items']), 1), 0)
    adsk.doEvents()

    counts = {'ok': 0, 'skip': 0, 'err': 0}
    log = []
    cancelled = False
    tmp_root = tempfile.mkdtemp(prefix='synclocal360_restore_')
    try:
        cache = _CloudCache(app)
        total = len(job['items'])
        for i, item in enumerate(job['items']):
            if progress.wasCancelled:
                cancelled = True
                break

            if job['single_project']:
                target_project = job['single_project']
                target_folder = ([item['project']] + item['folder']) if job['folders'] else []
            else:
                target_project = item['project']
                target_folder = item['folder'] if job['folders'] else []

            location = '/'.join([target_project] + target_folder + [item['name']])
            progress.progressValue = i
            progress.message = f'({i + 1}/{total}) Wysyłanie: {location}'
            adsk.doEvents()

            try:
                status, info = restore_item(item, target_project, target_folder, cache,
                                            tmp_root, i, job['skip_existing'])
                counts[status] += 1
                log.append(f"[{'OK' if status == 'ok' else 'POMINIĘTO'}] {location} <- {item['path']}"
                           + ('' if status == 'ok' else f' ({info})'))
            except Exception as e:
                counts['err'] += 1
                log.append(f'[BŁĄD] {location} <- {item["path"]}: {e}\n        '
                           f'{traceback.format_exc(limit=2).strip()}')
            adsk.doEvents()
        if progress.wasCancelled:
            cancelled = True
    finally:
        progress.hide()
        shutil.rmtree(tmp_root, ignore_errors=True)

    hub_name = ''
    try:
        hub_name = app.data.activeHub.name
    except Exception:
        pass

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = os.path.join(job['source'], f'restore_log_{stamp}.txt')
    header = [
        f'SyncLocal360 - przywracanie do chmury, {datetime.datetime.now().isoformat(timespec="seconds")}',
        f'Źródło: {job["source"]}',
        f'Hub docelowy: {hub_name}',
        f'Wysłano: {counts["ok"]}, pominięto: {counts["skip"]}, błędy: {counts["err"]}'
        + (' (PRZERWANO)' if cancelled else ''),
        '-' * 70,
    ]
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(header + log) + '\n')
    except Exception:
        log_path = '(nie udało się zapisać logu)'

    ui.messageBox(
        ('Przywracanie przerwane przez użytkownika.\n\n' if cancelled else 'Przywracanie zakończone.\n\n')
        + f'Wysłano: {counts["ok"]}\nPominięto (już w chmurze): {counts["skip"]}\nBłędy: {counts["err"]}\n\n'
        + 'Fusion może jeszcze przez chwilę przetwarzać wysłane pliki w chmurze '
        + '(widać to w panelu Dane / Data Panel).\n\n'
        + f'Log: {log_path}',
        'SyncLocal360 - przywracanie do chmury')


class RestoreEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global _pending_job
        job, _pending_job = _pending_job, None
        if not job:
            return
        try:
            run_restore(job)
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                'Błąd przywracania:\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# OKNO DIALOGOWE
# ---------------------------------------------------------------------------
def _clear_list():
    for inp in _state.get('list_inputs', []):
        try:
            inp.deleteMe()
        except Exception:
            pass
    _state['list_inputs'] = []
    _state['checks'] = []
    _state['groups'] = []
    _state['files'] = []


def _build_list(inputs, source):
    """(Prze)budowuje listę projektów i plików znalezionych w katalogu."""
    _clear_list()
    _state['gen'] = _state.get('gen', 0) + 1
    gen = _state['gen']

    grouped = _group_by_project(scan_source(source))
    total = sum(len(files) for _name, files in grouped)
    summary = inputs.addTextBoxCommandInput(
        f'rsSummary_{gen}', '',
        f'Znaleziono plików .f3d: <b>{total}</b> w {len(grouped)} projektach.'
        if total else 'W tym katalogu nie znaleziono plików .f3d.', 1, True)
    _state['list_inputs'].append(summary)

    for gidx, (project_name, files) in enumerate(grouped):
        check = inputs.addBoolValueInput(f'rsSel_{gen}_{gidx}', f'{project_name}  ({len(files)})',
                                         True, '', False)
        group = inputs.addGroupCommandInput(f'rsProj_{gen}_{gidx}', f'Pliki: {project_name}')
        group.isExpanded = False
        group.isVisible = False
        group.children.addBoolValueInput(f'rsAll_{gen}_{gidx}', 'Wszystkie pliki projektu', True, '', True)
        entries = []
        for fidx, item in enumerate(files):
            label = '/'.join(item['folder'] + [item['name']])
            input_id = f'rsFile_{gen}_{gidx}_{fidx}'
            group.children.addBoolValueInput(input_id, label, True, '', True)
            entries.append((input_id, item))
        _state['list_inputs'] += [check, group]
        _state['checks'].append(check)
        _state['groups'].append(group)
        _state['files'].append(entries)


class RestoreInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            changed = args.input
            cid = changed.id
            inputs = args.firingEvent.sender.commandInputs
            ui = adsk.core.Application.get().userInterface

            if cid == 'rsBrowse':
                dialog = ui.createFolderDialog()
                dialog.title = 'Wybierz katalog z plikami .f3d do przywrócenia'
                if dialog.showDialog() == adsk.core.DialogResults.DialogOK:
                    inputs.itemById('rsDir').value = dialog.folder
                    _build_list(inputs, dialog.folder)

            elif cid == 'rsMode':
                single = changed.selectedItem.index == 1
                inputs.itemById('rsTarget').isVisible = single

            elif cid in ('rsSelectAll', 'rsSelectNone'):
                selected = (cid == 'rsSelectAll')
                for gidx, check in enumerate(_state.get('checks', [])):
                    check.value = selected
                    _state['groups'][gidx].isVisible = selected

            elif cid.startswith('rsSel_'):
                gidx = int(cid.split('_')[2])
                _state['groups'][gidx].isVisible = changed.value

            elif cid.startswith('rsAll_'):
                gidx = int(cid.split('_')[2])
                group = _state['groups'][gidx]
                for input_id, _item in _state['files'][gidx]:
                    item = group.children.itemById(input_id)
                    if item:
                        item.value = changed.value
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                'Błąd okna przywracania:\n' + traceback.format_exc())


class RestoreValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            ok = any(c.value for c in _state.get('checks', []))
            if inputs.itemById('rsMode').selectedItem.index == 1:
                ok = ok and bool(inputs.itemById('rsTarget').value.strip())
            args.areInputsValid = ok
        except Exception:
            args.areInputsValid = True


class RestoreExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _pending_job
        app = adsk.core.Application.get()
        try:
            inputs = args.command.commandInputs
            single = inputs.itemById('rsMode').selectedItem.index == 1
            job = {
                'source': inputs.itemById('rsDir').value,
                'single_project': inputs.itemById('rsTarget').value.strip() if single else '',
                'folders': inputs.itemById('rsFolders').value,
                'skip_existing': inputs.itemById('rsSkip').value,
                'items': [],
            }
            for gidx, check in enumerate(_state.get('checks', [])):
                if not check.value:
                    continue
                group = _state['groups'][gidx]
                for input_id, item in _state['files'][gidx]:
                    inp = group.children.itemById(input_id)
                    if inp and inp.value:
                        job['items'].append(item)

            if not job['items']:
                app.userInterface.messageBox('Nie zaznaczono żadnego pliku do przywrócenia.')
                return

            _pending_job = job
            app.fireCustomEvent(EVENT_ID, '')
        except Exception:
            app.userInterface.messageBox('Błąd uruchamiania przywracania:\n' + traceback.format_exc())


class RestoreDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _state.clear()


class RestoreCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            cmd = args.command
            cmd.isRepeatable = False
            cmd.okButtonText = 'Wyślij do chmury'
            cmd.setDialogMinimumSize(460, 520)
            inputs = cmd.commandInputs
            settings = synclocal_core.load_settings()

            _state.clear()
            _state['list_inputs'] = []

            hub_name = ''
            try:
                hub_name = app.data.activeHub.name
            except Exception:
                pass

            inputs.addTextBoxCommandInput(
                'rsInfo', '',
                f'<b>Hub docelowy:</b> {hub_name}<br>'
                'Pliki .f3d z wybranego katalogu zostaną wysłane do tego huba. '
                'Brakujące projekty i foldery zostaną utworzone. '
                'Jeśli to nie ten hub - zamknij okno i przełącz hub w Fusion.',
                4, True)

            source = settings.get('vault_dir', '')
            dir_input = inputs.addStringValueInput('rsDir', 'Katalog źródłowy', source)
            dir_input.isReadOnly = True
            inputs.addBoolValueInput('rsBrowse', 'Zmień katalog...', False, '', False)

            mode = inputs.addDropDownCommandInput('rsMode', 'Projekty w chmurze',
                                                  adsk.core.DropDownStyles.TextListDropDownStyle)
            mode.listItems.add('Takie jak w kopii (oryginalne nazwy)', True)
            mode.listItems.add('Wszystko do jednego projektu', False)
            target = inputs.addStringValueInput('rsTarget', 'Nazwa projektu', DEFAULT_PROJECT)
            target.isVisible = False

            inputs.addBoolValueInput('rsFolders', 'Odtwórz podfoldery', True, '', True)
            inputs.addBoolValueInput('rsSkip', 'Pomiń pliki, które już są w chmurze (ta sama nazwa)',
                                     True, '', True)

            inputs.addBoolValueInput('rsSelectAll', 'Zaznacz wszystkie projekty', False, '', False)
            inputs.addBoolValueInput('rsSelectNone', 'Odznacz wszystkie projekty', False, '', False)

            _build_list(inputs, source)

            for event, handler in (
                (cmd.execute, RestoreExecuteHandler()),
                (cmd.inputChanged, RestoreInputChangedHandler()),
                (cmd.validateInputs, RestoreValidateHandler()),
                (cmd.destroy, RestoreDestroyHandler()),
            ):
                event.add(handler)
                handlers.append(handler)
        except Exception:
            ui.messageBox('Błąd okna przywracania:\n' + traceback.format_exc())


# ---------------------------------------------------------------------------
# START / STOP (wołane z SyncLocal360.py)
# ---------------------------------------------------------------------------
def start():
    global _restore_event
    app = adsk.core.Application.get()
    try:
        app.unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _restore_event = app.registerCustomEvent(EVENT_ID)
    handler = RestoreEventHandler()
    _restore_event.add(handler)
    handlers.append(handler)


def stop():
    global _restore_event
    try:
        adsk.core.Application.get().unregisterCustomEvent(EVENT_ID)
    except Exception:
        pass
    _restore_event = None
    handlers.clear()
    _state.clear()
