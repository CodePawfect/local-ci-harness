# Fachlicher Sicherheitsvertrag

Diese Fälle sind eine **zu implementierende und zu reviewende Testliste**, keine im
ZIP bereits bewiesene Sicherheitsabdeckung. OWASP Top 10:2025 dient als Orientierung,
nicht als binäre Scanner-Checkliste. Kategorien und Quellen siehe `../research.md`.
Tests mit konkreten Endpunkten, Rollen und Daten deiner Anwendung ergänzen.

## Autorisierung und Mandantentrennung

Zwei echte Testidentitäten, zwei Mandanten, mindestens ein fremdes Objekt. Als Nutzer A
alle Lesen-/Schreiben-/Löschen-/Export-Endpunkte mit IDs von B aufrufen. Erwartetes
403 oder bewusstes 404 anhand der API-Policy prüfen; entscheidend: keine fremden Daten,
keine Mutation und kein indirektes Metadatenleck. Listenfilter und Pagination dürfen
B nicht enthalten. Serverseitig manipulierte tenantId-, userId- oder owner-Felder dürfen
keine Rechte verleihen. Zugehörigkeit auch für verschachtelte Objekte prüfen.

Fehlende Anmeldung muss geschützte Daten verweigern. Ein aus der Oberfläche entfernter
Button ist kein Berechtigungsschutz. Rollenwechsel und abgelaufene/widerrufene Tokens
sowie direkter HTTP-Aufruf einer Server Action/Route müssen geprüft werden. Schutz in
Next.js ersetzt nicht die Autorisierung im Spring-Backend und umgekehrt.

## Zustand, Transaktionen und Geschäftsregeln

Jede kritische Mutation: Erfolg, fehlende Rechte, ungültiger Zustand, wiederholter
Aufruf und Parallelität. Beispiel Zahlung/Webhook: Signatur, falsche Signatur,
Replay/Idempotenz, keine Doppelbuchung. Beispiel Kontowechsel: keine Rechteausweitung
oder fremder Datenzugriff. Bei Fehlschlag keine halben DB-Änderungen. Diese Anforderungen
kann ein generischer Scanner nicht aus dem Code zuverlässig ableiten.

## Eingaben, Kryptografie, Session und Ausgabe

SQL-/JPQL-Zugriffe parametrisieren; dynamische Identifier nur mit erlaubtem Wertebereich.
HTML-Ausgabe mit realistisch bösartigen Eingaben testen, insbesondere Notizen, Rich Text,
Dateinamen und `dangerouslySetInnerHTML`. Uploads: Größe, Typ, Dateiname, Pfadtraversal,
Zugriffsrechte beim späteren Abruf. Keine ausführbaren Uploads im Webroot.

Serverseitige URL-Abrufe: erlaubte Ziele, Redirects und private/Metadata-Netze prüfen.
Nicht beliebige externe Systeme scannen; kontrollierte lokale Fixtures verwenden.

Passwörter nicht im Klartext, keine abgeschaltete Zertifikatsprüfung. Session-Cookies
und Auth-Architektur prüfen. Bei Cookie-basierter Authentifizierung CSRF explizit
berücksichtigen; „JWT“ allein ist keine Begründung für das Abschalten von CSRF.
CORS nach tatsächlich zugelassenen Origins prüfen, nicht als Autorisierung behandeln.
Secure-Cookie-/HTTPS-Tests benötigen eine zur Produktion passende HTTPS-Testumgebung.

`NEXT_PUBLIC_*` darf keine Geheimnisse enthalten. Auch Server-Component-Props,
serialisierte Antworten, Browserbundles, Fehlermeldungen und Build-Ausgaben auf
unbeabsichtigte Informationen prüfen. Keine realen Secrets als Test-Fiktionen verwenden.

## Logging, Fehler und Ressourcen

Abgelehnte sensible Aktionen müssen nach der Produktpolicy auditierbar sein, ohne
Passwörter, Tokens oder unnötige personenbezogene Daten zu protokollieren. Fehlerantworten
sollen keine Stacktraces, DB-Adressen oder internen Schlüssel offenlegen.

Unverfügbare Datenbank, Timeout externer Dienste, ungültige Payloads, große Requests und
fehlgeschlagene Serialisierung testen. Erwartet wird kontrolliertes, fail-closed
Verhalten statt unerlaubtem Fallback. Rate Limits und Ressourcenbegrenzungen separat
unter Last prüfen. Eine Code-Coverage-Zahl sagt nicht, dass diese Fehlerfälle getestet sind.

## Abnahme pro Feature

Akzeptanzkriterium → Testname → ausgeführter Lauf → konkrete Assertion dokumentieren.
Der Testsatz darf nicht automatisch abgeschwächt werden, nur weil Codex die Implementierung
anders gebaut hat. Sicherheitsfälle als relativ stabile, separat reviewte Regressionstests
pflegen. Browser-Snapshots, Mutationstests und unabhängiges Review sind Ergänzungen,
keine pauschalen Garantien.
