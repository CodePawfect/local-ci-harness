# Tatsächlicher Prüfstand

Erstellt: **20. September 2026**.

## In dieser Umgebung ausgeführt

`python3 -m unittest discover -s tests -v`: **96 Tests erfolgreich**.

Die Tests verwenden echte temporäre lokale Git-Repositories, modifizierte und neue
Dateien, unabhängige History-Kopien, Linked Worktrees und Symlinks. Geprüft werden
außerdem fehlende/fehlerhafte/übersprungene JUnit-Ergebnisse, Coverage-Zähler,
Semgrep-/Trivy-/Gitleaks-Belege, Secrets-Redaction, ZAP-Ziel-/Report-Prüfung,
Konfigurationsverträge, Projektprofile, Merge-Intent-Prüfungen, TUI-Zustände,
namespaced Reports, JSON-/Markdown-Zusammenfassungen sowie profilbasierte Sonar-
Provisionierung ohne Legacy-Projektliste sowie der Read-only-Sonar-UI-Zugang wird mit einer
gemockten Sonar-API geprüft: Projektschlüssel, Sprachprofile, Gate-Zuordnung,
Reference-Branch, Token-Datei, UI-Passwortdatei und Manifest werden kontrolliert.
Zusätzlich wird geprüft, dass Node-JUnit-Optionen über `NODE_OPTIONS` vor den
positionalen Testargumenten injiziert werden und dass Playwright JUnit, HTML und
isolierte E2E-Artefakte als argv-/Umgebungsparameter erhält.
Der profilbasierte Sonar-Lifecycle wird unit-getestet: Compose-Start und Cleanup
liegen um den Gate-Lauf; der tatsächliche Containerstart bleibt Infrastrukturabnahme.

Docker-Prozessaufrufe sind in diesen Tests **gemockt**. Geprüft sind Argumentbildung,
Nichtweitergabe von Tokenwerten in argv, read-only Policy, Nichtmounten des
Docker-Sockets, fehlschlagende Exit-Codes und Cleanup bei Timeout. Die Sonar-API
ist ebenfalls **gemockt**; geprüft sind Gate-Parameter-Kompatibilität und die
Zuordnung über genau die aktuelle analysisId, einschließlich negativer Fälle.

Zusätzlich: Python-Kompilierung, Shell-Syntax des Launchers, JSON-/YAML-/TOML-/XML-
Syntaxprüfung und JavaScript-Syntaxprüfung der ESLint-Konfiguration. Die Datei
`harness-test-output.txt` enthält einen historischen Starterlauf; der oben genannte
Testbefehl ist der maßgebliche aktuelle Nachweis.
Die Runtime-Auswahl und der Colima-Start werden mit gemockten Prozessaufrufen
geprüft; ein echter Colima-/Docker-Start bleibt Infrastrukturabnahme.

## Tatsächliche lokale Integrationsabnahme

Die Läufe wurden am 20. September 2026 mit Colima `default` (aarch64, 2 CPUs,
4 GB RAM) und den gelockten Images ausgeführt. Der profilbasierte Bootstrap für
`seo-orbiter` provisionierte Projekt, Quality Profiles, Quality Gate,
Reference-Branch und Tokens. Eine frische Sonar-Instanz wurde dabei automatisch
von `admin/admin` auf ein lokales Secret unter `.local/sonar-admin.password`
rotiert; kein Browser-Passwortwechsel war erforderlich. Der bestehende
`seo-orbiter`-Reader wurde anschließend real auf den Read-only-UI-Zugang
provisioniert; `./ci sonar credentials` lieferte URL und Login, ohne das
Admin-Passwort auszugeben.

Im letzten `push-main`-Lauf `20260920T160922-78ef748e` bestanden Install,
Secrets, Semgrep/SCA/IaC, Lint, Typecheck, Unit-Tests, Build, Coverage sowie E2E.
Die SonarQube-Instanz startete ebenfalls. Die JS/TS-Analyse scheiterte jedoch
am Sonar-JavaScript-Bridge-Prozess mit einem Speicherfehler unter der 4-GB-VM;
es wurde daher bewusst kein Sonar-Quality-Gate als bestanden behauptet. Für eine
vollständige React/TypeScript-Sonar-Abnahme muss Colima für den Analyseabschnitt
mehr Speicher erhalten oder der Scanner außerhalb dieser VM laufen.

Damit sind echte Docker-/Next-/Playwright-/Gitleaks-/Trivy-/Semgrep-Läufe bewiesen;
die Sonar-Analyse und ein Sonar-Quality-Gate bleiben wegen dieser Infrastrukturgrenze
offen. ZAP und Maven wurden in dieser Abnahme nicht ausgeführt.

Der Harness trennt dieses Ressourcenproblem nun explizit: normale Job-Container
bleiben standardmäßig auf `4g` begrenzt, der Sonar-Scanner erhält standardmäßig `6g`
und vor dem Sonar-Start werden mindestens `8 GiB` Docker-Runtime-Speicher verlangt.
Der neue Preflight wurde unit-getestet; ein erneuter echter Sonar-Lauf mit der
erhöhten Colima-Konfiguration ist noch nicht Bestandteil dieser Dokumentation.

Die App-Vorlagen sind keine vollständigen Apps. Es sind noch keine fachlichen
Backend-/Frontend-Sicherheitsregressionen für dein Produkt geschrieben. Die 96 Tests
verifizieren den Harness, **nicht** die Sicherheit oder Funktion deiner Anwendung.

## Lokale Erstabnahme

1. `./ci init`, Pfade und Projektadapter integrieren; für ein App-Repository
   `./ci setup --repo PATH` und danach `./ci prompt --repo PATH` ausführen.
2. `./ci doctor` ausführen.
3. `./ci lock-images`, Digests/Architektur prüfen, `./ci up`,
   `./ci bootstrap --repo PATH`; bei einer frischen Instanz rotiert der Bootstrap
   das Default-Admin-Secret automatisch. Projekt, Manifest, Profile, Reference-Branch und
   Token-Zuordnung im lokalen Sonar prüfen.
4. `./ci quick backend` und `./ci quick frontend` gegen echte kleine Testfälle.
5. `./ci full backend` / `./ci full frontend`; JUnit, Coverage, Paketbestand und exakt
   zugehörige Sonar-Analyse kontrollieren. Ersten Bestandsbefund manuell triagieren.
6. Für ein Projektprofil `./ci gate --repo PATH --intent merge-to-main` auf einem
   Feature-Branch und nach lokalem Merge `./ci gate --repo PATH --intent push-main`
   gegen die korrekte namespaced `summary.json` prüfen.
7. In einer Wegwerf-Testkopie absichtlich einen Unit-Test fehlschlagen lassen und
   danach Testberichte entfernen: beides muss rot sein. Einen harmlosen SAST-
   Regeltrigger als isolierte Fixture prüfen; anschließend die Änderung zurücknehmen.
8. Bei Bedarf `./ci lock-images --runtime`, `./ci e2e frontend` und ZAP auf einem
   eigenen, lokalen Testdeployment. Authentifizierte Routen separat abdecken.

Nicht durch Gate-Abschwächungen „abnehmen“. Ein echter Tool-Fehler muss behoben oder
als offene Integrationsarbeit benannt werden. Nie reale Secrets als Fehlertest verwenden.

## Bekannte funktionale Grenzen

Nur ein Maven-Modul, npm ohne Workspace-Adapter, keine Submodule. Der profilbasierte
Sonar-Projektstream wird lokal angelegt; ein automatischer PR-/Branch-Vergleich hängt
weiterhin von der tatsächlich gelockten Sonar-Edition ab. Kein Host-Socket für
Testcontainers. `full` enthält nicht die separaten Browser-/ZAP-Läufe. Noch kein
Build/Scan der produktiven App-Containerimages. Keine zuverlässige automatische
Feststellung, ob fachliche Tests hinreichend sind oder absichtlich abgeschwächt wurden.
