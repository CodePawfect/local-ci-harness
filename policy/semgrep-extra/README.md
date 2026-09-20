# Weitere geprüfte Semgrep-Regeln

Hier können zusätzliche lokal exportierte Semgrep-Regeln als `*.yml` oder `*.yaml`
versioniert werden. Der Runner lädt alle diese Dateien zusätzlich. Er lädt während
normaler Scans KEINE veränderlichen Registry-Regeln nach. Alle Policy-Dateien gehen
in den SHA-256 des Laufes ein.

Die neun eigenen Regeln in `../semgrep.yml` sind nur eine kleine Ergänzung zu
Sonars Java-/JS-/TS-Profilen, KEIN vollständiges OWASP-Top-10-Regelpaket. Ein
geprüftes OWASP-/Java-/React-Regelpaket kann hier ergänzt werden. Quelle, Revision,
Lizenz und Anpassungen dokumentieren und positive/negative Regeltests ergänzen.
Bei Updates zuerst Fehlalarme und neue Blocker prüfen. Proprietäre/Pro-Regeln
benötigen unter Umständen weitere Rechte oder eine andere Engine; der Runner
verwendet ausdrücklich `--oss-only`.

ERROR blockiert, WARNING erscheint als WARN mit Fundstellen im JSON-Bericht.
Syntax- und Parserfehler sowie null analysierte Dateien sind Fehler, nicht grün.
