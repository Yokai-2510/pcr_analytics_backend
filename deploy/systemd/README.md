# Daily-prep systemd units

Two units live in this folder. Together they:

1. **08:45 IST every Mon–Fri** — log into Upstox via Playwright, mint a fresh
   `access_token`, and persist it to `source/credentials.json`.
2. Immediately after — restart `index-pcr-worker.service` so the worker boots
   with the new token, runs its session prep, and is ready for the 09:15
   market open.

## Files

- `index-pcr-daily-prep.service` — oneshot service. ExecStart runs the
  Python refresh; ExecStartPost (with `+` prefix to elevate to root)
  restarts the worker.
- `index-pcr-daily-prep.timer` — calendar timer that fires the service on
  weekdays at 03:15 UTC (08:45 IST). `Persistent=true` so a missed run
  catches up on next boot.

## Install on a fresh EC2

```bash
sudo cp index-pcr-daily-prep.service /etc/systemd/system/
sudo cp index-pcr-daily-prep.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now index-pcr-daily-prep.timer
# Verify the next-fire time:
systemctl list-timers index-pcr-daily-prep.timer
```

## One-off manual trigger (handy for testing)

```bash
sudo systemctl start index-pcr-daily-prep.service
sudo journalctl -u index-pcr-daily-prep.service -n 30 --no-pager
```

The Playwright login takes ~7–10 seconds end-to-end. The worker restart
then takes ~3 seconds. Total ~15 seconds from timer fire to a fully
prepared worker.

## Dependencies

- `playwright` and `pyotp` Python packages must be installed in the
  venv at `/home/ubuntu/index_pcr/.venv`.
- `google-chrome-stable` (or `chromium`) must be installed system-wide
  for Playwright to launch a headless browser. The `upstox_auth.py`
  module points at `/usr/bin/google-chrome` by default.
- `credentials.json` must already contain `api_key`, `api_secret`,
  `redirect_uri`, `mobile_no`, `pin`, and `totp_key`. The Playwright
  flow uses these to drive the Upstox login dialog.
