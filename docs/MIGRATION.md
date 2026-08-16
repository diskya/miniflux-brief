# Legacy-to-Compose migration runbook

This is a cutover procedure, not an installation script. Implementation and CI
must never run it against `/var/lib/feedsum` or the legacy services. Keep the
legacy units, configuration, and verified backup until the new deployment has
been accepted.

## 1. Preflight without changing anything

Install Docker Engine from Docker's supported repository, including the Compose
plugin package (`docker-compose-plugin`). Verify it with `docker compose version`.
Do not substitute the retired Python `docker-compose` v1 command.

Prepare `.env` from `.env.example`, set mode `0600`, and generate the webhook and
UI tokens independently (`openssl rand -hex 32` twice). Do not reuse or expose
`MINIFLUX_API_TOKEN` in browser JavaScript. Validate the plan without printing
secrets:

```bash
sudo ops/reader-firewall.sh --dry-run
python3 ops/migrate.py \
  --source-db /var/lib/feedsum/feedsum.db \
  --env-file .env \
  --backup /srv/feedsum-backups/feedsum-before-compose.db \
  --legacy-writer-service feedsum.service
```

Check that the fixed `172.30.40.0/27` space in `docker-compose.yml` does not
overlap a host, VPN, or Docker network. If it does, change the two Compose
subnets, fixed service addresses, and `ops/reader-firewall.sh` together, then run
`ops/verify-stack.sh` before cutover.

Choose the webhook URL based on where Miniflux runs:

- Native Miniflux on the same host: `http://127.0.0.1:8012/webhook`.
- Miniflux in another Compose project via the published host port: use the
  Docker host gateway address reachable from that project, port 8012.
- Miniflux in another Compose project on a shared external network: attach only
  `feedsum` to that network and use `http://feedsum:8012/webhook` (or a unique
  network alias). Never attach `reader` to the Miniflux network.

Native Miniflux needs `INTEGRATION_ALLOW_PRIVATE_NETWORKS=1` for a loopback or
private webhook URL. This setting applies to integrations; review it separately
from feed-fetching policy.

## 2. Approved backup and volume seed

At the maintenance window, explicitly approve stopping only the legacy feedsum
writer. Do not stop Miniflux, the reader, or unrelated units. The utility uses
SQLite's backup API after the writer stops, so committed WAL pages are included;
never copy only `feedsum.db` while it is live.

```bash
sudo ops/reader-firewall.sh --apply
sudo ops/reader-firewall.sh --check
sudo python3 ops/migrate.py \
  --source-db /var/lib/feedsum/feedsum.db \
  --env-file "$PWD/.env" \
  --backup /srv/feedsum-backups/feedsum-before-compose.db \
  --volume miniflux-brief_feedsum-data \
  --legacy-writer-service feedsum.service \
  --execute --confirm-stop STOP_LEGACY_FEEDSUM
```

The utility refuses to overwrite a backup or non-empty volume. It runs SQLite
integrity checks, compares summary/payload/original counts, and installs the
seeded database as UID/GID 1101 with mode `0600`. Record those counts in the
change log.

## 3. Start and verify

Start only after reviewing the verified backup and volume:

```bash
docker compose up -d --wait
docker compose ps
curl --fail http://127.0.0.1:8012/healthz
sudo ops/reader-firewall.sh --check
```

Run every canary before acceptance:

1. A normal article reaches the static tier and produces the expected shape.
2. A JavaScript article reaches Chromium; a challenged page reaches Patchright.
3. A YouTube entry uses Gemini, or clearly reports the intentionally missing key.
4. Clicking **Brief** works with the UI token; anonymous `/v1` calls return 401.
5. A signed `new_entries` webhook queues an automatic job.
6. Saving an entry triggers `save_entry`, bypasses the automatic feed allowlist,
   and forces a new generation.
7. Write-back contains exactly one brief above the saved original.
8. A simulated transient Miniflux failure retries write-back without another LLM
   request; a permanent failure leaves the brief visible with a warning.
9. Restart during pending summary and pending write-back work; both resume with
   their stored attempts and generation.
10. Run `ops/verify-stack.sh` on a staging host to repeat the private egress and
    DNS-rebinding canaries.

Update Miniflux's webhook only when the new receiver is healthy. Apply the UI
assets with `FEEDSUM_UI_TOKEN` in the environment or use the manual browser-session
paste flow described in the README.

## 4. Rollback

If any canary fails, point the webhook/reverse proxy back to the legacy receiver,
run `docker compose stop feedsum` to stop the new writer, and start only the
legacy feedsum writer. Do not delete the new named volume or the verified backup;
they are evidence for diagnosis and a later retry. The legacy database and units
remain the rollback source until the operator explicitly accepts the deployment.
