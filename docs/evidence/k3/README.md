# K3 evidence

Pinned base: v2.6.12 / 985606b3497b784e556b1d7522c28098123a77d7.
RED: 7933afdad4ce56cacfef3dab0aa0899dd0f8d0a5, tests only.
GREEN: 025c2d135f710161b452650c358023a89a0ee40f, production-only.

Focused command: bench --site kamra.localhost run-tests --module kamra.tests.test_channel_manager_bookings.
Final RED: 9 tests, 7 genuine failures: sold-out AioSell events disappeared, foreign
currency was Confirmed, missing currency defaulted to INR, and rejected events were
not retained. GREEN: all 9 pass. Full eval baseline 89/89 -> candidate 89/89.

Capacity refusal rolls the worker back, then stores all event lines as non-inventory
Requested with OTA overbooking — resolve. Foreign currency is not posted to the
ledger: Requested carries the amount/currency/property-currency mismatch in its note.
Missing currency comes from Property.currency; no INR default remains. Matching
currency keeps Confirmed and OTA price. Replay remains idempotent by ota_ref.

Screenshot: live authenticated browser rendering of a native File from the disposable
bench. It shows an actual Requested Reservation with OTA ref, source OTA, original
special request and resolution note. Fresh Chrome profile removed. Project:
kamra-fixes-nrk9ze, loopback-only 127.0.0.1:18712; Frappe 16.52.0, payments version-16,
Kamra 2.6.12, Python 3.14.8, MariaDB 11.8. ERPNext not installed on the site.

CANNOT-VERIFY: independent cross-family specialist unavailable; CodeRabbit failed
HTTP 403 Invalid organization; hosted CI/upstream review not claimed. Native Desk
assets not qualified in the minimal disposable image. No SiteOps/live-system changes,
no skips or gate weakening.

