# Technische Dokumentation

## Architektur und Entscheidung

Der Host orchestriert mit Python-Standardbibliothek. Auf macOS wird Docker CLI über
das explizit ausgewählte Colima-Profil bedient; der Harness setzt dafür den passenden
`DOCKER_HOST`-Socket und startet das Profil bei Bedarf mit Docker-Runtime. Dauerhaft
laufen nur SonarQube Community Build und dessen PostgreSQL in Compose. Jede Prüfaufgabe läuft in einem
kurzlebigen, per SHA-256 gepinnten Container. Ein CI-Webserver, Jenkins-Agenten und
Docker-in-Docker sind für diesen Einzelentwickler-Workflow bewusst nicht eingebaut.

```text
Codex / Entwickler
       │ ./ci setup | prompt | gate | quick | full | e2e | zap
       ▼
Host-Runner ─── Git-Working-Tree-Snapshot ─── .local/projects/<slug>/runs/<id>/
       │
       ├── Profil (.ci/harness.json) ─── Adapter/Stages
       ├── Gitleaks / Semgrep / Trivy
       ├── Maven (Java 21), npm (Node 24), Python (3.12) oder explizite Custom-Commands
       ├── SonarScanner ─── SonarQube ─── PostgreSQL (nur Sonar-Daten)
       └── Playwright / optional ZAP
                    │
                    ▼
           reports/<slug>/<id>/summary.json + Logs + Belege
```

Dies ist ein bewusst lokaler CLI-Harness. Push-Trigger, Webhooks, PR-Kommentare,
Remote-Runner, Benutzerverwaltung für die Pipeline und Release-Deployment fehlen.
Die Weboberfläche von Sonar ist vorhanden; für sonstige Stufen gibt es Dateiberichte.

## Lokale Projektprofile und Adapter

`harness.json` im Harness-Root bleibt die vertrauenswürdige Installationskonfiguration
(Image-Quellen, Digests, zentrale Plugins, Sonar). Eine App erhält zusätzlich ein
versionierbares `.ci/harness.json` mit Schema 2. Das Profil wählt nur Adapter, Stages,
Argument-Arrays, relative Evidence-Pfade und explizite Testwerte. Es kann weder
Images, zentrale Policies, Scannerregeln, Gates noch Host-Mounts ersetzen.

`./ci setup --repo PATH` erkennt den Git-Root und erzeugt das Profil. Mit
`--subdir apps/web` kann ein Anwendungsteil eines Monorepos versioniert werden. Die interaktive
TUI besteht ausschließlich aus Python-Standardbibliothek und `curses`; die
profilunabhängige Logik ist ohne Terminal testbar. Die Erkennung schlägt `next-fullstack`
für Next/npm, `spring-maven` für ein Spring-Boot-POM und sonst `custom` bzw. `generic`
vor. `pnpm`, Yarn, Gradle und Maven-Reactoren werden nicht still als kompatibel
behandelt. Fehlende Commands/Evidence führen im Gate zu BLOCKED.

Ein Sonar-Projektprofil wird unabhängig von der Legacy-Konfiguration provisioniert:

```text
./ci bootstrap --repo PATH
```

Der Befehl lädt `.ci/harness.json`, leitet den Projektschlüssel aus `sonar_key` oder
dem sicheren Projektslug ab, wählt für `spring-maven` Java und für Next/npm
JavaScript/TypeScript. Bei `custom`/`generic` werden `sonar_languages` oder sichere
Projektmarker verwendet; unklare Sprachen blockieren statt eine falsche Analyse zu
behaupten. Das lokale Harness-Quality-Gate und die kopierten Sonar-way-Profile werden
dem Projekt zugewiesen. Ein projektgebundener Analysis-Token wird mit `0600` unter
`.local/sonar-<slug>.token` abgelegt. `profile_projects` im Policy-Manifest enthält
nur Zuordnungen, Sprachen, Zielbranch, Profilpfad und Tokenpfad, niemals den Tokenwert.
Der Bootstrap erzeugt außerdem den lokalen Least-Privilege-Benutzer
`local-harness-reader` für die Browseransicht. Sein zufälliges UI-Passwort liegt nur
unter `.local/sonar-reader.password` mit Modus `0600` und kann ausschließlich über
`./ci sonar credentials` bewusst ausgegeben werden. Das zufällige Admin-Passwort
bleibt unter `.local/sonar-admin.password` intern und wird nicht für Browsernutzer
verwendet. User-Tokens sind API-Credentials und keine Browserpasswörter.

Der Generic-Adapter führt die zentralen Baseline-Stages aus. Der Next-Adapter führt
npm, Lint, Typecheck, Tests, Coverage, Build, Playwright und LCOV/Sonar mit expliziten
Nachweisen aus. Spring/Maven bleibt ein Single-Module-Adapter mit Surefire/Failsafe,
JaCoCo, CycloneDX, Trivy und Maven-Sonar. Der Custom-Adapter akzeptiert nur sichere
argv-Arrays; Shell-Strings und `sh -c`/`bash -c` werden abgelehnt.

## Merge-Intention und Gate-Status

Ein Gate wird nur bewusst über eine Intention ausgeführt:

```text
merge-to-main: Feature-/Arbeitsbranch, Zielbranch existiert
push-main:     Checkout steht bereits auf Zielbranch nach lokalem Merge
```

Der Harness merged oder pusht nie. Vor jedem Gate wird der aktuelle Git-Working-Tree
einschließlich uncommitted Änderungen in eine unabhängige lokale History-Kopie
gesnapshotet. Report-Metadaten enthalten Branch, HEAD, Ziel-Ref, Merge-Base,
Source-Hash, Profil-Hash und zentralen Policy-Hash.

`Runner` behält die Legacy-Pfade für `quick`/`full` bei. Profil-Gates nutzen dagegen
`reports/<project-slug>/<run-id>/` und `.local/projects/<project-slug>/runs/<run-id>/`.
`latest` wird erst nach `finish()` aktualisiert, sodass alte erfolgreiche Reports
nicht als aktueller Lauf erscheinen können.

Der Gate-Status wird aus allen Stages abgeleitet: BLOCKED hat Vorrang vor FAIL,
FAIL/ERROR vor REVIEW, REVIEW vor READY. READY ist nur ein Lauf ohne offene Warnung
oder Blocker. `gate` gibt READY mit 0 zurück, REVIEW/FAIL/BLOCKED mit 1 und
Konfigurations-/Infrastrukturfehler vor einem verwertbaren Lauf mit 2.

## Container-Lifecycle

Alle Prüf- und Buildschritte werden als kurzlebige `docker run --rm`-Container
ausgeführt. Ein Profil-Gate startet keinen App-Service vorausgesetzt; ein Playwright-
`webServer` kann die gebaute App innerhalb des Browser-Containers starten.
Auf macOS ist der Docker-Socket dabei der des konfigurierten Colima-Profils. Der
Harness beendet nach einem Gate die Job-Container und den temporären Sonar-Compose-
Stack, aber nicht die Colima-VM; deren Lebenszyklus bleibt unter Entwicklerkontrolle.

Die Compose-Dienste `postgres` und `sonarqube` sind nur für eine ausgewählte Sonar-
Stage erforderlich. `gate` startet sie erst nach erfolgreichem Snapshot-, Build-,
Test- und E2E-Teil, wartet auf `system/status=UP` und fährt sie in einem
`finally`-Cleanup mit `docker compose down --remove-orphans` wieder herunter. Die benannten Volumes
`sonar-postgres`, `sonar-data` und `sonar-extensions` bleiben absichtlich bestehen;
sie enthalten die lokale Baseline und vermeiden eine erneute Provisionierung bei jedem
Gate. `./ci up` ist weiterhin der manuelle Bootstrap-Modus und hat bewusst keinen
automatischen Shutdown.

Das externe Netzwerk `local-ci-harness` wird bei Bedarf angelegt und bleibt als
harmloses Docker-Netzwerk erhalten. Ein optionaler Test-Postgres aus
`examples/compose.test-db.yaml` oder ein ZAP-Ziel muss projektbezogen orchestriert
werden; diese V1 startet und beendet solche App-Testdienste nicht automatisch.

## Ausführung und Zustandsmodell

`main.py` enthält Adapter und CLI, `core.py` Snapshots/Evidence/Runner, `sonar.py`
Server-API und Gate-Export. Die Anwendungskonfiguration liegt in `harness.json`.
Ein `fcntl`-Lock verhindert parallele Befehle innerhalb derselben Installation.

Ein Job schreibt während der Ausführung `RUNNING`, beziehungsweise vorzeitig `FAIL`
nach einem Fehler. Erst `finish()` darf PASS setzen. BLOCKED bedeutet nicht „nicht
anwendbar“, sondern verhindert einen erfolgreichen Abschluss. ZAP-Warnings und
Semgrep-Review-Funde werden explizit als WARN transportiert. Ein finaler PASS mit
WARNs ist **kein** Befundfreiheitsnachweis.

Exit-Codes der Tools werden geprüft, danach Belege separat gelesen. Tests mit Exit 0,
aber ohne JUnit-Ausgabe oder mit nur übersprungenen Tests, reichen nicht. Coverage
wird aus Zählern berechnet, nicht aus einer behaupteten Prozent-Zeichenkette. Ein
fehlender LCOV-Bericht blockiert die Sonar-Übertragung des Frontends. Trivy muss für
Abhängigkeiten Pakete identifiziert haben; für IaC darf es keine passenden Dateien
geben. Dieser N/A-Fall besagt nichts über die Deployment-Sicherheit außerhalb des
geprüften Repository-Umfangs.

Der profilbasierte Node-Adapter setzt den Test-Reporter über `NODE_OPTIONS`, weil
`npm run` zusätzliche Argumente hinter die positionalen Testdateien anhängt und Node-
Test-Runner-Optionen dort nicht zuverlässig verarbeitet. Der Ausgabeort wird aus
`evidence.tests` abgeleitet und standardmäßig als `test-results/TEST-node.xml`
angelegt. Die Playwright-Stufe setzt zusätzlich `--reporter=list,junit,html`, schreibt
`PLAYWRIGHT_JUNIT_OUTPUT_FILE` in den konfigurierten E2E-Evidence-Bereich und nutzt
einen separaten `e2e-artifacts`-Unterordner. Damit bleiben Unit- und E2E-JUnit-Nachweise
im gemeinsamen Standardpfad getrennt und verifizierbar.

Snapshots enthalten modifizierte getrackte und nicht ignorierte neue Dateien. Vorher
und nachher wird ein Inhalts-Hash berechnet. Am Laufende wird der Original-Arbeitsbaum
erneut geprüft. Generierte `target`, `node_modules`, `.next`, Coverage- und Testausgaben
werden nicht aus dem Arbeitsverzeichnis übernommen. So kann ein früheres positives
Testergebnis nicht zufällig das neue Ergebnis liefern. Der Runner schreibt nicht in
das Original-Repo; eigentliche Buildprozesse sehen nur den Snapshot.

Ein Full-Snapshot hat eine unabhängige lokale Git-Kopie für Sonar-SCM und Gitleaks.
Git-Worktree-Verweise auf Host-Pfade werden dadurch vermieden. Hooks sind deaktiviert.
Externe oder kaputte Quellcode-Symlinks und Submodule werden abgelehnt. Interne
Quellcode-Symlinks werden relativ auf den Snapshot umgeschrieben. Artefakt-Exporte
folgen keinen erzeugten Symlinks. Trotzdem ist dies kein vollständiger Schutz vor
absichtlich manipulierten Test-/Scan-Reports oder allen möglichen Dateisystem-Races.

## Sonar: Profile versus Gate

Ein Quality Profile bestimmt die aktiven Sprachregeln. Das Quality Gate entscheidet,
welche Metriken die Abnahme blockieren. Duplikaterkennung ist keine einzelne
abschaltbare Code-Smell-Regel. Deshalb lässt der Harness diese Metrik sichtbar, nimmt
sie aber aus dem harten Gate heraus. Keine pauschale `sonar.cpd.exclusions=**/*`.

Der Bootstrap kopiert die installierten Sonar-way-Profile für Java, JS und TS. Er
aktiviert nicht wahllos alle existierenden Regeln. Kopien bleiben nach einem Update
bestehen; neue Upstream-Regeln werden nicht automatisch in die Kopie synchronisiert.
Ein Mensch muss Profiländerungen explizit prüfen. Bereits vorhandene gleichnamige
Profile gelten als bewusst gepflegte lokale Policy.

Das Gate verwendet null neue Security-/Reliability-Issues und 80 % Coverage auf neuem
Code. Die verwendeten Metriknamen hängen vom Standard-/MQR-Modus des Servers ab.
Security-Hotspot-Review wird nur konfiguriert, wenn die Metrik existiert; neuere Server
stellen das Modell um. Kompatibilität der tatsächlich gelockten Version lokal prüfen.

Für Legacy-Projekte bleibt `new_code_days` aus Gründen der CLI-Kompatibilität erhalten.
Ein profilbasierter Bootstrap setzt dagegen `REFERENCE_BRANCH` auf den im Profil
festgelegten Zielbranch und schreibt diese Entscheidung ins Policy-Manifest. Der
lokale Gate-Report enthält zusätzlich Branch, Ziel-Ref, Merge-Base und Source-Hash.
Eine Sonar-Edition mit echter Branch-/PR-Analyse kann diese Referenz direkt auswerten;
die Community-Edition muss anhand der tatsächlich gelockten Version abgenommen werden.
Die absolute lokale Coverage wird unabhängig geprüft. Eine ältere Schwachstelle kann
außerhalb des New-Code-Gates liegen und bleibt trotzdem Sicherheitsarbeit.

Analyse-Tokens haben Projektbezug. Ein separater Reader bekommt nur Projekt-Browse /
Code-Viewer-Rechte. Beim ersten lokalen Bootstrap erkennt der Harness den initialen
`admin`/`admin`-Zustand, rotiert das Admin-Passwort über `api/users/change_password`
und speichert das generierte Secret ausschließlich im Harness unter
`.local/sonar-admin.password` mit Modus `0600`. Bei bereits angepassten Instanzen
werden dieses lokale Secret, `SONAR_ADMIN_PASSWORD` oder eine verdeckte Eingabe
verwendet. Das Admin-Secret und Tokens gehören nicht in Reports oder Projektprofile.
Der Runner liest seine lokale Reader-Datei, nicht eine beliebige vom Build
vorgeschlagene Server-URL.

Das Ergebnis wird über **ceTaskId → analysisId → Quality Gate** geholt. Es wird nicht
blind die letzte eventuell alte Projektanalyse als Erfolg verwendet. Offene Issues
werden zusätzlich als JSON exportiert (maximal 10.000, darüber `truncated: true`).
Der Issue-Export ist eine Projektansicht; das Gate ist an die konkrete Analyse gebunden.

Wichtig: Der Bootstrap ändert sein eigenes Gate schrittweise. Nach einem abgebrochenen
Bootstrap ist dessen Konfiguration möglicherweise unvollständig. Der erfolgreiche Manifest-Marker wird VOR der Änderung entfernt; weitere Sonar-Läufe
blockieren ohne diesen Marker. Bis zur erfolgreichen Wiederholung/Prüfung keine
Freigabe daraus ableiten. Den Manifest-Beleg unter
`.local/sonar-policy.json` prüfen. Server-Policy-Änderungen während eines normalen
Laufs werden derzeit nicht vollständig gegen ein unabhängiges Manifest attestiert.
Der lokale Policy-Hash deckt Dateien ab, nicht jeden Serverzustand.

## Container und Vertrauensgrenzen

Job-Container laufen mit Host-UID/GID, fallen nicht auf root zurück, verlieren Linux-
Capabilities und erhalten `no-new-privileges`. Root-Dateisysteme sind überwiegend
read-only, `/tmp` ist tmpfs, die Policy ist read-only gemountet. ZAP erhält wegen seiner
Laufzeitdateien ein beschreibbares Root-Dateisystem. Maven/npm-Arbeitskopien und
explizite Cache-/Report-Verzeichnisse sind beschreibbar.

Keine automatische Freigabe von `/var/run/docker.sock`, `$HOME`, Git-/Cloud-Credentials
oder `.m2/settings.xml`. Firmen-Repositories mit privaten Dependencies benötigen einen
separaten, eng begrenzten Test-Credential-/Settings-Adapter. Niemals die gesamte
Host-Konfiguration als pragmatische Abkürzung mounten.

Der **Host-Runner selbst** darf Docker starten. Wer seine Befehle oder seine
Konfiguration verändern darf, kann diese Vorgaben umgehen. Ein read-only Mount
schützt nicht vor einem Agenten, der auf dem Host denselben Pfad bearbeiten darf.
AGENTS, Dateihashes und Containerflags sind keine voneinander unabhängigen
Sicherheitsgrenzen. Für adversarielle/unbekannte Repositories ist eine isolierte
VM/ein dedizierter Runner mit separaten Credentials nötig.

Maven-Plugins, npm-Lifecycle-Skripte und Tests sind ausführbarer Code. Gepinnte Pakete
und Images können weiterhin bösartig oder verwundbar sein. Digests gewährleisten
Identität, nicht Vertrauenswürdigkeit. Registry-/Vulnerability-Datenbanken und
Dependencies brauchen Internet. Gitleaks/Semgrep laufen ohne Container-Netzwerk;
Trivy und Buildjobs dürfen Daten herunterladen. Keine Behauptung vollständiger
Egress-Isolation oder Offline-Fähigkeit. Keine automatische Secret-Validierung gegen
Anbieter-APIs.

Bekannte Secret-Werte aus expliziten Token-/Passwort-Umgebungsvariablen werden nach
Jobende aus Logs redigiert; Gitleaks-Match/Secret-Felder zusätzlich aus JSON. Das kann
beliebige sensible Anwendungslogs, URL-Parameter, PII oder manipulierte Ausgaben nicht
zuverlässig reinigen. Reports und Snapshots privat halten.

## Maven- und Frontend-Scope

`clean verify` beinhaltet Build/Package und die konfigurierten Tests. Ein zweiter
identischer „Build“-Schritt wäre redundant. [R1] SBOM-Erzeugung und Sonar sind danach
separate Ziele. CycloneDX löst den tatsächlichen Maven-Abhängigkeitsbestand auf;
Trivy scannt diesen BOM. Das ist kein Scan eines produktiven OS-/Container-Images.

Der erste Adapter verweigert POMs mit `<modules>`. Für einen Reactor müssen JUnit-
Nachweise über Module aggregiert, JaCoCo korrekt aggregiert und Sonar mit vollständigem
Reactor-Kontext ausgeführt werden. Entweder in derselben Maven-Session oder mit
ordnungsgemäß installierten Reactor-Artefakten; nicht bloß einzelne `target`-Pfade
anpassen und den Anspruch „alle Module geprüft“ behalten. [R7]

Next.js-Adapter: npm v2/v3-Lock, keine Workspaces, feste Scripts. Das Projekt muss
`src`-Layout oder bewusst konfigurierte Sonar-/Vitest-Pfade haben. TypeScript-
`ignoreBuildErrors`, `eslint-disable`, Coverage-Ausschlüsse und Test-`skip` können
Prüfungen abschwächen; der Harness erkennt nicht jede solche Manipulation. Das bleibt
Reviewarbeit. `--disable-nosem` und die Ablehnung lokaler `.semgrepignore` verhindern
nur bestimmte einfache Scanner-Umgehungen, nicht jedes Policy-Gaming.

Die E2E-Runtime erhält ein zur gelockten Playwright-Version passendes Image.
Build und Tests werden dort erneut ausgeführt. Die Node-Version im Playwright-Image
kann von der normalen Build-Runtime abweichen: Produkt-Engines und native Dependencies
lokal gegenprüfen. Das Grundgerüst unterstützt Chromium; weitere Browser und Mobile-
Viewports gezielt ergänzen statt eine ungewartete Browsermatrix zu starten.

## Testcontainers, Deployment und nächster Ausbau

Ein Socket-Mount wäre der einfache, aber hochprivilegierte Weg für verschachtelte
Testcontainers. Er ist **nicht** aktiviert. Mögliche bewusste Erweiterungen sind ein
separater Docker-Daemon in einer isolierten VM oder ein vertrauenswürdiger Host-
Testadapter mit Testcontainers. Remote-Daemon-Netzwerkpfade, veröffentlichte Ports und
Ryuk müssen dabei korrekt aufgelöst werden. Tests nicht als Workaround deaktivieren.

Produktions-Dockerimages werden noch nicht gebaut oder mit `trivy image` geprüft.
Ein sinnvoller Folgeadapter baut das echte App-Image, scannt dessen OS-/Runtime-
Pakete, startet genau dieses Image im isolierten Testnetz und führt Health-/API-/
Browser-Tests aus. Für Spring zusätzlich Migrationen gegen eine Datenbankkopie mit
synthetischen Daten; für Next SSR/Server Actions und Auth-Flows tatsächlich ausführen.

Empfohlene Reihenfolge der Weiterentwicklung: echte App-Adapter abnehmen; fachliche
Sicherheitstests; Deployment-Image-/Runtime-Adapter; Architecture-/Contract-Tests;
gezielte Mutationstests; erst danach parallele Jobs, PR-Integration oder zusätzliche
Scanner. Ein geschützter CI-Lauf auf dem zu mergenden Commit bleibt die unabhängige
Kontrolle gegenüber der veränderlichen lokalen Arbeitsumgebung.
