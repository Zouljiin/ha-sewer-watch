# Sewer Watch

Watches the Spencer County, KY Fiscal Court website and tells you when anything
about the sewer project shows up.

## What it checks (every 3 hours by default)

- **Fiscal Court agenda page.** It reads the agenda text, pulls out the
  sewer-related items, and sends them in the alert.
- **Meeting Minutes page.** It downloads every new agenda/minutes PDF, plus the
  attachments on each meeting's "More..." page.
- **Public Notices page**, plus any `extra_pages` you add.

It downloads each PDF and reads the text, using OCR when the county posts a
scan. It then searches for your keywords and sends a push with the matching
passages and a picture of the page with the mention highlighted. The push has
buttons for **Open PDF**, **Watch meeting**, and **Sewer Watch reader**.

In the reader, every document is shown as its real pages, with keywords
highlighted in yellow.

## Install

1. Go to **Settings → Apps → App store** → ⋮ → **Repositories**. Add
   `https://github.com/Zouljiin/ha-sewer-watch`.
2. Install **Sewer Watch**. Start it and turn on **Show in sidebar**.
3. Open **Sewer Watch** in the sidebar. Under **Send alerts to**, tick your phone
   (or several), then click **Save & send test**.

Phones appear in that list once the Home Assistant Companion app is installed
and logged in on them. Until you pick one, alerts only show up in Home
Assistant's own notifications.

### First run

The first run reads the last `backfill_months` (default 12) of documents
quietly. When it finishes, it sends one "Sewer Watch is running" push listing
the recent mentions. After that, you only get alerts for new or re-uploaded
documents.

Older history is listed in the reader too. Opening an older document reads it
on demand.

## Settings (Configuration tab)

| Setting | Meaning |
|---|---|
| How often to check the county site | Minutes between checks (180 = every 3 hours) |
| How many months of past documents to read | How far back the first start reads |
| Read scanned PDFs (OCR) | Needed for scanned minutes. Slower on a Raspberry Pi |
| Alert on every new county document | Off = only alert when a keyword is mentioned |
| Remind me on the morning of a meeting | Sends the livestream link and the sewer items on the agenda |
| Words to watch for | Not case-sensitive |
| Extra web pages to watch | Other pages to check for new PDFs |

Where alerts go is set in the **Sewer Watch panel**, not here, so you can pick
from a list of your phones.

## In Home Assistant

- `sensor.sewer_watch` has the last check time, the current agenda date and
  keywords, and the latest document and latest mention, with URLs.
- Every alert also fires the event `sewer_watch_alert` (with `title`,
  `message`, `links`, and `important`). You can build your own automations on
  it, such as flashing a light or sending a TTS announcement.
- Alerts also land in **Notifications** (full text) and the **Logbook**.

Alert pictures are saved in `www/sewer_watch/` in your HA config folder, which
keeps the last 40. They're served at `/local/sewer_watch/` so your phone can
show them.

## Limits

- The county site has no feed, so this reads the pages. If the county
  redesigns the site, you'll get a "can't read the county site" push after 48
  hours rather than silence.
- State (KDEP) permits aren't scraped. Use the free email list on the
  [Water Public Notices page](https://eec.ky.gov/Environmental-Protection/Water/Pages/Water-Public-Notices-and-Hearings.aspx).
