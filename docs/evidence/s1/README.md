# S1 evidence

Base: `v2.6.12` / `985606b3497b784e556b1d7522c28098123a77d7`.
RED: `0eebfb36ae4150fef17121b6b0465bda24457b4c` (tests only).
GREEN: `7448febbb6ade8d1438e2674341f041ad40944a7` (authz.py only).
(Pre-rebase SHAs. Rebased on maximus/integration-20261015 at 5ca279e, K1/K2/C3
merged: RED a5d4f6d, GREEN cf15288.)

## Review round (Grok, changes requested)

RED `6b39e6f499b363f19ddef3270d960b58046ef9e5` (tests only): 19 tests, 5 genuine
failures. Under strict=1 an unscoped Front Desk user still listed both hotels'
Reservations through `frappe.get_list`; Folio, Room and Housekeeping Task rows
of the other hotel were listed; Property-linked DocTypes had no permission hooks;
a Kamra Agent with no Property permission stayed chain-wide; site config "0" was
read as on.

GREEN `7917c462ac65c66b7fc536293b6d3b6cde86e708` (authz.py + hooks.py only): 19/19.
`property_query_conditions` / `property_has_permission` are registered for all
62 Kamra DocTypes with a Property link (a test fails when a new one is added
without them). They add nothing unless `kamra_strict_property_scope` is on;
under strict they apply `restricted_properties()`: nothing for users without
Property permissions, only their properties otherwise. Only Administrator and
System Manager stay chain-wide (Kamra Agent no longer). The flag is read with
`cint`.

Eval harness on the rebased candidate: strict off 89/89 (unchanged default).
Strict on 85/89: the four eval personas without Property permissions
(frontdesk, hk and now the governed agent user) are refused "You don't have
access to EVAL Hotel". These are fixture gaps in strict mode, not skipped.

Live REST on the disposable bench (`lab.py`, `live-rest.txt`), Front Desk
user with no Property permission, P1 and P2 each holding one stay:
strict off: `/api/resource/Reservation` lists both; strict on: `{"data": []}`
(screenshot `strict-scope-list-empty.png`); a Kamra-Agent-only user also gets
`[]`; reading the P2 record returns 403; `front_desk_snapshot(P2)` returns 403
(screenshot `strict-scope-refused.png`, refreshed). Frappe caches site config
per server process, so the server was restarted after each flag change.

Disposable Docker project: `kamra-fixes-nrk9ze`; HTTP bound only to
`127.0.0.1:18712`. Installed apps: Frappe 16.52.0, payments 0.0.1
(version-16 commit cca07d9f9392e2ea0e521c5975151db9e4b6c321), Kamra 2.6.12.
Python 3.14.8, MariaDB 11.8. ERPNext is not installed on the site.

Command: `bench --site kamra.localhost run-tests --module kamra.tests.test_property_scope`.
RED: 12 tests, 2 genuine failures, PermissionError not raised.
GREEN: 12 tests, all pass. Existing deprecated limit_page_length warning unchanged.

Full harness command: `bench --site kamra.localhost execute kamra.scripts.eval_harness.execute`.
Baseline and candidate with the default/disabled strict setting: 89/89 passed.
Also exercised the full harness with strict=1: 87/89; two existing harness personas
have no Property User Permission and are correctly refused. No skip escapes added.
Opt-in fixtures in test_property_scope cover strict=1 separately.

Screenshots: [strict-scope-list-empty.png](strict-scope-list-empty.png),
[strict-scope-refused.png](strict-scope-refused.png).
Fresh headless Google Chrome profile, deleted after capture; the browser renders
an actual authenticated HTTP response from the running bench, omitting only the
long `exc` stack from the displayed JSON. It is a labelled evidence view, not a
claim of Desk UI acceptance. Front Desk user unscoped@example.test has zero
Property User Permissions; strict=1; front_desk_snapshot(property=P2) returns
HTTP 403 / PermissionError. Property P2 exists in the dev database.

CANNOT-VERIFY: independent cross-family verification agent unavailable in this
session. CodeRabbit CLI 0.7.6 review attempted but refused with HTTP 403,
"Invalid organization". Hosted checks and upstream review are separate evidence.
The disposable image has no built website assets/Node, so native Desk rendering
was not qualified; REST behavior and browser-rendered response were qualified.

