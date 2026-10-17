# snow-email

Daily email of Utah Snow Ensemble snowfall percentiles (24 h, 5 day, 10 day), run by GitHub Actions from Nov 1 to May 1.

## Setup

1. Create a repo from this one (make it **private**, see note below).
2. Gmail: turn on 2-step verification, then create an app password.
3. Repo → Settings → Secrets and variables → Actions → add secrets:
   - `SMTP_USER` — sending Gmail address
   - `SMTP_PASS` — the app password
   - `EMAIL_TO` — where to send (comma-separate multiple)
4. Actions tab → "Snow email" → Run workflow, to test.

Non-Gmail: add repo variables `SMTP_HOST` and `SMTP_PORT` (SSL).

## Locations

Edit `config.yaml`. Each location needs a URL to a Utah Snow Ensemble text table in the same format as the Alta-Collins page (H2 "... Guidance ..." titles, Min/P10–P90/Max rows, 6-h columns). Windows and percentiles are also set there.

## Behavior

- Values are cumulative snowfall from model init, read from the "Total Snowfall" table.
- A run older than `max_age_hours` gets a warning line.
- If a fetch fails, it waits `retry_delay_min` (60) and tries again; if that fails too, the email says the data was not available and the Actions run is marked failed.
- Schedule is in `.github/workflows/snow.yml` (cron is UTC; 13:50 UTC = 6:50 MST).

## Notes

- GitHub disables scheduled workflows in **public** repos after 60 days with no commits, which would happen every summer. Private repos aren't affected; a daily run uses ~1 min of the 2000 free minutes/month (~61 min on retry days).
- GitHub cron can start late by several minutes or more at busy times.

## Local test

```
pip install -r requirements.txt
DRY_RUN=1 python snow_email.py
```
