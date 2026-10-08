# Sewer Watch – Home Assistant app repository

Watches the Spencer County, KY Fiscal Court site (agendas, minutes, meeting
packets, public notices). It reads every PDF, using OCR for scans, and sends a
push alert with the sewer, Top Flight, and Shelbyville Rd passages plus links
to the source.

## Install

1. In Home Assistant, go to **Settings → Apps → App store** → ⋮ → **Repositories**.
2. Add `https://github.com/Zouljiin/ha-sewer-watch`.
3. Install **Sewer Watch**. Start it and turn on **Show in sidebar**.
4. Open **Sewer Watch** in the sidebar, tick your phone under **Send alerts to**,
   and click **Save & send test**.

See [sewer_watch/DOCS.md](sewer_watch/DOCS.md) for details.
