# C3 evidence

Pinned base: `v2.6.12` / `985606b3497b784e556b1d7522c28098123a77d7`.
RED: `41eb1b532cc9917723cc65c251e01a638a8d75e1` (tests only).
GREEN: `65bfc3832db242d38826923c5e2e61770850e994` (ledger.py only).

Focused command:
`bench --site kamra.localhost run-tests --module kamra.tests.test_supervisor_reason`.
RED: 5 tests, 3 genuine behavior errors: Finance and Hotel Admin were asked
for a PIN despite the property's PIN setting being off, and Front Desk received
the PIN error instead of the role refusal. GREEN: all 5 pass, including PIN-on
rejection of absent/incorrect PIN and acceptance of a valid cashier PIN.

Full harness command:
`bench --site kamra.localhost execute kamra.scripts.eval_harness.execute`.
Baseline 89/89 -> candidate 89/89. See the unabridged output files here.

Additional full-app run on candidate source GREEN:
`bench --site kamra.localhost run-tests --app kamra`.
119 integration tests: 116 passed, 1 failure and 2 errors; 36
unspecified-category tests: all passed. No skips were reported. The two
banquet email errors come from the missing Frappe asset manifest
(`AttributeError: 'NoneType' object has no attribute 'get'`), and the MCP
failure expects a different protected-resource metadata URL. Those modules
and their tests are unchanged from the base, but a full-app baseline run was
not performed, so this is NOT a proven regression-free full-app result.
See `app-tests.txt`; no fixes or skip escapes were added for these findings.

The supervisor role gate remains unconditional. Only the PIN gate follows
`Property.require_cashier_pin`. The existing no-property behavior remains
unchanged: a supervisor PIN is required.

Disposable project: `kamra-fixes-nrk9ze`, HTTP bound only to `127.0.0.1:18712`.
Installed apps: Frappe 16.52.0, payments version-16 at
`cca07d9f9392e2ea0e521c5975151db9e4b6c321`, Kamra 2.6.12 from this branch.
Python 3.14.8, MariaDB 11.8; ERPNext is not installed on the site.

`proof.py` invokes the real `ledger.require_reason` with three ordinary-role
users and a supervisor-only reason. Finance and Hotel Admin can use the code
without a PIN when the property setting is off; Front Desk gets PermissionError.
The resulting JSON is persisted as a native File and fetched from the running
bench by a fresh-profile headless Chrome browser. The screenshot is explicitly
a labelled rendering of live REST/File JSON, not native Desk acceptance.
Browser profiles are removed immediately after capture.

Cleanup completed: `docker compose -p kamra-fixes-nrk9ze -f
/tmp/kamra-3712-compose.yaml down -v` removed this project's three containers,
two named volumes and network. The disposable demo data is discarded.
No project-labelled containers, volumes or Chrome profiles remain.

Reproduce the proof in the disposable Compose bench:

```sh
docker compose -p kamra-fixes-nrk9ze -f /tmp/kamra-3712-compose.yaml exec -T bench \
  ./env/bin/python -c 'import os, sys, frappe; os.chdir("/home/frappe/frappe-bench/sites"); frappe.init(site="kamra.localhost"); frappe.connect(); exec(compile(sys.stdin.read(), "c3_live_proof", "exec")); frappe.destroy()' \
  < docs/evidence/c3/proof.py
```

CANNOT-VERIFY: independent cross-family specialist not callable in this session;
CodeRabbit CLI 0.7.6 refused the C3 review with HTTP 403 `Invalid organization`.
Hosted CI and upstream review are not claimed. Native Desk assets are not
qualified in the minimal disposable image. The full-app suite is not green;
its base comparison was not run. No SiteOps/live-system changes,
CI weakening, skipped test escapes or upstream PRs.
