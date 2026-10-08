# Sewer Watch – Home Assistant app repository

Watches the Spencer County, KY Fiscal Court site (agendas, minutes, meeting
packets, public notices). It reads every PDF, using OCR for scans, and sends a
push alert with the sewer, Top Flight, and Shelbyville Rd passages plus links
to the source.

## Install

1. In Home Assistant, go to **Settings → Apps → App store** → ⋮ → **Repositories**.
2. Add `https://github.com/Zouljiin/ha-sewer-watch`.
3. Install **Sewer Watch**, start it, open it in the sidebar, and tick
   your phone under **Send alerts to**.
4. Start it and turn on **Show in sidebar**.

See [sewer_watch/DOCS.md](sewer_watch/DOCS.md) for details.
