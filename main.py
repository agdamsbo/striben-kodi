# -*- coding: utf-8 -*-
"""Kodi plugin entry point: list saved films, borrow on confirmation, play."""
import os
import re
import sys
from urllib.parse import parse_qsl, quote, urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib import api

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE = "plugin://%s/" % ADDON_ID
S = ADDON.getLocalizedString

# language alias tokens used to match Kodi's subtitle stream names
LANG_ALIASES = {
    "da": ("da", "dan", "danish", "dansk"),
    "en": ("en", "eng", "english", "engelsk"),
    "sv": ("sv", "swe", "swedish", "svensk"),
    "no": ("no", "nb", "nor", "norwegian", "norsk"),
    "de": ("de", "ger", "deu", "german", "tysk"),
}


def log(msg):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), xbmc.LOGINFO)


def make_client():
    profile = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    store = api.TokenStore(os.path.join(profile, "tokens.json"))
    return api.Client(
        store,
        agency=ADDON.getSetting("agency"),
        municipality=ADDON.getSetting("municipality"),
        cpr=ADDON.getSetting("cpr"),
        pin=ADDON.getSetting("pin"),
        log=log,
    ), store


def fail(exc):
    log("error: %r" % (exc,))
    xbmcgui.Dialog().ok("Filmstriben", str(exc))


# ------------------------------------------------------------------ listing

def list_favorites():
    client, _ = make_client()
    try:
        ids = client.favorites()
        movies = client.content(ids) if ids else []
    except Exception as e:
        fail(e)
        xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
        return
    xbmcplugin.setContent(HANDLE, "movies")
    for m in movies:
        f = m.get("fields") or {}
        title = f.get("primaryTitle") or f.get("originalTitle") or str(m["recordId"])
        li = xbmcgui.ListItem(label=title)
        tag = li.getVideoInfoTag()
        tag.setTitle(title)
        tag.setMediaType("movie")
        plot = f.get("primaryDescription") or ""
        if f.get("points") is not None:
            plot = "[B]%s %s[/B][CR][CR]%s" % (f["points"], S(30109), plot)
        tag.setPlot(plot)
        try:
            tag.setYear(int(f.get("year")))
        except (TypeError, ValueError):
            pass
        secs = int(f.get("lengthHours") or 0) * 3600 + int(f.get("lengthMinutes") or 0) * 60
        if secs:
            tag.setDuration(secs)
        tx = m.get("taxonomy") or {}
        if tx.get("director"):
            tag.setDirectors(tx["director"])
        if tx.get("genre"):
            tag.setGenres(tx["genre"])
        img = f.get("primaryImage") or ""
        if img.startswith("//"):
            img = "https:" + img
        if img:
            li.setArt({"poster": img, "thumb": img})
        li.setProperty("IsPlayable", "true")
        url = BASE + "?" + urlencode({"action": "play", "id": m["recordId"], "title": title,
                                      "points": f.get("points") or 0})
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    if not movies:
        xbmcgui.Dialog().notification("Filmstriben", S(30103), xbmcgui.NOTIFICATION_INFO)
    xbmcplugin.endOfDirectory(HANDLE)


# ----------------------------------------------------------------- playing

def play(record_id, title, cost):
    client, _ = make_client()
    try:
        play_url = client.restore(record_id)  # already borrowed? -> no new loan
        if not play_url:
            if not client.available(record_id):
                raise api.NotAvailable(S(30102))
            pts = client.points() or {}
            msg = S(30101).format(title=title, cost=cost)
            if pts.get("value") is not None:
                msg += "[CR]" + S(30104).format(left=pts.get("value"), quota=pts.get("quota"))
            if not xbmcgui.Dialog().yesno(S(30100), msg):
                xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
                return
            play_url = client.rent(record_id)
        info = client.stream_info(play_url)
    except Exception as e:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        fail(e)
        return

    try:
        import inputstreamhelper
        helper = inputstreamhelper.Helper("mpd", drm="com.widevine.alpha")
        if not helper.check_inputstream():
            xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
            return
    except ImportError:
        pass

    hdr = "User-Agent=%s&Origin=%s&Referer=%s" % (
        quote(api.UA, safe=""), quote(api.STREAM_SITE, safe=""), quote(api.STREAM_SITE + "/", safe=""))
    li = xbmcgui.ListItem(path=info["manifest"])
    li.setProperty("inputstream", "inputstream.adaptive")
    li.setProperty("inputstream.adaptive.manifest_type", "mpd")
    li.setProperty("inputstream.adaptive.license_type", "com.widevine.alpha")
    li.setProperty("inputstream.adaptive.license_key",
                   "%s|%s|R{SSM}|" % (info["license_url"], hdr))
    li.setProperty("inputstream.adaptive.stream_headers", hdr)
    li.setMimeType("application/dash+xml")
    li.setContentLookup(False)
    tag = li.getVideoInfoTag()
    tag.setTitle(title)
    subs = info.get("subtitles") or []
    if subs:
        li.setSubtitles([s["url"] for s in subs])
    xbmcplugin.setResolvedUrl(HANDLE, True, li)
    apply_subtitles(ADDON.getSetting("subtitles") or "off", bool(subs))


def _tokens(name):
    return set(re.split(r"[^a-z]+", name.lower())) - {""}


def apply_subtitles(pref, has_tracks):
    """Pick the preferred subtitle track once playback has started."""
    player = xbmc.Player()
    monitor = xbmc.Monitor()
    for _ in range(120):
        if player.isPlayingVideo():
            break
        if monitor.waitForAbort(0.5):
            return
    else:
        return
    streams = []
    for _ in range(20):  # streams are populated a moment after start
        streams = player.getAvailableSubtitleStreams()
        if streams:
            break
        if monitor.waitForAbort(0.5):
            return
    log("subtitle streams: %s (pref=%s)" % (streams, pref))
    if pref == "off" or not streams:
        player.showSubtitles(False)
        return
    idx = 0 if pref == "auto" else None
    if idx is None:
        wanted = set(LANG_ALIASES.get(pref, (pref,)))
        for i, name in enumerate(streams):
            if _tokens(name) & wanted:
                idx = i
                break
    if idx is None:
        player.showSubtitles(False)
        return
    player.setSubtitleStream(idx)
    player.showSubtitles(True)


# ------------------------------------------------------------------ router

def logout():
    _, store = make_client()
    store.clear()
    xbmcgui.Dialog().notification("Filmstriben", S(30105), xbmcgui.NOTIFICATION_INFO)


def router(query):
    p = dict(parse_qsl(query))
    action = p.get("action")
    if action == "play":
        play(int(p["id"]), p.get("title", ""), p.get("points", "?"))
    elif action == "logout":
        logout()
    else:
        list_favorites()


if __name__ == "__main__":
    router(sys.argv[2][1:] if len(sys.argv) > 2 else "")
