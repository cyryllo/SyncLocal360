# -*- coding: utf-8 -*-
"""
SyncLocal360 - lokalne kopie projektów Fusion.

Na bazie dodatku NoCloud2 autorstwa Kamila Sokalskiego
(https://modelowanie3d.org), wydanego na licencji otwartej z wymogiem
uznania autorstwa.

Przyciski (karta Bryła -> panel SyncLocal360):
  * Szybki Zapis        - zapis aktywnego projektu na dysk (.f3d + miniatura)
  * Kopia z chmury      - wsadowy zapis zaznaczonych projektów z chmury (+ opcjonalnie STEP)
  * Przywróć do chmury  - wsadowe wgranie plików .f3d z dysku do chmury
  * Galeria            - przeglądarka kopii w przeglądarce WWW (kategorie, etykiety)
  * Ustawienia          - folder, autozapis, wersjonowanie
"""

import sys
import os
import importlib
import traceback
import adsk.core, adsk.fusion

current_dir = os.path.dirname(os.path.realpath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

import synclocal_core
import synclocal_backup
import synclocal_restore
import synclocal_gallery

importlib.reload(synclocal_core)
importlib.reload(synclocal_backup)
importlib.reload(synclocal_restore)
importlib.reload(synclocal_gallery)

app = adsk.core.Application.get()
ui = app.userInterface
handlers = []

# --- KONFIGURACJA INTERFEJSU ---
WORKSPACE_ID = 'FusionSolidEnvironment'
TAB_ID = 'SolidTab'
PANEL_ID = 'SyncLocal360_Panel'
PANEL_NAME = 'SyncLocal360'
FALLBACK_PANEL_ID = 'SolidCreatePanel'   # gdyby nie udało się utworzyć własnego panelu

CMD_SAVE_ID = 'SyncLocal360_Save'
CMD_BACKUP_ID = synclocal_backup.CMD_ID
CMD_RESTORE_ID = synclocal_restore.CMD_ID
CMD_GALLERY_ID = 'SyncLocal360_Gallery'
CMD_SETTINGS_ID = 'SyncLocal360_Settings'
ALL_CMD_IDS = [CMD_SAVE_ID, CMD_BACKUP_ID, CMD_RESTORE_ID, CMD_GALLERY_ID, CMD_SETTINGS_ID]

ICON_SAVE = 'resources/icons/save'
ICON_BACKUP = 'resources/icons/backup'
ICON_RESTORE = 'resources/icons/restore'
ICON_GALLERY = 'resources/icons/gallery'
ICON_SETTINGS = 'resources/icons/gear'


# --- SZYBKI ZAPIS ---
class SaveCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        app = adsk.core.Application.get()
        ui = app.userInterface
        try:
            cmd = args.command
            cmd.isRepeatable = False

            settings = synclocal_core.load_settings()
            design = adsk.fusion.Design.cast(app.activeProduct)
            doc = design.parentDocument if design else None

            if settings.get("ask_name_enabled") and doc is not None:
                current_name = (doc.name or "").strip()
                if current_name in synclocal_core.UNNAMED_DOC_NAMES or current_name == "":
                    entered, cancelled = ui.inputBox(
                        "Podaj nazwę projektu przed zapisem:",
                        "SyncLocal360 - Nazwa projektu",
                        current_name
                    )
                    if cancelled or not entered or not entered.strip():
                        ui.messageBox("Zapis przerwany: nie podano nazwy projektu.")
                        return
                    try:
                        doc.name = entered.strip()
                    except Exception:
                        pass

            ok, result = synclocal_core.save_current_document_to_vault(reason="manual")
            if ok:
                if settings.get("notify_on_save", True):
                    ui.messageBox(f"Zapisano lokalnie: {result}")
            else:
                ui.messageBox(f"Błąd zapisu: {result}")

            args.command.doExecute(False)
        except Exception:
            ui.messageBox("Błąd zapisu:\n{}".format(traceback.format_exc()))


# --- GALERIA ---
class GalleryCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            args.command.isRepeatable = False
            synclocal_gallery.start_in_fusion()
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                "Nie udało się otworzyć galerii:\n" + traceback.format_exc())


# --- USTAWIENIA ---
class SettingsCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            settings = synclocal_core.load_settings()
            folder_value = inputs.itemById("vaultDir").value
            settings["vault_dir"] = folder_value if folder_value else settings["vault_dir"]
            settings["autosave_enabled"] = inputs.itemById("autosaveEnabled").value
            settings["autosave_interval_min"] = max(1, int(round(inputs.itemById("autosaveInterval").value)))
            settings["versioning_enabled"] = inputs.itemById("versioningEnabled").value
            settings["ask_name_enabled"] = inputs.itemById("askNameEnabled").value
            settings["notify_on_save"] = inputs.itemById("notifyOnSave").value
            synclocal_core.save_settings(settings)
            synclocal_core.restart_autosave_timer()
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                "Błąd zapisu ustawień:\n" + traceback.format_exc())


class SettingsInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            if args.input.id == "browseFolderBtn":
                inputs = args.firingEvent.sender.commandInputs
                dialog = adsk.core.Application.get().userInterface.createFolderDialog()
                dialog.title = "Wybierz folder zapisu SyncLocal360"
                if dialog.showDialog() == adsk.core.DialogResults.DialogOK:
                    inputs.itemById("vaultDir").value = dialog.folder
        except Exception:
            pass


class SettingsCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            settings = synclocal_core.load_settings()
            cmd = args.command
            cmd.setDialogMinimumSize(420, 320)
            inputs = cmd.commandInputs

            inputs.addStringValueInput("vaultDir", "Folder zapisu", settings["vault_dir"]).isReadOnly = True
            inputs.addBoolValueInput("browseFolderBtn", "Wybierz folder...", False, "", False)
            inputs.addBoolValueInput("autosaveEnabled", "Autozapis włączony", True, "", settings["autosave_enabled"])
            inputs.addValueInput("autosaveInterval", "Interwał autozapisu (min)", "",
                                 adsk.core.ValueInput.createByReal(settings["autosave_interval_min"]))
            inputs.addBoolValueInput("versioningEnabled", "Szybki Zapis: twórz nowe wersje zamiast nadpisywać",
                                     True, "", settings["versioning_enabled"])
            inputs.addBoolValueInput("askNameEnabled", "Pytaj o nazwę przed zapisem (plik bez nazwy)",
                                     True, "", settings.get("ask_name_enabled", False))
            inputs.addBoolValueInput("notifyOnSave", "Powiadamiaj o zapisie", True, "",
                                     settings.get("notify_on_save", True))

            on_execute = SettingsCommandExecuteHandler()
            cmd.execute.add(on_execute)
            handlers.append(on_execute)
            on_changed = SettingsInputChangedHandler()
            cmd.inputChanged.add(on_changed)
            handlers.append(on_changed)
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                "Błąd ustawień:\n" + traceback.format_exc())


# --- PANEL I PRZYCISKI ---
def get_panel(create=True):
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    if workspace is None:
        return None
    tab = workspace.toolbarTabs.itemById(TAB_ID)
    if tab is not None:
        panel = tab.toolbarPanels.itemById(PANEL_ID)
        if panel is None and create:
            try:
                panel = tab.toolbarPanels.add(PANEL_ID, PANEL_NAME)
            except Exception:
                panel = None
        if panel is not None:
            return panel
    return workspace.toolbarPanels.itemById(FALLBACK_PANEL_ID) if create else None


def add_button(panel, cmd_id, name, desc, icon_folder, created_handler, promoted=True):
    cmd_def = ui.commandDefinitions.itemById(cmd_id)
    if cmd_def:
        cmd_def.deleteMe()
    cmd_def = ui.commandDefinitions.addButtonDefinition(cmd_id, name, desc, icon_folder)
    cmd_def.commandCreated.add(created_handler)
    handlers.append(created_handler)

    control = panel.controls.itemById(cmd_id)
    if control:
        control.deleteMe()
    control = panel.controls.addCommand(cmd_def)
    control.isPromoted = promoted
    control.isPromotedByDefault = promoted
    return control


def run(context):
    try:
        panel = get_panel(create=True)
        if panel is None:
            ui.messageBox('SyncLocal360: nie znaleziono przestrzeni roboczej Bryła (Design).')
            return

        buttons = [
            (CMD_SAVE_ID, 'Szybki Zapis', 'Zapisuje aktywny projekt lokalnie (.f3d + miniatura)',
             ICON_SAVE, SaveCommandCreatedHandler(), None),
            (CMD_BACKUP_ID, 'Kopia z chmury',
             'Zapisuje lokalnie zaznaczone projekty z chmury Fusion (opcjonalnie także STEP)',
             ICON_BACKUP, synclocal_backup.BackupCommandCreatedHandler(), synclocal_backup.start),
            (CMD_RESTORE_ID, 'Przywróć do chmury',
             'Wgrywa zaznaczone pliki .f3d z dysku do chmury Fusion, odtwarzając projekty i foldery',
             ICON_RESTORE, synclocal_restore.RestoreCommandCreatedHandler(), synclocal_restore.start),
            (CMD_GALLERY_ID, 'Galeria',
             'Otwiera w przeglądarce galerię lokalnych kopii: kategorie, etykiety, notatki, '
             'otwieranie i wstawianie projektów',
             ICON_GALLERY, GalleryCommandCreatedHandler(), None),
            (CMD_SETTINGS_ID, 'Ustawienia SyncLocal360', 'Folder zapisu, autozapis, wersjonowanie',
             ICON_SETTINGS, SettingsCommandCreatedHandler(), None),
        ]
        for cmd_id, name, desc, icon, handler, starter in buttons:
            try:
                if starter:
                    starter()
                add_button(panel, cmd_id, name, desc, icon, handler)
            except Exception:
                ui.messageBox(f'SyncLocal360: błąd dodawania przycisku „{name}”:\n{traceback.format_exc()}')

        synclocal_core.start_autosave()
    except Exception:
        ui.messageBox('Krytyczny błąd ładowania SyncLocal360:\n{}'.format(traceback.format_exc()))


def stop(context):
    try:
        synclocal_core.stop_autosave()
        synclocal_backup.stop()
        synclocal_restore.stop()
        synclocal_gallery.stop()

        workspace = ui.workspaces.itemById(WORKSPACE_ID)
        panels = []
        own_panel = get_panel(create=False)
        if own_panel:
            panels.append(own_panel)
        if workspace:
            fallback = workspace.toolbarPanels.itemById(FALLBACK_PANEL_ID)
            if fallback:
                panels.append(fallback)

        for cmd_id in ALL_CMD_IDS:
            for panel in panels:
                control = panel.controls.itemById(cmd_id)
                if control:
                    control.deleteMe()
            cmd_def = ui.commandDefinitions.itemById(cmd_id)
            if cmd_def:
                cmd_def.deleteMe()

        if own_panel and own_panel.id == PANEL_ID:
            own_panel.deleteMe()
    except Exception:
        pass
