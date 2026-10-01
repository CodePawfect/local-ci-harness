**Umsetzungsplan: stabile Codequalität für Coding-Agenten**

Stand: 1. Oktober 2026. Ausgangspunkt: Commit `0aa233aa2195`.
Status: geplant; dieses Dokument implementiert keine neuen Prüfungen.

Das Ziel ist ein Codebestand, den ein Coding-Agent mit begrenztem Kontext
zuverlässig ändern kann. Die Harness soll einheitliche Sprachidiome, explizite
Schnittstellen, klare Modulgrenzen und nachvollziehbare Reparaturhinweise prüfen.
Deterministisch sind die festgelegten Prüfungen und ihre Eingaben; umfassende
Lesbarkeit und gute fachliche Zuständigkeiten bleiben auch Review-Aufgaben.

**Ausgangslage und verbindliche Grenzen**

- `harness/quality.py` erzwingt Pflichtstages und Coverage-Untergrenzen, aber
  keine konkreten Sprachregeln. Der moderne Runner führt `commands.format`
  bislang nicht aus.
- Lint-/Typecheck-Kommandos stammen aus dem Anwendungsprofil. Ein erfolgreicher
  Exit belegt allein noch keinen bestimmten Regelsatz oder vollständigen Umfang.
- Sonar übernimmt vorhandene Profile anhand ihres Namens. Ein Sollvergleich
  aller aktiven Regeln und Parameter fehlt. Wartbarkeit und Duplikation sind
  ausdrücklich `report-only`.
- Die Sonar-Gateabfrage ist an `analysisId` gebunden. Der zusätzliche Issueexport
  fragt dagegen den aktuellen Projektzustand ab und kann abgeschnitten sein.
  Dieser Export darf nicht ungeprüft als Befundliste derselben Analyse gelten.
- Python hat kein verpflichtendes Sprachprofil. Die bisherigen Semgrep-Regeln
  ersetzen keine allgemeine Sprach- oder Wartbarkeitsprüfung.
- Bestehende Sicherheitsregeln, Coverage-Untergrenzen von 70 % Zeilen/60 %
  Branches, Tests, Timeouts und Gatebedingungen bleiben erhalten. Keine neuen
  Baselines oder Ausnahmen für Sicherheitsbefunde.
- Jobs bleiben digestgebunden, ohne Host-Docker-Socket, mit lokal archivierten
  Reports und Snapshots einschließlich uncommitteter Änderungen. Nichtzero-Exits
  und die Unterscheidung von `FAIL`, `BLOCKED`, `ERROR` und `WARN` bleiben erhalten.
- Das bleibt eine lokale Verification Harness und kein Sandboxnachweis gegen
  absichtlich bösartige Programme oder gefälschte Toolausgaben.

**Umsetzung in sechs aufeinander aufbauenden Paketen**

| Paket | Ergebnis | Voraussetzung |
|---|---|---|
| 1 | Policyvertrag, Formatstage und einheitliche Befunde | Bestehender Runner |
| 2 | Verbindliches JS-/TypeScript-Profil | Paket 1 |
| 3 | Verbindliches Python-Profil und Anwendung auf den Harness | Paket 1 |
| 4 | Festgeschriebenes Java-/Sonar-Profil mit überprüfter Analysezuordnung | Paket 1 |
| 5 | Architektur- und Wartbarkeitsgates | Pakete 2–4 für den jeweiligen Stack |
| 6 | Schnelle, klar begrenzte Prüfschleife für Agenten | Pakete 1–5 |

Pakete 1–3 bilden das erste nutzbare Lieferpaket. Ein einzelnes Paket gilt erst
nach seinen realen Integrationsprüfungen als fertig. Jede Änderung bleibt separat
reviewbar; die Einführung neuer Pflichtprüfungen wird in der Policy dokumentiert.

**Paket 1: Policyvertrag, Formatierung und Befundschema**

Betroffen: `harness/quality.py`, `harness/project.py`, `harness/main.py`,
`harness/core.py`, `policy/quality.json`, `tests/test_quality.py`,
`tests/test_project.py` und `tests/test_core.py`. Neue Verantwortung für
Toolbefunde in einem kleinen separaten Modul, statt weiterer Parser im CLI-Modul.

1. Zentrale, versionierte Sprachprofile unter `policy/languages/` definieren:
   Profil-ID, erlaubte Toolversionen, Pflichtregeln, Parameter, Sprachversion,
   Eingabeumfang und verpflichtende Teilprüfungen. Projektprofile wählen daraus
   und ergänzen strengere Regeln; sie dürfen Pflichtregeln nicht abschalten.
2. Anwendungssprachen und Quellroots gegen das Snapshotinventar prüfen.
   `custom` oder `generic` darf für unterstützte Sprachen keine Umgehung sein.
   Unklare Mischprojekte brauchen eine eindeutige Zuordnung. Nicht unterstützte
   Sprachprüfungen werden als nicht abgedeckt ausgewiesen; eine zentral
   erforderliche, nicht verfügbare Prüfung blockiert das Gate.
3. Eine echte Formatstage ergänzen und für unterstützte Sprachprofile zentral
   verlangen. Bestehendes `commands.format` integrieren; der verbindliche Nachweis
   kommt vom festgelegten Formatter. Gatejobs prüfen ausschließlich, ohne Quellen
   automatisch zu formatieren oder zu reparieren.
4. Neue Profileigenschaften mit expliziter Schema-/Migrationsstrategie einführen.
   Alte Profile bleiben lesbar oder erhalten eine konkrete Migrationsmeldung.
   Fehlende neue Pflichtprüfungen ergeben `BLOCKED`, niemals einen schwächeren
   kompatiblen `READY`-Pfad. Setup schreibt keine Anwendungskonfigurationen um.
5. `findings.json` mit Schema-Version einführen: Tool, Regel-ID, Kategorie,
   Schweregrad, repo-relativer Pfad, Position, Nachricht und Fingerprint.
   Run-ID, Producer, Source-/Policyhash und Rohreporthash gehören zur Evidenz.
   Sortierung und Fingerprints sind stabil; Laufzeit und Run-ID gehören nicht
   zum Befundfingerprint. Änderungen an Fingerprintregeln versionieren.
6. Reparaturhinweise und Prüfcommands aus vertrauenswürdigen Harness-Rezepten
   ableiten. Scannertexte und Quellkommentare bleiben untrusted data und werden
   nicht als Agentenanweisungen oder ausführbare Kommandos übernommen.
7. Frische Reports pro Producer archivieren. Null Befunde sind erlaubt, müssen
   aber mit erfolgreich geprüften Eingaben belegt sein. Fehlende, ungültige oder
   abgeschnittene Pflichtreports und unerwartet leerer Scanumfang bestehen nicht.

Abnahme: zuerst Regression für den nicht ausgeführten Formatcheck; danach
Nachweise für fehlendes Formatkommando, kaputten Report, alten Report,
Pflichtregelabschaltung, unerlaubten Ausschluss, falsche Toolversion und
Adapterwechsel als Umgehungsversuch. Gleiche Eingaben müssen dieselben
normalisierten Befunde ergeben. Richtige Formatierung plus absichtlicher
Formatfehler werden mit einem echten Formatter im Container geprüft.

**Paket 2: JavaScript und TypeScript**

Betroffen: zentrale Sprachprofile/Toollocks, `templates/frontend/`, Runner und
neue kleine JS-/TS-/Next-Fixtures einschließlich Lockfiles.

- JS: explizite ESLint-Regeln auf Basis der empfohlenen Korrektheitsregeln und
  ein verbindlicher Prettier-Check. Für reines JS wird eine fehlende TS-Prüfung
  nur dann als nicht anwendbar ausgewiesen, wenn das zentrale Sprachprofil dies
  ausdrücklich so festlegt.
- TS: `strict` und `noUncheckedIndexedAccess`; typgestütztes ESLint auf Basis von
  `recommendedTypeChecked`, mindestens mit Prüfungen gegen unbehandelte/falsch
  verwendete Promises und unsichere Typverwendung. Vorhandene Next-/React-Regeln
  bleiben erhalten.
- Die Harness ruft die geprüften Tools mit eigenen Argumentarrays und zentraler
  Policy auf. Projekt-Lintskripte können zusätzlich laufen, ersetzen den Nachweis
  aber nicht. Effektive Konfigurationen für alle relevanten Konfigurationsbereiche
  sowie Compileroptionen nach Auflösung von `extends` erfassen und validieren.
- Quellinventar, tatsächlicher Scanumfang, Ignore-Muster und Inline-Suppressions
  berücksichtigen. Keine globale `eslint-disable`- oder Typprüfungsumgehung.
  Enge Ausnahmen brauchen dokumentierte, zentral geprüfte Gültigkeit.
- Toolabhängigkeiten separat exakt locken und ihre tatsächliche Version prüfen.
  Kein `npx`-Download unbekannter Versionen und kein automatischer Installfallback.
  Installation erfolgt explizit; Prüfungen nutzen vorbereitete, gepinnte Tools.

Abnahme: echter JS-/TS-Lauf sowie minimale Next-Integration. Fixtures für
unbehandeltes Promise, unsicheren Zugriff, gelockerte Compileroptionen,
Datei-Overrides, Inline-Abschaltung, Formatfehler und korrekten Code. Parserfehler
und nicht aufgelöste Imports dürfen nicht als sauberer Scan gelten. Die bestehende
native Node-Integration bleibt erhalten und bekommt den passenden JS-Nachweis.

**Paket 3: Python und Selbstprüfung des Harness**

Betroffen: Python-Sprachprofil, exakt gelockte Qualitätswerkzeuge, Runner,
Python-Fixture und die Python-Konfiguration des Harness.

- Ruff mit expliziter Auswahl aus `F`, `E4`, `E7`, `E9`, `I`, `B`, `UP` und
  `ruff format --check`. Ein erster Satz idiomatischer Regeln prüft beispielsweise
  mutable Defaultargumente, unbenutzte Variablen und veraltete Sprachkonstrukte.
- mypy mit nachvollziehbaren Modulroots und strengen Optionen für die vereinbarten
  vollständig typisierten Module. Eine Migration von Altcode weist verbleibende
  Lücken ausdrücklich aus; sie darf keine vollständige Typprüfung behaupten.
- Mindestsprachversion des Harness bleibt Python 3.10. Der gepinnte Runner mit
  Python 3.12 darf nicht versehentlich 3.12-only-Quellcode erlauben.
- Python-Kommandos erhalten einen dedizierten, kontrollierten Toolpfad und
  gelockte Abhängigkeiten. Kein globales Ignorieren fehlender Typinformationen,
  breites `noqa` oder `ignore_errors`, um einen Lauf grün zu bekommen.
- Im Harness gefundene Fehler beheben, ohne bestehende Testassertionen oder Gates
  zu reduzieren. Typisierungsarbeiten in überschaubare Änderungen aufteilen.

Abnahme: echte Containerläufe für korrekten Code, mutable Defaults, Syntaxfehler,
Typfehler, unerlaubte Suppressions und falsche Targetversion. Ruff, Formatter und
mypy werden auch auf dem vereinbarten Harness-Umfang ausgeführt. Umfang und
verbleibende Typisierungslücken sind im Report sichtbar.

**Paket 4: Java und überprüfbare Sonar-Policy**

Betroffen: `harness/sonar.py`, Maven-Template, Sprachprofile/Toollocks,
`tests/test_adapters.py` und eine echte Single-Module-Maven-Fixture.

- Zuerst die Fähigkeiten des bereits gepinnten lokalen Sonar-Images tatsächlich
  ausführen und erfassen: Analyzer-Versionen, Rule-API, MQR-/Standardmetriken,
  Quality-Gate-API und New-Code-Definition. Dokumentation allein ist kein
  Integrationsnachweis. Fehlende notwendige Funktionen blockieren die betroffene
  Prüfung; keine automatische schwächere Variante.
- Aktive Java-/JS-/TS-Sonar-Regeln mit Parametern und wirksamen Schweregraden
  vollständig exportieren, kanonisieren und gegen einen zentralen Sollzustand
  prüfen. Name und Regelanzahl reichen nicht. Policy vor und nach der Analyse
  verifizieren; Drift invalidiert den Lauf.
- Java erhält einen exakt gelockten Formatter und ein verbindliches geprüftes
  Sonar-Sprachprofil. Compilation, JUnit und JaCoCo bleiben eigenständige Belege.
  Für dieses Java-Profil ist Sonar erforderlich; daraus entsteht keine globale
  Sonarpflicht für alle anderen Stacks.
- Die aktuelle `analysisId`-Gatezuordnung bewahren. Befunde erst nach belegter
  Zugehörigkeit zur selben Analyse normalisieren. Dafür Capabilityprüfung,
  Analysezuordnung und gegebenenfalls Serialisierung je Sonarprojekt implementieren.
  Projektzustand allein wird nicht als historische Analyse ausgegeben.
- Vollständige Pagination sowie Fehlermeldungen für abgeschnittene oder während
  des Exports überholte Daten implementieren. Keine grünen Pflichtreports aus
  unvollständigen Daten.

Abnahme: echter Maven-/Sonar-Lauf mit gutem Code und gezieltem Regelverstoß;
zusätzlich manipuliertes Profil, fehlender Analyzer, falsche Analysezuordnung,
unvollständige Pagination und parallel überholte Analyse. Mocktests ergänzen
die API-Fehlerfälle, ersetzen diese Integrationsnachweise aber nicht.

**Paket 5: Architektur und Wartbarkeit**

Betroffen: neue zentrale Architektur-/Wartbarkeitspolicy, Fixtureverträge,
Tooladapter und Sonargates. Projektabhängige Modulregeln werden als Daten
beschrieben und gegen die zentrale Policy geprüft.

- Kleine, projektspezifische Verträge: keine unerlaubten Modulzyklen, festgelegte
  Abhängigkeitsrichtungen, definierte Modulzugänge. Beispiel: Fachkern hängt
  nicht von Infrastruktur ab; UI greift nicht direkt auf Persistenz zu.
- JS/TS: dependency-cruiser; Java: ArchUnit. Für Python einen passenden
  Importgraphprüfer nach einem kleinen Capabilitynachweis festlegen. Keine
  pauschale neue Schichtenarchitektur für bestehende Projekte.
- Geplante Startwerte zur Kalibrierung: kognitive Komplexität pro Funktion 15,
  Verschachtelung 4, Parameteranzahl 5, Duplikation im neuen Code 3 %.
  Das sind Vorschläge, noch keine aktiven Grenzwerte. Messverfahren je Sprache
  benennen; kognitive und zyklomatische Komplexität nicht gleichsetzen.
- Neue Wartbarkeitsbefunde und ihre Gatewirkung explizit festlegen. Eine gute
  aggregierte Sonarbewertung ersetzt kein Gate auf ausgewählte Einzelregeln.
  Sicherheits-/Reliability-/Coveragebedingungen werden nicht reduziert.
- Für "neuen Code" eine unveränderliche Referenz erfassen. Bei `merge-to-main`
  ist der konkrete Vergleichscommit maßgeblich; für `push-main` ist eine echte
  frühere Referenz nötig, nicht der Vergleich von `main` mit sich selbst.
  Uncommittete Änderungen, Umbenennungen und komplette betroffene Funktionen
  berücksichtigen. Fehlende Referenz ergibt `BLOCKED` für die verlangte Prüfung.
- Altbefunde vollständig sichtbar machen. Keine automatisch erzeugte Baseline,
  die alle vorhandenen Befunde ausblendet. Eine bewusst beschlossene
  New-Code-Wartbarkeitspolicy lässt Sicherheits-, Test- und Coveragegates intakt.
  Änderungen an bestehenden Gates oder Ausnahmen bleiben owner-reviewpflichtig;
  bestätigte False Positives folgen der bestehenden engen Dokumentationsregel.

Abnahme: je unterstütztem Stack korrekte und verbotene Abhängigkeit, Zyklus sowie
Wartbarkeitsfälle unmittelbar unter/an/über dem festgelegten Grenzwert. Echte
New-Code-Fälle auf Featurebranch und `main`, uncommittete Änderungen,
Umbenennungen, fehlende Referenz und unterscheidbarer Altbestand. Neue
Gatebedingungen erst nach tatsächlichen Sonar-/Toolläufen übernehmen.

**Paket 6: schnelle Prüfschleife**

Betroffen: CLI, gemeinsamer Pipelineaufbau, Reports und
`harness/project.py::render_agent_prompt`.

- `./ci check --repo ...` benötigt keinen Merge-/Push-Intent und führt den zentral
  festgelegten schnellen Satz aus: Formatierung, Sprachregeln, Typprüfung und
  statisch ausführbare Architekturregeln. Benötigte Build-/Installabhängigkeiten
  sind explizit; ein nicht ausführbarer verlangter Check wird nicht übersprungen.
- Dieselben Adapter, Konfigurationen und Befundparser wie im vollständigen Gate
  verwenden. Der Vollgate darf keine schwächere zweite Implementierung besitzen.
- Zu Beginn vollständige relevante Quellroots prüfen. Eine spätere inkrementelle
  Optimierung braucht einen gesonderten Nachweis für abhängige Module; kein
  bloßer Dateidiff als Ersatz für die Typ-/Architekturprüfung.
- Mode `check`, klare Checkabdeckung und bewusst nicht ausgeführte Vollgatestages
  melden. Nie `gate.status=READY` und kein nutzbarer Merge-/Push-Freigabebeleg.
- Fehlgeschlagene, blockierte und fehlerhafte Checks sowie Reviewbedarf haben
  Nichtzero-Exits. Ein erfolgreicher Schnellcheck bescheinigt ausschließlich
  seinen angegebenen Umfang.
- Agentenprompt verwendet konkrete Runreports und trusted Reparaturrezepte,
  bewahrt die Stopregel nach drei erfolglosen Reparaturen und autorisiert weder
  automatische Commits/Pushes noch Policyabschwächungen.

Abnahme: Intent-unabhängiger Check, gleiche Befunde wie im entsprechenden
Vollgateteil, keine READY-Verwechslung, vollständige ausgewählte Teilchecks,
sichtbare Infrastrukturfehler und kein Zugriff auf einen veralteten `latest`-Run.
Laufzeiten erfassen; keinen unbelegten Geschwindigkeitswert versprechen.

**Verifikation und Lieferung**

- Nach jeder Harnessänderung:
  `python3 -m unittest discover -s tests -v`.
- Bestehende reale Regression:
  `python3 tests/verify_integration.py`.
- Neue Fixtureläufe für Format, JS/TS/Next, Python, Maven/Sonar und Architektur
  separat ergänzen. Sie installieren keine fehlenden Tools stillschweigend und
  überspringen keine Infrastrukturfehler.
- Jede Pflichtregel braucht mindestens einen echten positiven und negativen
  Fixturefall. Jeder Nachweisvertrag braucht zusätzlich einen fehlenden oder
  manipulierten Nachweisfall.
- Wiederholung identischer lokaler Eingaben mit gleicher Policy/Toolchain muss
  denselben normalisierten Befundsatz und dieselbe Entscheidung ergeben.
  Unterschiedliche Run-IDs, Zeiten und Laufzeiten sind erwartbar.
- Für jedes Paket: Diffreview, Hosttests, passende reale Integrationen,
  Report-/Run-ID-Nachweise, dann eine separat reviewbare Lieferung. Commit/Push
  ist kein Teil dieses Planungsauftrags.
- Abschlussberichte nennen tatsächlich ausgeführte Commands, Ergebnisse,
  Run-IDs und nicht ausgeführte Stacks. Die bisherige Node-Integration wird nicht
  als Next-, Java-, Browser- oder Sonarnachweis verwendet.

Nicht Teil dieser Ausbaustufe: synthetischer Mergebaum, vollständiger Offline-
Replay, neue Adapter für sämtliche Sprachen, automatische Quellreparaturen,
Mutationstests für jede Anwendung oder eine Garantie fachlicher Codequalität.
Diese Grenzen werden weiterhin in README und Reports ausgewiesen.

**Technische Referenzen für die Umsetzung**

- [typescript-eslint: Shared Configs](https://typescript-eslint.io/users/configs/)
- [TypeScript: noUncheckedIndexedAccess](https://www.typescriptlang.org/tsconfig/noUncheckedIndexedAccess.html)
- [Ruff: Regeln](https://docs.astral.sh/ruff/rules/)
- [mypy: Kommandozeile](https://mypy.readthedocs.io/en/stable/command_line.html)
- [dependency-cruiser](https://github.com/sverweij/dependency-cruiser)
- [ArchUnit: User Guide](https://www.archunit.org/userguide/html/000_Index.html)
- [Sonar: Quality Gates](https://docs.sonarsource.com/sonarqube-server/2026.1/quality-standards-administration/managing-quality-gates/introduction-to-quality-gates)

Die tatsächlich gepinnten Versionen und Fähigkeiten werden bei der Umsetzung
festgelegt und ausgeführt. Diese Referenzen ersetzen keine Laufnachweise.
