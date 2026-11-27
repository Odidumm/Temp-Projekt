# Temperaturüberwachung Serverraum

Der Raspberry Pi misst mit einem DS18B20 die Temperatur und schickt sie alle
`INTERVAL_S` Sekunden an den Server auf dem NAS. Zwei LEDs zeigen den Zustand,
so wie der Server es vorgibt:

- **gelb** = Warnung (blinkt, wenn der Sensor ausgefallen ist)
- **rot** = Alarm

Die Grenzwerte stellst du im Dashboard des Servers ein. Ist der Server nicht
erreichbar, entscheidet der Pi selbst mit den zuletzt bekannten Grenzen, merkt
sich die Messwerte und liefert sie später nach. Eine kleine lokale REST-API
zeigt den Zustand und erlaubt das manuelle Schalten der LEDs zum Testen.

Die verbindliche Beschreibung der Schnittstelle steht in [pi-api.txt](pi-api.txt).

- [Hardware anschließen](#hardware-anschließen)
- [1-Wire aktivieren](#1-wire-aktivieren)
- [Installation](#installation)
- [Als Service einrichten](#als-service-einrichten)
- [Logs ansehen](#logs-ansehen)
- [Lokale REST-API](#lokale-rest-api)
- [Wie die LEDs entschieden werden](#wie-die-leds-entschieden-werden)
- [Puffer und Uhrzeit](#puffer-und-uhrzeit)
- [Wie schnell geht es?](#wie-schnell-geht-es)
- [Schnittstelle zum Server](#schnittstelle-zum-server)
- [Ohne Hardware testen](#ohne-hardware-testen)
- [Konfiguration](#konfiguration)

## Hardware anschließen

Getestet auf einem Raspberry Pi 5 mit Raspberry Pi OS (Debian 13 „trixie“) und Python 3.13.

| Bauteil | Anschluss am Pi |
|---|---|
| DS18B20 VDD (meist rot) | 3,3 V, Pin 1 |
| DS18B20 GND (meist schwarz) | GND, Pin 39 |
| DS18B20 DATA (meist gelb) | **GPIO 21, Pin 40** |
| Pull-up **4,7 kΩ** | zwischen DATA und 3,3 V |
| rote LED, Anode (langes Bein) | über Vorwiderstand 220–330 Ω an **GPIO 17, Pin 11** |
| gelbe LED, Anode (langes Bein) | über Vorwiderstand 220–330 Ω an **GPIO 27, Pin 13** |
| beide LED-Kathoden (kurzes Bein) | GND, Pin 9 oder Pin 14 |

Jede LED braucht ihren eigenen Vorwiderstand. Der Pull-up-Widerstand am Sensor
ist Pflicht, ohne ihn erkennt der Kernel keinen Sensor (siehe
[Fehlersuche](#fehlersuche-1-wire)). Manche Sensormodule haben ihn schon auf der
Platine.

## 1-Wire aktivieren

`raspi-config` aktiviert 1-Wire immer auf GPIO 4. Für GPIO 21 trägst du die
Zeile von Hand ein:

```bash
sudo nano /boot/firmware/config.txt
```

Ganz unten im Abschnitt `[all]`:

```ini
dtoverlay=w1-gpio,gpiopin=21
```

Speichern, dann `sudo reboot`. Danach prüfen:

```bash
ls /sys/bus/w1/devices/
# erwartet: 28-xxxxxxxxxxxx  w1_bus_master1

cat /sys/bus/w1/devices/28-*/w1_slave
# ... crc=57 YES
# ... t=23125        <- 23,125 °C
```

### Fehlersuche 1-Wire

| Symptom | Ursache |
|---|---|
| Nur `w1_bus_master1`, kein `28-…` | Sensor nicht angeschlossen oder falscher Pin |
| Wechselnde Geräte `00-…`, im `dmesg`: `Family 0 … is not registered` | Datenleitung hängt auf LOW: **Pull-up fehlt**, oder DATA/GND vertauscht |
| `pinctrl get 21` zeigt `lo` | Im Ruhezustand muss die Leitung `hi` sein, also wieder der Pull-up |
| `t=85000` | Sensor hat nach dem Einschalten noch nicht gemessen; wird vom Programm verworfen |
| `crc=… NO` | Störungen auf der Leitung (Kabel zu lang, Pull-up zu groß); wird verworfen |

## Installation

```bash
sudo apt install python3-venv python3-gpiozero python3-lgpio   # auf Raspberry Pi OS meist schon da

cd /home/pi/Desktop/Temp-RestAPI
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env          # enthält Tokens
nano .env               # SENSOR_ID, BASE_URL, API_TOKEN, LOCAL_TOKEN anpassen
```

Wird der Projektordner später verschoben oder umbenannt, muss `.venv` gelöscht
und neu angelegt werden, weil es feste Pfade enthält. Das gilt auch für die
Pfade in der systemd-Unit (siehe unten).

`--system-site-packages` sorgt dafür, dass gpiozero und lgpio aus den apt-Paketen
genutzt werden. Per pip sind sie auf dem Pi 5 umständlicher zu installieren.

### Sensor auf dem NAS anlegen

Ein Sensor meldet sich nicht selbst an. Er muss vorher im Projektordner auf dem
NAS angelegt werden:

```bash
docker compose exec tempmon flask sensor add rack-a --name "Rack A"
```

Der Befehl zeigt das Token **genau einmal** an. Es gehört als `API_TOKEN` in die
`.env` (nur das Token, ohne `Bearer` davor), die Kennung (`rack-a`) als
`SENSOR_ID`. Token verloren:

```bash
docker compose exec tempmon flask sensor rotate-token rack-a
```

### Adresse des Servers (BASE_URL)

| `BASE_URL` | wann |
|---|---|
| `http://<NAS-IP>:50001` | Der Pi ist im selben Netz wie das NAS. |
| `https://api.odidumm.com` | von überall, sobald die Adresse im Cloudflare Tunnel eingetragen ist |

Ohne Pfad eintragen, `/api/readings` hängt das Programm selbst an. Außerhalb des
eigenen Netzes lässt das Programm nur `https://` zu: Cloudflare leitet `http://`
nicht um, das Token ginge also unverschlüsselt durchs Internet.

Prüfen, ob der Pi den Server erreicht:

```bash
curl <BASE_URL>/healthz
# {"status":"ok","role":"ingest"}
```

Steht dort `"role":"dashboard"`, ist es die Adresse des Dashboards, nicht die der
Schnittstelle.

### LOCAL_TOKEN erzeugen

Ein zufälliges `LOCAL_TOKEN` erzeugen (mindestens 16 Zeichen; wer es kennt,
kann die LEDs abschalten):

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"
```

Solange in der `.env` noch der Platzhalter `bitte-aendern` steht, startet der
Dienst nicht und meldet einen Konfigurationsfehler.

### Starten

Einmal von Hand starten (mit Strg+C beenden):

```bash
.venv/bin/python -m temp_monitor
```

## Als Service einrichten

Der Dienst läuft direkt unter systemd, nicht in Docker, damit er auf die GPIOs
zugreifen kann. Er läuft als Benutzer `pi` und startet nach einem Neustart
automatisch.

```bash
sudo cp deploy/temp-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now temp-monitor
systemctl status temp-monitor
```

Liegt das Projekt nicht unter `/home/pi/Desktop/Temp-RestAPI`, passe vorher die
Pfade in `deploy/temp-monitor.service` an (`WorkingDirectory`, `ExecStart`).

Nach Änderungen an der `.env` oder am Code:

```bash
sudo systemctl restart temp-monitor
```

Bei einem Fehler in der `.env` beendet sich der Dienst mit Exit-Code 2 und wird
**nicht** automatisch neu gestartet. Der Grund steht im Log. Bei allen anderen
Fehlern startet systemd ihn nach 5 Sekunden neu.

Die zuletzt vom Server erhaltenen Grenzwerte und die letzte Stufe
(ok/warn/alarm) liegen in `/var/lib/temp-monitor/state.json` und überstehen
Neustarts.

## Logs ansehen

```bash
journalctl -u temp-monitor -f                  # live mitlesen
journalctl -u temp-monitor -b                  # seit dem letzten Boot
journalctl -u temp-monitor --since "1 hour ago"
journalctl -u temp-monitor -p warning          # nur Warnungen und Fehler
```

**Wichtig:** Raspberry Pi OS hält das Journal nur im RAM
(`Storage=volatile`). Nach einem Neustart sind alle Logs weg, und
`journalctl -b -1` (vorheriger Boot) zeigt nichts. Wer die Logs behalten
möchte, schaltet das Journal auf die SD-Karte um (kostet etwas
Schreibzugriffe):

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' \
  | sudo tee /etc/systemd/journald.conf.d/50-persistent.conf
sudo systemctl restart systemd-journald
```

Der Dateiname muss alphabetisch nach `40-rpi-volatile-storage.conf` kommen.
Danach funktioniert auch:

```bash
journalctl -u temp-monitor -b -1               # vom vorherigen Boot
```

Typische Meldungen:

| Meldung | Bedeutung |
|---|---|
| `Stufe ok -> warn bei 27.20 °C (Server)` | LEDs umgeschaltet, vom Server oder vom Pi |
| `Push fehlgeschlagen: … – Wert gepuffert (5 von höchstens 60000)` | Server nicht erreichbar, der Wert wird später nachgeliefert |
| `Pi entscheidet selbst: Alarm ab …` | keine gültige Antwort, es gelten die letzten Grenzen |
| `Noch nie Grenzwerte vom Server bekommen, LEDs bleiben aus` | noch keine einzige gültige Antwort |
| `5 gepufferte Werte nachgeliefert, noch 0 im Puffer` | Ausfall ist aufgeholt |
| `Server verweigert den Zugang (HTTP 401: …)` | `API_TOKEN` oder `SENSOR_ID` in der `.env` ist falsch |
| `Messwert … verworfen: HTTP 400: …` | Server lehnt den Wert ab, Grund steht dahinter |
| `Push fehlgeschlagen: HTTP 403: error code: 1010 …` | Cloudflare blockt die Anfrage (User-Agent), der Wert wird gepuffert |
| `Push fehlgeschlagen: HTTP 530: error code: 1033 …` | Cloudflare erreicht das NAS nicht (Tunnel aus), der Wert wird gepuffert |
| `Uhr noch nicht per NTP gestellt: …` | nach dem Booten, es wird noch nicht gesendet |
| `Messwert verworfen (1. Fehler in Folge): CRC-Fehler …` | ungültiger Messwert, wurde nicht gesendet |
| `Sensor ausgefallen (3 ungültige Messungen in Folge) – gelbe LED blinkt` | Sensor prüfen |

Mehr Details liefert `LOG_LEVEL=DEBUG` in der `.env`.

## Lokale REST-API

Standardmäßig auf Port 8080, erreichbar unter `http://<pi>:8080`.

```bash
curl http://localhost:8080/health
# {"ok": true, "sensor_id": "rack-a", "server_reachable": true,
#  "last_push": "2026-11-24T13:05:54Z", "last_error": null, "buffered": 0,
#  "sensor_ok": true, "level": "warn",
#  "leds": {"red": "off", "yellow": "on"},
#  "led_mode": {"red": "auto", "yellow": "auto"}}

curl http://localhost:8080/temperature
# {"sensor_id": "rack-a", "temp_c": 27.13, "ts": "2026-11-24T13:05:54Z", "sensor_ok": true}
# 503, solange es noch keinen gültigen Messwert gibt
```

In `/health` bedeuten:

- `server_reachable`: Der Server hat die letzte Messung angenommen.
- `last_push`: Zeitpunkt der letzten angenommenen Messung
- `last_error`: warum die letzte Messung nicht angenommen wurde, sonst `null`
- `buffered`: Anzahl der Werte, die noch nachgeliefert werden

LEDs manuell schalten (nur mit `LOCAL_TOKEN`):

```bash
curl -X POST http://localhost:8080/led \
     -H "Authorization: Bearer $LOCAL_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"led": "red", "state": "on"}'
# {"leds": {"red": "on", "yellow": "off"}, "led_mode": {"red": "manual", "yellow": "auto"}}
```

- `state`: `on`, `off` oder `auto` (gibt die Steuerung zurück an Server bzw. Pi)
- `led`: `red` oder `yellow`. Ohne `led` gilt der Befehl für **beide** LEDs,
  `{"state": "auto"}` setzt also alles zurück.

`POST /led` ohne oder mit falschem Token liefert 401, ein ungültiger Body 400.

Die API lauscht standardmäßig auf allen Netzwerkschnittstellen. Brauchst du sie
nur auf dem Pi selbst, setze `LOCAL_HOST=127.0.0.1`. Verbindungen, die 10 s
lang nichts senden, werden geschlossen, und es sind höchstens 32 gleichzeitig
offen. So kann niemand im LAN mit hängenden Verbindungen den Dienst lahmlegen.

## Wie die LEDs entschieden werden

Grundlage ist die Stufe `ok`, `warn` oder `alarm`:

| Stufe | rot | gelb |
|---|---|---|
| ok | aus | aus |
| warn | aus | **an** |
| alarm | **an** | aus |

Von der höchsten zur niedrigsten Priorität:

1. **Manuell** (`POST /led` mit `on`/`off`, je LED): gilt, bis `auto` kommt.
   Messen und Senden laufen in der Zeit weiter. Nach einem Neustart des
   Dienstes gilt immer `auto`.
2. **Sensor ausgefallen**: Nach 3 ungültigen Messungen in Folge **blinkt gelb**.
   Rot bleibt, wie es war: Ein Alarm endet nicht dadurch, dass der Sensor
   ausfällt. In dieser Zeit wird nichts gesendet. Mit dem nächsten gültigen
   Wert ist alles wieder normal.
3. **Server**: Antwortet der Server mit HTTP 200 und einer gültigen Antwort, folgt
   Rot genau dem Feld `led`. Gelb leuchtet, wenn `status` gleich `warn` ist. Die
   Grenzwerte aus der Antwort werden gespeichert.
4. **Pi**: Gibt es keine gültige Antwort (Server nicht erreichbar, Timeout 5 s,
   400, 401, 403, 5xx, unlesbare Antwort, Uhr nicht gestellt), bestimmt der Pi die
   Stufe selbst mit den zuletzt empfangenen Grenzen:

   | | beginnt ab | endet unter |
   |---|---|---|
   | Alarm (rot) | `threshold_c` | `clear_c` |

   Dazwischen bleibt Rot, wie es war, auch wenn es zuletzt der Server geschaltet
   hat und auch über einen Neustart hinweg. Beispiel mit Alarmgrenze 30: rot ab
   30,0 °C, bei 28,0 °C noch an, bei 27,9 °C aus.

   **Gelb bleibt in dieser Zeit aus**, weil der Server keine Warngrenze
   mitschickt. Schickt er zusätzlich `warn_c`, gilt auch ohne Server: gelb ab
   `warn_c`, aus unter `warn_c − (threshold_c − clear_c)`.

   Vor der allerersten Antwort bleiben beide LEDs aus. Es gibt keine fest
   eingebauten Grenzen.

Ungültige Messwerte werden verworfen und nicht gesendet: CRC-Fehler, der
85-°C-Einschaltwert, nur Nullen (Leitung auf GND), Werte außerhalb von −40 bis
125 °C (mehr nimmt der Server nicht an). Bei einem Fehler liest das Programm
sofort ein zweites Mal.

## Puffer und Uhrzeit

**Puffer:** Was mit einem Messwert passiert, hängt von der Antwort ab:

| Antwort | Messwert |
|---|---|
| 200 | angenommen |
| nicht erreichbar, Timeout, 5xx (auch von Cloudflare, wenn das NAS aus ist) | gepuffert |
| 403 (kommt von Cloudflare, nicht vom Server) | gepuffert, User-Agent prüfen |
| alles andere, was nicht beim Server ankam (404, 429, Weiterleitung …) | gepuffert |
| 400 | verworfen, ein neuer Versuch hilft nicht |
| 401 | verworfen, `API_TOKEN` oder `SENSOR_ID` ist falsch |
| 200 mit unlesbarer Antwort | verworfen, der Wert ist vermutlich schon gespeichert |

Der Puffer liegt im Arbeitsspeicher und fasst 60000 Werte, bei 1 s Takt gut
16 Stunden, bei 30 s drei Wochen. Danach fallen die ältesten heraus, nach einem
Neustart ist er leer. Ist der Server wieder da, geht zuerst der aktuelle Wert
hinaus, damit der Alarm sofort stimmt. Danach folgen die gepufferten mit ihrem
ursprünglichen Zeitstempel, und zwar nur in der Zeit, die bis zur nächsten Messung
übrig ist; der Rest folgt im nächsten Takt. Die Antworten auf nachgelieferte Werte
ändern weder LEDs noch Grenzen.

Der Server erkennt keine doppelten Werte. Kommt nach einem Timeout ein Wert zum
zweiten Mal, steht er doppelt im Verlauf. Auf Alarm und LEDs hat das keinen
Einfluss.

**Uhrzeit:** Der Server wertet einen Messwert nur für den Alarm aus, wenn sein
Zeitstempel nicht älter ist als der letzte. Geht die Uhr nach dem Booten noch
falsch, wären die Werte also wirkungslos. Deshalb sendet das Programm erst, wenn
die Uhr einmal per NTP gestellt war:

```bash
timedatectl show -p NTPSynchronized --value     # yes
```

Bis dahin wird gemessen und die LED geschaltet, aber weder gesendet noch
gepuffert.

## Wie schnell geht es?

Mit `INTERVAL_S=1` schickt der Pi jede Sekunde einen Wert. Schneller geht es nicht
sinnvoll: Der DS18B20 braucht für eine Messung mit voller Auflösung (12 Bit,
0,06 °C) rund 0,8 s, und der Zeitstempel hat nur ganze Sekunden. Netz und Server
bremsen nicht, im LAN dauert eine Anfrage rund 5 ms.

Was beim Sekundentakt zu beachten ist:

- Die Datenbank auf dem NAS wächst um rund 7,4 MB pro Tag und Sensor, bei 30 s
  sind es 0,2 MB.
- Wie schnell ein Wert zu sehen ist, bestimmt das Dashboard: Es fragt nur in
  festen Abständen nach.
- Ist das NAS ausgeschaltet, wartet jede Anfrage bis zu 5 s auf das Timeout. So
  lange dauert dann auch ein Takt.

## Schnittstelle zum Server

Vollständig beschrieben in [pi-api.txt](pi-api.txt). Der Pi schickt alle
`INTERVAL_S` Sekunden:

```http
POST <BASE_URL>/api/readings
Authorization: Bearer <API_TOKEN>
Content-Type: application/json
User-Agent: tempmon-pi/1.0

{"sensor_id": "rack-a", "temp_c": 31.4, "ts": "2026-11-24T13:05:54Z"}
```

Der `User-Agent` ist über `https://api.odidumm.com` nötig: Cloudflare blockt den
Standardwert von Python (`Python-urllib/…`) mit HTTP 403 „error code: 1010“.

Der Server antwortet mit HTTP 200 und:

```json
{"status": "alarm", "led": "on", "threshold_c": 30.0, "clear_c": 28.0}
```

| Feld | Bedeutung |
|---|---|
| `status` | `ok`, `warn` oder `alarm`. Die Hysterese wendet der Server selbst an. |
| `led` | `on` oder `off`, genau so wird die rote LED geschaltet |
| `threshold_c` | Alarmgrenze aus dem Dashboard, Alarm ab diesem Wert |
| `clear_c` | Alarm endet unter diesem Wert |

Die Antwort gilt nur, wenn alle vier Felder da sind, die Zahlen endlich sind und
`clear_c ≤ threshold_c` gilt. Sonst entscheidet der Pi selbst und behält seine
gespeicherten Grenzen. Das Feld `warn_c` (Warngrenze) gehört nicht zum Vertrag
und ist freiwillig. Weiterleitungen (HTTP 30x) werden nicht verfolgt.

## Ohne Hardware testen

### Automatische Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Die Tests nutzen einen Fake-Sensor, Fake-LEDs und einen echten kleinen
HTTP-Server als Ersatz für den zentralen Server. Abgedeckt sind Push (Body,
Header, Antwort, Fehlerarten), Entscheidung ohne Server, Puffer, Uhrzeit,
Hysterese, Sensor-Auswertung, Sensorausfall, lokale API und Konfiguration.

### Von Hand ausprobieren

Terminal 1, Ersatz für den zentralen Server (Warnung ab 27 °C, Alarm ab 30 °C,
Alarm-Ende unter 28 °C):

```bash
.venv/bin/python tools/mock_server.py --port 9000 --token test
```

Terminal 2, der Dienst mit Fake-Sensor und simulierten LEDs. Diese Variablen
überschreiben die Werte aus der `.env`. Port 8081 und eine eigene Zustandsdatei,
damit der Test nicht mit einem laufenden Service kollidiert:

```bash
echo 25 > /tmp/fake_temp
SENSOR_BACKEND=fake FAKE_TEMP_FILE=/tmp/fake_temp GPIOZERO_PIN_FACTORY=mock \
SENSOR_ID=rack-a BASE_URL=http://127.0.0.1:9000 API_TOKEN=test INTERVAL_S=2 \
LOCAL_PORT=8081 STATE_FILE=/tmp/test-state.json \
.venv/bin/python -m temp_monitor
```

Terminal 3:

```bash
echo 28 > /tmp/fake_temp      # Warnung (gelb)
echo 31 > /tmp/fake_temp      # Alarm (rot)
curl localhost:8081/health
# Mock-Server mit Strg+C beenden -> Pi entscheidet selbst, "buffered" steigt
echo 29 > /tmp/fake_temp      # rot bleibt an (Hysterese)
echo 27 > /tmp/fake_temp      # rot aus, gelb bleibt ohne Server aus
# Mock-Server wieder starten -> die gepufferten Werte werden nachgeliefert
rm /tmp/fake_temp             # Sensorausfall -> nach 3 Messungen blinkt gelb
```

Den Server allein prüfen, ohne Programm, steht in [pi-api.txt](pi-api.txt),
Abschnitt 6.

Lässt du `GPIOZERO_PIN_FACTORY=mock` weg, schalten die echten LEDs. Dafür muss
der Service gestoppt sein (`sudo systemctl stop temp-monitor`), sonst sind die
GPIOs belegt. Solange die LEDs nur simuliert werden, steht beim Start eine
Warnung im Log.

## Konfiguration

Alle Werte stehen in der `.env` (Vorlage: `.env.example`). Echte
Umgebungsvariablen haben Vorrang vor der `.env`.

| Variable | Pflicht | Standard | Bedeutung |
|---|---|---|---|
| `SENSOR_ID` | ja | | Kennung des Sensors, wie auf dem NAS angelegt (a-z, 0-9, `-`, `_`) |
| `BASE_URL` | ja | | `http://<NAS-IP>:50001` oder `https://api.odidumm.com`, ohne Pfad |
| `API_TOKEN` | ja | | Token des Sensors, ohne `Bearer` davor |
| `LOCAL_TOKEN` | ja | | Token für `POST /led`, mindestens 16 Zeichen |
| `LED_RED_PIN` | | 17 | GPIO der roten LED (Alarm, BCM-Nummer) |
| `LED_YELLOW_PIN` | | 27 | GPIO der gelben LED (Warnung, BCM-Nummer) |
| `INTERVAL_S` | | 30 | Messintervall in Sekunden (mind. 1) |
| `LOCAL_HOST` | | 0.0.0.0 | Adresse der lokalen API (`127.0.0.1` = nur lokal) |
| `LOCAL_PORT` | | 8080 | Port der lokalen API |
| `W1_DEVICE_ID` | | erster `28-*` | bestimmten Sensor verwenden, z. B. `28-5a097e0f64ff` |
| `STATE_FILE` | | `/var/lib/temp-monitor/state.json` unter systemd, sonst `./state.json` | Datei für Grenzwerte und letzte Stufe |
| `LOG_LEVEL` | | INFO | DEBUG, INFO, WARNING, ERROR |
| `SENSOR_BACKEND` | | ds18b20 | `fake` zum Testen ohne Sensor |
| `FAKE_TEMP_C` | | 22.0 | Wert des Fake-Sensors |
| `FAKE_TEMP_FILE` | | | Fake-Sensor liest den Wert aus dieser Datei. Unter systemd ist `/tmp` privat, dort z. B. `/var/lib/temp-monitor/fake_temp` |
| `GPIOZERO_PIN_FACTORY` | | | `mock` = LEDs nur simulieren |

Die früheren Variablen `API_URL` und `LED_PIN` gibt es nicht mehr. Stehen sie noch
in der `.env`, meldet der Dienst einen Konfigurationsfehler. `FALLBACK_ALARM_C` und
`FALLBACK_WARN_C` haben keine Wirkung mehr, die Grenzen kommen nur noch aus dem
Dashboard.

## Projektstruktur

```
temp_monitor/
  __main__.py    Start, Signale, alles verbinden
  config.py      .env laden und prüfen
  sensor.py      Sensor-Schnittstelle, DS18B20, Fake-Sensoren
  led.py         LED (gpiozero) und Fake-LED
  client.py      Push an den Server, Antwort prüfen
  controller.py  Messschleife, Puffer, Stufen mit Hysterese, LED-Logik
  state.py       Grenzwerte und letzte Stufe speichern
  timeutil.py    Zeitstempel, NTP-Prüfung
  server.py      HTTP-Server der lokalen API (Timeouts, Verbindungslimit)
  api.py         lokale REST-API (Flask)
tests/           pytest
tools/           Mock-Server zum Ausprobieren
deploy/          systemd-Unit
pi-api.txt       Auftrag und API-Beschreibung des Servers (verbindlich)
```
