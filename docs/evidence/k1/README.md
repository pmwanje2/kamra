# K1 evidence

Pinned base: 985606b3497b784e556b1d7522c28098123a77d7 (v2.6.12).
RED: 23e4fec924cf8abaa1fd5f34a41e0db4bab8b98c, tests only.
GREEN: cc0724ccc092a3981af8682caff670801f43ff37, three production lines only.

Command: `bench --site kamra.localhost run-tests --module kamra.tests.test_channel_manager_availability`.
RED: 6 tests, 2 failures, `1 != 0` for unassigned Confirmed stays in both legacy and SIU snapshots.
GREEN: 6/6 pass, including room blocks, assigned stays, Requested and adjacent dates.
Full harness: `bench --site kamra.localhost execute kamra.scripts.eval_harness.execute`;
baseline 89/89, candidate 89/89 (strict scope setting disabled, same as baseline).

Environment: disposable Compose project kamra-fixes-nrk9ze, loopback-only 127.0.0.1:18712;
Frappe 16.52.0, payments version-16 at cca07d9f9392e2ea0e521c5975151db9e4b6c321,
Kamra 2.6.12, Python 3.14.8, MariaDB 11.8. ERPNext is not installed on the site.

The screenshot shows live bench data: one physical room and an unassigned Confirmed
reservation; `build_push_bodies` publishes available=0. The computed preview was stored
as a native File, fetched from the running bench, and rendered in a labelled browser
evidence view. No outbound OTA call. Fresh Chrome profile removed after capture.
Reproduction script: proof.py (run through bench console with language en).

CANNOT-VERIFY: independent cross-family specialist unavailable; CodeRabbit failed
HTTP 403 Invalid organization. Hosted CI and upstream review are not claimed. Native
Desk rendering unqualified because the disposable image lacks built website assets/Node.

