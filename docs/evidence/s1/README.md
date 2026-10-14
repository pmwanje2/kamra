# S1 evidence

Base: `v2.6.12` / `985606b3497b784e556b1d7522c28098123a77d7`.
RED: `0eebfb36ae4150fef17121b6b0465bda24457b4c` (tests only).
GREEN: `7448febbb6ade8d1438e2674341f041ad40944a7` (authz.py only).

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

Screenshot: [strict-scope-refused.png](strict-scope-refused.png).
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

