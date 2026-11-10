# cruise-watch

Polls cruise-deal news feeds for Tahiti, Alaska, Mediterranean, transatlantic, Panama Canal,
Antarctica, Japan and China. Pushes a phone alert when a post advertises a fare under your
per-person-per-night limit, with a rough total for 4 adults + 1 child. No dependencies (Python 3.11+).

## Setup (5 minutes)
1. Install the free **ntfy** app (iOS/Android), subscribe to a long random topic, e.g. `cruise-snandra-7f3k9q2x`.
2. Put that topic in `config.toml` (`ntfy_topic`) or set env var `NTFY_TOPIC`.
3. `python cruise_watch.py --test-notify` should buzz your phone.
4. `python cruise_watch.py --dry-run` shows what it would alert on without sending.

## Run it continuously
- **Free, no computer left on:** push this folder to a private GitHub repo, add repo secret `NTFY_TOPIC`.
  `.github/workflows/watch.yml` runs it every 3 hours.
- **Or cron on any always-on machine:** `17 */3 * * * cd /path/cruise-watch && python3 cruise_watch.py`

## Tuning
Edit `config.toml`: `max_pp_night` per destination, `child_price_factor`, more `queries`, extra RSS feeds.
Run tests with `python -m unittest discover -s tests`.

## Sources
- **CruiseSheet** (main source): real sailings with the per-person fare and per-night price, cheapest first, one request per destination
  (limits in `[[cruisesheet]]` in `config.toml`). Alerts on a new sailing under your limit, and again if a known sailing's price drops.
  At most `max_alerts_per_run` (default 5) alerts per run, best first; other matches are recorded silently.
- **News feeds** (Google News, Cruise Hive, Cruise Fever): headlines only, so rarely priced. Kept as a backup.

Check the run log's `funnel:` line. `sheet_cards=0` means CruiseSheet could not be read from GitHub's servers
(for example, blocked as a bot); `sheet_new` is how many new deals were found.

## Limits to know about
- It reads deal *headlines and summaries* (Google News RSS plus blogs), not live cruise-line inventory.
  Cruise lines publish no public price API and their sites block scrapers. Treat an alert as a lead,
  then confirm the sailing, cabin type and 3rd/4th-guest rate on the cruise line or a Vacations To Go / agent quote.
- Headline "from $X" fares are usually per person, cheapest inside cabin, double occupancy, before taxes/fees (often $150-400 pp more)
  and gratuities. The family total is an estimate only.
- Fetching was not tested against live sites in the build environment (network blocked). The extra blog feed URLs
  are unverified, so open them in a browser; failed feeds just log a warning.

## Phone app (installable web app)
Each run writes every current deal to `site/deals.json` and publishes the `site/` folder with GitHub Pages
(needs Pages enabled with Source = GitHub Actions; on a free GitHub plan the repo must be Public).
Open the Pages address in Chrome on Android, then menu > **Add to Home screen** / **Install app**.
The app lists all current deals, filterable by destination. Alerts still come through ntfy.
