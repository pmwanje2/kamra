# K3 evidence

Pinned base: v2.6.12 / 985606b3497b784e556b1d7522c28098123a77d7.
Rebased on maximus/integration-20261015 at 5ca279e (K1, K2, C3 merged).
RED: 7933afdad4ce56cacfef3dab0aa0899dd0f8d0a5, tests only.
GREEN: 025c2d135f710161b452650c358023a89a0ee40f, production-only.
(Pre-rebase SHAs; after rebase: RED 53aa695, GREEN 52d6266.)

Review round (Grok, changes requested):
RED 04b0a766104febb180f401de08ca15a52bbbafbe, tests only: 12 tests, 3 genuine
failures. An OTA cancel left a retained Requested stay Requested; a modify that
dropped a Requested line left it Requested; a physical-room overlap refusal
(title "Double booking blocked", lower case) was logged and the booking dropped.
GREEN ccf05454969dec8f27a94a954a29d54bc9f6fe4f, production only: 12/12 pass.
Inventory refusals now raise an explicit InventoryRefused (a ValidationError
subclass) from the sold-out, room-overlap and both villa checks; the worker
retains on that exception class instead of reading message titles. OTA cancel
and modify reconciliation release Requested lines without a fee or credit note
(they hold no room and took no money). Eval harness on the rebased candidate:
89/89. Neighbouring suites (aiosell, reservation capacity, OTA availability) pass.
See red-review.txt, green-review.txt, eval-after.txt, proof-output.txt.

Focused command: bench --site kamra.localhost run-tests --module kamra.tests.test_channel_manager_bookings.
Final RED: 9 tests, 7 genuine failures: sold-out AioSell events disappeared, foreign
currency was Confirmed, missing currency defaulted to INR, and rejected events were
not retained. GREEN: all 9 pass. Full eval baseline 89/89 -> candidate 89/89.

Capacity refusal rolls the worker back, then stores all event lines as non-inventory
Requested with OTA overbooking — resolve. Foreign currency is not posted to the
ledger: Requested carries the amount/currency/property-currency mismatch in its note.
Missing currency comes from Property.currency; no INR default remains. Matching
currency keeps Confirmed and OTA price. Replay remains idempotent by ota_ref.

Screenshot (refreshed): live authenticated browser rendering of a native File from
the disposable bench. proof.py drives the real worker with real commit/rollback: a
sold-out AioSell book event is kept as Requested with the resolution note and
sellable stays 0, then an AioSell cancel for the same booking turns it Cancelled
("Booked elsewhere", note naming the OTA booking). Fresh Chrome profile removed. Project:
kamra-fixes-nrk9ze, loopback-only 127.0.0.1:18712; Frappe 16.52.0, payments version-16,
Kamra 2.6.12, Python 3.14.8, MariaDB 11.8. ERPNext not installed on the site.

CANNOT-VERIFY: independent cross-family specialist unavailable; CodeRabbit failed
HTTP 403 Invalid organization; hosted CI/upstream review not claimed. Native Desk
assets not qualified in the minimal disposable image. No SiteOps/live-system changes,
no skips or gate weakening.

