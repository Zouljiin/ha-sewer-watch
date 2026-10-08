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
passages. The push has buttons for **Open PDF**, **Watch meeting**, and
**Sewer Watch reader**.

## Install (Home Assistant OS / Supervised, 2026.2+ where add-ons are "Apps")

1. **Settings → Apps → App store** (bottom right): install the **Samba share** app,
   set a username/password on its Configuration tab, and start it.
2. On your computer open `\\homeassistant.local` (Windows) or `smb://homeassistant.local`
   (Mac). Copy this `sewer_watch` folder into the **`local_apps`** share so the path is
   `local_apps/sewer_watch/config.yaml` (no extra folder level).
3. **Settings → Apps → App store** → ⋮ (top right) → **Check for updates**, refresh the
   page. **Sewer Watch** appears under **Local apps** at the top (Ctrl+F5 if not).
4. Install (first build takes a few minutes). On **Configuration**, set
   `notify_service` to your phone, e.g. `mobile_app_pixel_8` (Developer Tools → Actions,
   search "notify.mobile_app"). Start it and turn on **Show in sidebar**.

If it doesn't appear: **Settings → System → Logs**, choose **Supervisor** in the
top-right dropdown; the validation error is at the bottom.

### First run

The first run reads the last `backfill_months` (default 12) of documents
quietly. When it finishes, it sends one "Sewer Watch is running" push listing
the recent mentions. After that, you only get alerts for new or re-uploaded
documents.

Older history is listed in the reader too. Opening an older document reads it
on demand.

## Options

| Option | Meaning |
|---|---|
| `notify_service` | Your phone's notify service, without the `notify.` prefix |
| `check_interval_minutes` | How often to check the county site |
| `backfill_months` | How far back to read on first run |
| `ocr` | OCR scanned PDFs (slower on a Raspberry Pi, but county minutes are often scans) |
| `notify_every_new_document` | `false` = only alert when a document mentions a keyword |
| `meeting_day_reminder` | Push on the morning of a meeting, with the livestream link |
| `keywords` | What to search for (case-insensitive) |
| `extra_pages` | Other pages to watch for new PDFs (for example, a Sanitation District page if one appears) |

## In Home Assistant

- `sensor.sewer_watch` has the last check time, the current agenda date and
  keywords, and the latest document and latest mention, with URLs.
- Every alert also fires the event `sewer_watch_alert` (with `title`,
  `message`, `links`, and `important`). You can build your own automations on
  it, such as flashing a light or sending a TTS announcement.
- Alerts also land in **Notifications** (full text) and the **Logbook**.

## Limits

- The county site has no feed, so this reads the pages. If the county
  redesigns the site, you'll get a "can't read the county site" push after 48
  hours rather than silence.
- State (KDEP) permits aren't scraped. Use the free email list on the
  [Water Public Notices page](https://eec.ky.gov/Environmental-Protection/Water/Pages/Water-Public-Notices-and-Hearings.aspx).
