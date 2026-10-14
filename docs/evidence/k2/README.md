# K2 evidence

Pinned base: v2.6.12 / 985606b3497b784e556b1d7522c28098123a77d7.
RED: e411e39e8f245e5bd7798260bdefe9391cab0233, tests only.
GREEN: 32c1c8c4a9f15003066d094dd204b1966f9ba40e, production-only.

Focused command: `bench --site kamra.localhost run-tests --module kamra.tests.test_reservation_capacity`.
RED: 3 tests, locking assertion failed (no Room Type FOR UPDATE query); the initial
fixture also caught a test-only query-spy issue, which was corrected before final RED.
GREEN: 3/3 pass. Requested status remains non-inventory and does not lock/count.
Full eval baseline 89/89 -> candidate 89/89.

Screenshot is a live authenticated browser rendering of a native File fetched from
the disposable bench. It records real SQL call order: Room Type SELECT ... FOR UPDATE
at index 0, then physical-room COUNT and reservation COUNT. Fresh Chrome profile
removed. No live system touched.

CANNOT-VERIFY: independent cross-family specialist unavailable; CodeRabbit review
failed HTTP 403 Invalid organization; hosted CI/upstream review not claimed. Native
Desk assets are not qualified in this minimal disposable image.

