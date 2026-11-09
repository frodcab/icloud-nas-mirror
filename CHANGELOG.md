# Changelog

Versions use `YYYY.MM.DD.NN` (date plus the number of the release that day).

## 2026.11.09.01 — English interface, optional login, tests in the build

- **`NAMES_LANG`** (`es` | `en`) chooses the names of the folders and suffixes the app creates on disk (`Albums`/`Álbumes`, `Hidden`/`Oculto`, `Upload`/`Subir`,
  `(edited)`/`(editada)`, `(NAS conflict …)`/`(conflicto NAS …)`). Default `es` (what existing installs use); the docker-compose template sets `en`.
- **English interface.** The panel, the login pages and the e-mails are available in English and Spanish (`ES · EN` selector, browser language by
  default, `NOTIFY_LANG` for e-mails). The Spanish text of the code is the key and `static/i18n-en.json` the catalog, so a new language is one file.
  Logs are now in English.
- The tests are now part of the build: `docker build --target test .` runs pyflakes and pytest inside the production Python image, and
  publishing an image requires them (plus a smoke test of the built image, and an arm64 native-library check) to pass first.
- New option `AUTH_DISABLED` to run the panel without the login (for setups already protected by a VPN or an authenticating proxy). Off by
  default; private networks only unless configured, anti-CSRF and Host checks stay on. `/health` reports `auth_disabled`.

## 2026.11.07.01 — first public release

First public version, extracted from the author's private repository.

- iCloud Drive ⇄ folder two-way sync, with conflict copies, trash and confirmation of large deletions.
- iCloud Photos ⇄ folder: originals, edited versions, Live Photos, albums, hidden photos, uploads from `Subir/`.
- Per-run limits, back-off and a watchdog to be gentle with the Apple account and to survive stuck runs.
- Web panel with password + TOTP login (Spanish only for now).
- Optional HEIC → JPG copies.
