# striben-kodi

This is an AI-assisted reverse-engineered Kodi client for the Danish public libraries movie renting service Filmstriben. 

Lists your saved ("gemte") films, borrows a film after you confirm, and plays it.

## Install on LibreELEC
1. Copy `plugin.video.filmstriben.zip` to the box (or a USB stick).
2. Kodi: Settings > Add-ons > Install from zip file (allow "Unknown sources" if asked).
3. Kodi installs the dependencies automatically (requests, InputStream Adaptive,
   InputStream Helper). On first playback InputStream Helper offers to download
   the Widevine module - accept.
4. Add-on settings: municipality name, library ID (Silkeborg = 774000), CPR, PIN,
   default subtitle language.

## Notes
- Library ID for municipal libraries = 7 + 3-digit municipality code + 00.
- CPR and PIN are stored in plain text in Kodi's add-on settings
  (`/storage/.kodi/userdata/addon_data/plugin.video.filmstriben/settings.xml`).
  Only the API tokens are kept separately in `tokens.json` in the same folder.
- Selecting a film that is already borrowed plays it without using another loan.
- If something fails: enable Kodi debug logging, reproduce, and send the log
  (lines tagged `[plugin.video.filmstriben]`). No credentials are written to it.
- Unofficial: built from the website's own traffic, so it can break if the
  service changes.
