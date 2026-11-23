# SyncLocal360 Galeria – aplikacja na Linuksa

Samodzielna aplikacja (AppImage) do przeglądania i porządkowania lokalnych kopii
projektów zrobionych dodatkiem SyncLocal360. Nie potrzebuje Fusion.

- wybór folderu z kopiami (pamięta ostatnio używane),
- kategorie (z podkategoriami), etykiety, notatki, ulubione – zapisywane w tych samych plikach co galeria
  w Fusion (`config.json` projektu, `synclocal_gallery.json`), więc obie strony widzą to samo
  przez Nextcloud,
- otwieranie `.step` / `.f3d` w wybranych programach (np. MitCad, FreeCAD) albo w programie
  domyślnym systemu; podwójne kliknięcie miniatury otwiera projekt programem od dwukliku,
- uruchomiona z AppImage dodaje się do menu programów: `~/.local/share/applications/synclocal360-galeria.desktop`
  i ikony `~/.local/share/icons/hicolor/*/apps/synclocal360-galeria.png`. Przy każdym starcie wpis
  jest odświeżany (po aktualizacji wskazuje nowy plik AppImage). Nie robi tego, gdy ten sam plik
  zintegrowało już inne narzędzie (np. AppImageLauncher). Usunięcie z menu: skasuj te pliki.

Interfejs to ten sam plik co w dodatku: `../resources/gallery/index.html`.

Autor: Cyryl Sochacki. Projekt SyncLocal360 jest oparty na dodatku NoCloud2 autorstwa
Kamila Sokalskiego (https://modelowanie3d.org) – zob. [NOTICE.md](../NOTICE.md).
Kod aplikacji: `src-tauri/src/lib.rs` (Tauri 2, Rust).

## Budowanie

Jednorazowo (biblioteki systemowe, Debian/Ubuntu):

```
sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev \
  libayatana-appindicator3-dev patchelf build-essential file
```

Potem w tym katalogu:

```
npm install            # narzędzie tauri (raz)
npm run dev            # uruchomienie na próbę, bez pakowania
npm run build          # AppImage
```

Gotowy plik: `src-tauri/target/release/bundle/appimage/SyncLocal360 Galeria_1.0.0_amd64.AppImage`
(w Releases na GitHubie ten sam plik nazywa się `SyncLocal360-Galeria-1.0.0-x86_64.AppImage`).
Pierwsze budowanie trwa kilka minut (kompilacja zależności); kolejne są szybkie.

Ikony aplikacji: `python3 tools/make_app_icon.py` (czysty Python, bez PIL).

## Uruchamianie

```
chmod +x "SyncLocal360-Galeria-1.0.0-x86_64.AppImage"
./"SyncLocal360-Galeria-1.0.0-x86_64.AppImage"                 # folder z ustawień
./"SyncLocal360-Galeria-1.0.0-x86_64.AppImage" ~/Nextcloud/NoCloud   # konkretny folder
```

Ustawienia aplikacji: `~/.config/org.synclocal360.galeria/settings.json`
(folder, ostatnie foldery, programy do otwierania plików).

## Programy do otwierania plików

Ustawienia (ikona zębatki) → „Programy do otwierania plików”. Każdy program ma:

- nazwę (np. `FreeCAD`),
- polecenie, w którym `%f` to ścieżka pliku: `freecad %f`,
  `flatpak run org.freecad.FreeCAD %f`, `~/Aplikacje/FreeCAD.AppImage %f`,
- rozszerzenia (np. `step, stp`); puste = wszystkie pliki.

Kolumna **Dwuklik** wybiera program, którym projekt otwiera się po dwukliku na miniaturze
(gdy nic nie wybrano – pierwszy na liście). Jeśli ten program ma na liście rozszerzeń `f3d`
(np. MitCad), dostaje plik `.f3d`, w innym razie `.step`. Można też wybrać „Program domyślny systemu”.
Jeśli wybrany program nie obsługuje danego pliku, użyty zostanie pierwszy pasujący z listy.
W panelu projektu przycisk programu od dwukliku jest niebieski.

**FreeCAD:** jeśli polecenie zawiera „freecad”, a plik to STEP/IGES/BREP, aplikacja nie podaje
FreeCAD-owi samego pliku, tylko jednorazowe makro (`~/.cache/synclocal360/open_*.py`, usuwa się samo).
Makro otwiera plik, nazywa dokument jak plik i ustawia widok izometryczny z „dopasuj wszystko”.
Bez tego FreeCAD zostawia kamerę w punkcie zero z kadrem ok. 4 mm i modele z Fusion są
niewidoczne albo mocno przybliżone.

Aplikacja sama dopisuje znalezione programy (każdy tylko raz – usunięty z listy nie wraca):

- **MitCad**: polecenie `mitcad` w `PATH` albo najnowszy `Mitcad-*.AppImage` w `~/Pulpit`,
  `~/Desktop`, `~/Aplikacje`, `~/Applications`, `~/Programy`, `~/.local/bin`, `~/bin`,
  `~/Pobrane`, `~/Downloads`; rozszerzenia `f3d, f3z, step, stp, iges, igs, stl, 3mf`,
- **FreeCAD**: w `PATH` albo jako Flatpak.

**Aktualizacje AppImage:** jeśli polecenie wskazuje na plik `.AppImage`, którego już nie ma
(np. `Mitcad-0.70.0-x86_64.AppImage` po aktualizacji do 0.71.0), aplikacja uruchamia najnowszy
plik AppImage z tego samego katalogu o tym samym początku nazwy (`Mitcad…`).

Pliki `.f3d` otwiera Fusion; **MitCad** potrafi je zaimportować (z historią projektu), ale nie
zawsze się to udaje, dlatego galeria podaje mu `.f3d` dopiero wtedy, gdy nie ma `.step`
(przycisk „MitCad (import)”). Pozostałym programom wystarczą kopie `.step` (opcja „Dodatkowo
eksportuj do STEP”). STEP to sama geometria, bez historii i parametrów z Fusion.

Pracę z programu na Linuksie zapisuj w folderze projektu w jego własnym formacie
(np. `Obudowa.mitcad`, `Obudowa.FCStd`). Galeria pokaże ten plik w „Plikach”, a dwuklik otworzy
go zamiast STEP. Własny format to rozszerzenie programu spoza formatów wymiany (STEP, IGES, STL,
3MF, `.f3d`…); dla MitCad (`mitcad`), FreeCAD (`fcstd`) i SolveSpace (`slvs`) galeria dopisuje je sama.

Jeśli zmienisz na Linuksie sam `.f3d` albo `.step` z kopii, dodatek w Fusion go nie nadpisze –
nowa wersja z chmury pojawi się obok, a w galerii wybierzesz, którą zostawić (⚠ zmiany lokalne).
