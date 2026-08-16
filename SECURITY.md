# Security policy

## Reader boundary

The reader is not protected by an ordinary Docker bridge. Compose gives it only
two `internal` networks, routes all three Scrapling tiers through Squid, and uses
fixed addresses so `ops/reader-firewall.sh` can reject the Docker host gateway.
Run the firewall script before starting the stack and verify it after every host
firewall or Docker upgrade. `ops/verify-stack.sh` proves public fetching, direct
socket denial, proxy-side private DNS denial, and static/browser redirect denial.

`READER_ALLOW_DIRECT=1` is only for unit tests or an operator-supplied equivalent
network sandbox. Production fails closed when `READER_OUTBOUND_PROXY` is absent.

## Vulnerability scans

CI runs `pip-audit` over both pinned requirement sets and scans all three images
with Trivy. A fixable HIGH or CRITICAL finding fails CI. Unfixed findings are
reported in the job output but do not fail a build, because Chromium/Patchright
images commonly contain browser and OS findings whose fixes have not reached the
base distribution. Review those reports on every scheduled run; do not suppress
a finding by identifier without a repository issue that records the exposure,
workaround, and removal date.

Please report vulnerabilities privately to the repository owner rather than in
a public issue.
