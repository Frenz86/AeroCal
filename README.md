# ✈️ AeroCal

Avvisi di Google Calendar dalla system tray di Windows: **5 minuti prima di ogni evento** un aeroplanino di carta attraversa lo schermo trainando uno striscione con il titolo dell'evento, accompagnato da una notifica toast di Windows.

## Caratteristiche

- 🛩️ **Animazione aeroplanino** — un aereo pubblicitario con striscione ondeggiante attraversa lo schermo (2 passaggi) con il titolo e l'orario dell'evento
- 🔔 **Toast di Windows** — notifica nativa con suono 5 minuti prima dell'inizio
- 🖥️ **Icona nella system tray** — menu con prossimi eventi, test dell'animazione, cambio URL e avvio automatico
- 🔁 **Aggiornamento automatico** — scarica il calendario ogni 2 minuti e gestisce anche gli eventi ricorrenti
- 🚀 **Avvio con Windows** — attivabile/disattivabile con un click dal menu della tray
- 🔒 **Sicurezza** — l'URL segreto del calendario è cifrato con DPAPI (legato al tuo account Windows), sono accettati solo URL HTTPS di `calendar.google.com`, i testi del calendario vengono sanificati e **nessun contenuto del calendario finisce nei log**

## Download (consigliato)

Non serve Python: scarica **`AeroCal.exe`** già pronto.

- Dalla pagina [**Releases**](https://github.com/Frenz86/AeroCal/releases) (build dei tag `v*`)
- Oppure dall'ultima build di [**GitHub Actions**](https://github.com/Frenz86/AeroCal/actions) → artifact `AeroCal-windows`

Al primo avvio Windows SmartScreen potrebbe chiedere conferma (l'exe non è firmato): clicca *Ulteriori informazioni* → *Esegui comunque*.

## Configurazione: l'URL segreto iCal

Al primo avvio AeroCal chiede l'**indirizzo segreto in formato iCal** del tuo Google Calendar:

1. Apri [Google Calendar](https://calendar.google.com) dal browser (non dall'app)
2. ⚙️ **Impostazioni** → nella colonna a sinistra scegli il tuo calendario
3. Sezione **"Integra il calendario"**
4. Scorri fino a **"Indirizzo segreto in formato iCal"** e copialo

> ⚠️ **Attenzione a non copiare l'"Indirizzo pubblico"**: quello funziona solo se il calendario è pubblico. Serve l'indirizzo *segreto* (contiene `/private-.../basic.ics`). E non condividerlo con nessuno: chi lo possiede può leggere il tuo calendario.

L'URL viene salvato **cifrato con DPAPI** in `%APPDATA%\AeroCal\config.dat`: è leggibile solo dal tuo account Windows su questo PC.

## Utilizzo

Una volta avviato, AeroCal vive nella system tray. Click sull'icona per il menu:

| Voce | Cosa fa |
|---|---|
| **Prossimi eventi** | Finestra con l'elenco degli eventi delle prossime 24 ore e lo stato dell'aggiornamento |
| **Testa aeroplanino** | Lancia l'animazione di prova |
| **Cambia URL ICS** | Riapre la finestra di configurazione |
| **Avvio automatico** | Attiva/disattiva l'avvio con Windows (spunta = attivo) |
| **Esci** | Chiude l'applicazione |

Note sul funzionamento:

- Vengono avvisati solo gli eventi con un orario (gli eventi *tutto il giorno* sono ignorati)
- Il calendario viene riscaricato ogni 2 minuti, e subito dopo il risveglio del PC dalla sospensione
- Ogni evento viene notificato una sola volta (deduplicazione per UID + orario)
- È attiva una protezione single-instance: avviare AeroCal due volte non crea duplicati

## Esecuzione da sorgente

Requisiti: **Windows** e **Python 3.11+** (consigliato 3.13).

```powershell
git clone https://github.com/Frenz86/AeroCal.git
cd AeroCal
pip install -r requirements.txt

# avvio senza finestra console (uso normale)
pythonw main.py

# (ri)configurare l'URL ICS
python main.py --setup

# test dell'animazione
python main.py --fly "Ciao mondo!"
```

## Compilare l'exe in locale

```powershell
pip install -r requirements.txt "pyinstaller>=6.10"

# genera l'icona a partire dal disegno dell'aereo
python -c "import main; main.make_icon_image().resize((256, 256)).save('aerocal.ico', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])"

pyinstaller --onefile --noconsole --clean --name AeroCal --icon aerocal.ico --collect-data tzdata --hidden-import pystray._win32 main.py
```

L'eseguibile finisce in `dist\AeroCal.exe`.

### Build automatica (CI)

Il workflow [`build-exe.yml`](.github/workflows/build-exe.yml) compila l'exe a ogni push su `main`, esegue uno smoke test e carica l'artifact. Creando un tag `v*` (es. `v1.0.0`) viene pubblicata automaticamente una **Release** con l'exe allegato.

```powershell
git tag v1.0.0
git push origin v1.0.0
```

## File e cartelle usati a runtime

| Percorso | Contenuto |
|---|---|
| `%APPDATA%\AeroCal\config.dat` | URL ICS cifrato con DPAPI |
| `%APPDATA%\AeroCal\aerocal.log` | Log tecnico minimale (solo tipi di errore, mai contenuti del calendario) |
| `HKCU\...\CurrentVersion\Run` → `AeroCal` | Chiave di registro per l'avvio automatico (solo se attivato) |

## Struttura del progetto

```
AeroCal/
├── main.py                        # tutta l'app: tray, motore, animazione, setup
├── requirements.txt               # dipendenze runtime
├── AeroCal.spec                   # spec PyInstaller
└── .github/workflows/build-exe.yml  # build CI dell'exe + release sui tag
```

## Licenza

Progetto personale — usalo e modificalo liberamente.
