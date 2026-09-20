# Recherche und Entscheidungen

Stand: **17. September 2026**. Offizielle Dokumentation wurde zur Planung gelesen;
die Container-/Server-Kompatibilität wurde hier mangels Docker nicht praktisch
nachgewiesen. Die Ausgangstags werden erst auf dem Zielsystem zu Digests aufgelöst.
Keine unüberprüften Image-Hashes oder behaupteten realen Scanergebnisse liegen bei.

## Ergebnis zur Machbarkeit

Der geplante Workflow ist technisch sinnvoll als lokaler, wiederholbarer
Verifikationszyklus: Code ändern, unabhängig ausführbare Checks starten, strukturierte
Belege lesen, Fehler beheben und erneut prüfen. Der Mehrwert liegt in diesen
Rückmeldungen, nicht allein im Besitz eines Sonar-Servers. Fachliche Akzeptanzfälle,
Sicherheits-Regressionstests und ein geschützter letzter Merge-Gate bleiben nötig.
Das ist die Architekturentscheidung dieses Projekts, keine gemessene Produktivitäts-
oder Fehlerreduktionszahl.

## Warum diese Auswahl?

Maven besitzt bereits den passenden Lifecycle. `verify` führt die vorausgehenden
Phasen aus; Integrationstests benötigen die richtige Plugin-Bindung. Deshalb keine
redundante zweite Kompilierung mit der Überschrift „Build“. [R1]

Gitleaks prüft Arbeitsstand und geklonte Git-Historie. Sonar liefert Sprachregeln und
eine lokal sichtbare Projektanalyse. Semgrep ergänzt wenige versionierte Sicherheits-
Patterns, ohne ein vollständiges OWASP-Regelpaket zu behaupten. CycloneDX und Trivy
liefern die Abhängigkeits-/Fehlkonfigurationsperspektive. Semgreps gewöhnlicher Scan
liefert nicht standardmäßig bei jedem Fund einen Fehlercode: deshalb liest der Runner
Severity und Parserfehler explizit aus dem JSON. [R2, R10, R11, R12]

Bei Next.js werden Linting, Typecheck, Unit-Tests und Produktionsbuild als eigenständige
Schritte behandelt. Die Next-16-Änderung am Linting und die Einschränkungen beim Testen
asynchroner Server Components sind Gründe für die getrennten Stufen. [R3, R4]

Playwright deckt reale Browserflüsse ab und benötigt eine passende Browser-/Paketversion.
ZAP-Baseline betrachtet erreichbare HTTP-Oberflächen passiv; Authentifizierung,
OpenAPI-Import und aktiver Scan sind hier keine implementierte Vollabdeckung. [R5, R8]

„Alles per Agent schreiben“ ist nicht dasselbe wie „alles ungeprüft vom Agenten
freigeben“. AGENTS definiert das Arbeitsverfahren. Technische Rechte, unabhängige
Akzeptanztests und ein unveränderlicher CI-Gate müssen separat umgesetzt werden. [R9]

## OWASP Top 10:2025 – eigene Zuordnung des Prüfplans

OWASP beschreibt zentrale Risikokategorien, kein einzelnes Tool, das sie vollständig
abhaken könnte. [R13] Die folgende Zuordnung ist ein **eigener Testplan**. „Vorgesehen“
ist nicht gleich „im Starter bereits automatisch abgedeckt“.

| Kategorie | Vorgesehene Evidenz / verbleibende fachliche Arbeit |
|---|---|
| A01 Broken Access Control | Rollen-/Mandanten-/Objektberechtigungs-Tests; vom Produktteam zu ergänzen |
| A02 Security Misconfiguration | Trivy-IaC + passive HTTP-Befunde; tatsächliches Deployment zusätzlich prüfen |
| A03 Software Supply Chain Failures | Gelockte Abhängigkeiten/Images, BOM und CVEs; Provenance/Malware nicht umfassend abgedeckt |
| A04 Cryptographic Failures | Teilweise statische Regeln; TLS, Passwortspeicherung, Schlüssel- und Sessionkonzept prüfen |
| A05 Injection | Sprachregeln, Eingabe-/Ausgabe-Regressionen; Zusatz-Semgrep-Regeln sind nur Teilabdeckung |
| A06 Insecure Design | Bedrohungsmodell, Geschäftsregeln, Missbrauchsfälle; keine generische Scannerentscheidung |
| A07 Authentication Failures | Login-/Token-/Session-/Rate-Limit-Tests; noch projektspezifisch einzurichten |
| A08 Software or Data Integrity Failures | Signaturen, Webhooks, Replay, Artefaktvertrauen; nicht allein durch Image-Pinning gelöst |
| A09 Security Logging and Alerting Failures | Audit-Ereignisse, sensible Logdaten, Alarmierung tatsächlich testen |
| A10 Mishandling of Exceptional Conditions | Timeouts, ungültige Daten, Rollback, fail-closed und Ressourcenlimits testen |

Ein verschwundener High-CVE-Befund beweist keine korrekte Autorisierung. Ein fehlender
SAST-Fund beweist kein sicheres Design. Eine 80-%-Coverage beweist keine wirksamen
Assertions. Als konkrete Ergänzung liegt `docs/security-test-contract.md` bei.

## Sonar-Policy bewusst nicht maximal streng

Kopierte Standardprofile bilden einen nachvollziehbaren Ausgangspunkt. Harte Gates
konzentrieren sich hier auf neue Security-/Reliability-Issues und Coverage. Duplikate
und allgemeine Maintainability bleiben sichtbar. Das vermeidet den Anreiz, verständliche
Codeblöcke ausschließlich zur Senkung einer Metrik künstlich zusammenzuziehen.
False Positives werden begründet geprüft, nicht durch pauschale Source-Ausschlüsse
entsorgt. Diese Gewichtung ist eine Produktentscheidung dieses Harness, nicht die
unveränderte Sonar-way-Gate-Vorgabe. [R6]

## Versionierung und Update-Politik

Maven-Sonar-Plugin ist in der Vorlage explizit auf `5.8.0.7211`, CycloneDX auf `2.9.1`
und das JaCoCo-Template auf `0.8.13` gesetzt. Das sind bewusste Startwerte, kein
Versprechen, dass jede Komponente „immer die neueste“ ist. Framework-Parent,
Compiler-/Test-Plugins und npm-Lockfile bleiben Teil der App-Konfiguration. [R7, R11]

Scanner-Images werden mit Digest eingefroren. Neue CVE-Daten dürfen sich dagegen bei
Trivy aktualisieren: identischer Source kann durch neue Erkenntnisse später rot
werden. Das ist erwünscht. Registry-Rulepacks nur nach Quellen-/Lizenz-/Versionsreview
als lokale Regeln ergänzen. Kein heimliches dynamisches `--config auto` in der Pipeline.

## Offizielle Quellen

[R1] Apache Maven: Build Lifecycle und Failsafe-Nutzung.
- https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html
- https://maven.apache.org/surefire/maven-failsafe-plugin/usage.html

[R2] Gitleaks: CLI, Git-/Directory-Modi, JSON, Redaction und Konfiguration.
- https://github.com/gitleaks/gitleaks

[R3] Next.js: Upgrade Guide für Version 16, eigenständiges Linting.
- https://nextjs.org/docs/app/guides/upgrading/version-16

[R4] Next.js: Vitest, React Testing Library und Async-Server-Component-Einschränkung.
- https://nextjs.org/docs/app/guides/testing/vitest

[R5] Playwright: Docker-Runtime und Versionsabgleich.
- https://playwright.dev/docs/docker

[R6] SonarQube Community Build: Quality Gates, New Code, Small-Changes-Verhalten,
Hotspot-Umstellung und konfigurierbare Metriken.
- https://docs.sonarsource.com/sonarqube-community-build/quality-standards-administration/managing-quality-gates/introduction-to-quality-gates

[R7] SonarScanner for Maven: Plugin-Versionen und Reactor-/Install-Hinweise.
- https://docs.sonarsource.com/sonarqube-community-build/analyzing-source-code/scanners/sonarscanner-for-maven

[R8] ZAP: Baseline-Scan, Konfiguration, Exit-Codes und Grenzen des passiven Scans.
- https://www.zaproxy.org/docs/docker/baseline-scan/

[R9] OpenAI: Arbeitsanweisungen mit AGENTS.md.
- https://developers.openai.com/codex/guides/agents-md

[R10] Trivy: Filesystem-Scans, Scanner-Auswahl und Fehlkonfigurationen.
- https://trivy.dev/latest/docs/target/filesystem/

[R11] CycloneDX Maven Plugin; JaCoCo Maven und Java-Unterstützung.
- https://cyclonedx.github.io/cyclonedx-maven-plugin/
- https://www.jacoco.org/jacoco/trunk/doc/maven.html
- https://www.jacoco.org/jacoco/trunk/doc/changes.html

[R12] Semgrep: CLI-Verhalten, lokale Konfigurationen, `--strict`, `--oss-only`,
Metrics und Ignore-Verhalten.
- https://semgrep.dev/docs/cli-reference
- https://semgrep.dev/docs/metrics

[R13] OWASP Top 10:2025.
- https://top10.owasp.org/2025/

[R14] Docker: Container-Run-Flags, Bind-Mounts und Host-Gateway.
- https://docs.docker.com/reference/cli/docker/container/run/

[R15] Testcontainers: Container-in-Container-Ausführung und Docker-Socket-Muster.
- https://java.testcontainers.org/supported_docker_environment/continuous_integration/dind_patterns/
