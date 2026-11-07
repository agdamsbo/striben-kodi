# -*- coding: utf-8 -*-
"""Filmstriben client: login via login.bib.dk, GraphQL API, play-page parsing.

Reverse-engineered from browser traffic of filmstriben.dk. Everything here
runs with the user's own library credentials, for personal use.
"""
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urljoin, urlparse

import requests

API_URL = "https://api.filmstriben.dk/fil/graphql"
SITE = "https://filmstriben.dk"
STREAM_SITE = "https://streaming.filmstriben.dk"
PORTAL = "at_home"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:159.0) Gecko/20100101 Firefox/159.0"
DEFAULT_TENANT = "5c8ddf99"
LICENSE_URL = "https://{tenant}.drm-widevine-licensing.axprod.net/AcquireLicense"

# Public client credentials that the filmstriben.dk front end itself uses to
# obtain an anonymous API token.
CLIENT_ID = "0d3b086940a035acd91f4f35652d689f"
CLIENT_KEY = "c19d251e70690c6b996b5f7f3de7e8be"

Q_AUTH = ('query Authentication { authentication( clientId: "%s" clientKey: "%s" )'
          ' { expireIn token __typename } }' % (CLIENT_ID, CLIENT_KEY))

Q_AUTH_USER = (
    "mutation AuthenticateUser($errorRedirect: String, $successRedirect: String, "
    "$preferWithoutMitID: Boolean) { authenticateUserWithRedirect("
    "errorRedirect: $errorRedirect successRedirect: $successRedirect "
    "preferWithoutMitID: $preferWithoutMitID ) { url __typename } }")

Q_FAVORITES = (
    "query GetCommonUserInformation($token: String, $refreshToken: String) { "
    "user(token: $token, refreshToken: $refreshToken) { favorites { listId: id "
    "hitCount unfilteredTotalHits elements { id faustNumber created "
    "movie(idOnly: true) { id: recordId __typename } __typename } __typename } "
    "__typename } }")

Q_POINTS = (
    "query GetCommonUserInformation($token: String, $refreshToken: String) { "
    "user(token: $token, refreshToken: $refreshToken) { points { month { quota "
    "value __typename } __typename } __typename } }")

Q_CONTENT = (
    "query GetContent($portal: availablePortal, $recordIds: [Int]) { "
    "movies(recordId: $recordIds, portal: $portal) { recordId id: recordId type "
    "fields { originalTitle primaryTitle primaryImage primaryDescription url year "
    "lengthHours lengthMinutes ageRestriction points __typename } "
    "taxonomy { category actor director genre __typename } __typename } }")

Q_AVAILABLE = (
    "query CheckLoanAvailability($portal: availablePortal, $recordId: Int) { "
    "movie(recordId: $recordId, portal: $portal) { availableForLoan __typename } }")

Q_RESTORE = (
    "query RestoreMovieRent($portal: availablePortal, $recordId: Int, "
    "$token: String, $refreshToken: String) { movie(recordId: $recordId) { "
    "moviePlaylist(portal: $portal, token: $token, refreshToken: $refreshToken) "
    "__typename } }")

Q_RENT = (
    "query RentMovie($portal: availablePortal, $recordId: Int, $token: String, "
    "$refreshToken: String, $platform: availablePlatform, "
    "$notificationsServiceCallbackId: String) { movie(portal: $portal, "
    "recordId: $recordId) { rentMovie( token: $token refreshToken: $refreshToken "
    "platform: $platform notificationsServiceCallbackId: "
    "$notificationsServiceCallbackId ) __typename } }")

_AUTH_ERR = re.compile(r"token|authenticat|unauthori|expired|not logged|login", re.I)


class FilmstribenError(Exception):
    pass


class AuthError(FilmstribenError):
    pass


class NotAvailable(FilmstribenError):
    pass


class TokenStore(object):
    """Tiny JSON file holding the API tokens (not the credentials)."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        try:
            with open(path, "r") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, **kw):
        self.data.update(kw)
        self._save()

    def clear(self):
        self.data = {}
        self._save()

    def drop(self, *keys):
        for k in keys:
            self.data.pop(k, None)
        self._save()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.data, f)
            os.chmod(self.path, 0o600)
        except Exception:
            pass


# ---------------------------------------------------------------- parsing ---

def parse_login_form(html, page_url):
    """Return (post_url, library_name_field) from the login.bib.dk login page."""
    tag = re.search(r"<form\b[^>]*borchk-login-form[^>]*>", html)
    if not tag:
        raise FilmstribenError("Login form not found - the login page has changed")
    action = re.search(r'action="([^"]+)"', tag.group(0))
    if not action:
        raise FilmstribenError("Login form has no action")
    name = re.search(r'name="(libraryName-[0-9.]+)"', html)
    return urljoin(page_url, action.group(1)), (name.group(1) if name else "libraryName")


def parse_auth_redirect(location):
    """Pull {'token':..., 'expiresIn':...} out of the final login redirect."""
    q = parse_qs(urlparse(location).query)
    raw = q.get("biblogin-authentication-data", [None])[0]
    if not raw:
        return None
    return json.loads(raw)


def parse_play_page(html):
    """Extract manifest URL, Widevine JWT and license tenant from a play page."""
    manifest = re.search(r"https?://[^\"'\s]+?manifest\.mpd[^\"'\s]*", html)
    jwt = re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", html)
    if not manifest or not jwt:
        raise FilmstribenError("Could not find stream details on the play page")
    tenant = re.search(r',"([0-9a-f]{8})",!?[01]\)', html)
    return {
        "manifest": manifest.group(0),
        "jwt": jwt.group(0),
        "tenant": tenant.group(1) if tenant else DEFAULT_TENANT,
    }


def parse_mpd_subtitles(mpd_xml, manifest_url):
    """Return [{'lang','label','url'}] for text/vtt tracks in a DASH manifest."""
    out = []
    try:
        root = ET.fromstring(mpd_xml.encode("utf-8") if isinstance(mpd_xml, str) else mpd_xml)
    except ET.ParseError:
        return out
    for el in root.iter():
        if not el.tag.endswith("AdaptationSet"):
            continue
        if el.get("mimeType") != "text/vtt" and el.get("contentType") != "text":
            continue
        label = None
        for ch in el:
            if ch.tag.endswith("Label") and ch.text:
                label = ch.text.strip()
        for rep in el.iter():
            if rep.tag.endswith("BaseURL") and rep.text:
                out.append({
                    "lang": (el.get("lang") or "").lower(),
                    "label": label or el.get("lang") or "",
                    "url": urljoin(manifest_url, rep.text.strip()),
                })
    return out


# ----------------------------------------------------------------- client ---

class Client(object):
    def __init__(self, store, agency, municipality, cpr, pin, log=None):
        self.store = store
        self.agency = agency.strip()
        self.municipality = (municipality or "").strip()
        self.cpr = (cpr or "").replace("-", "").strip()
        self.pin = pin or ""
        self.log = log or (lambda msg: None)
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "da,en;q=0.8"})
        # Drop stored tokens if the credentials were changed in settings.
        sig = hashlib.sha256(("%s|%s|%s" % (self.agency, self.cpr, self.pin)).encode()).hexdigest()
        if self.store.get("cred_sig") != sig:
            self.store.clear()
            self.store.set(cred_sig=sig)

    # -- low level
    def _gql(self, op, query, variables, auth=True):
        headers = {"Content-Type": "application/json", "Origin": SITE, "Referer": SITE + "/"}
        if auth:
            headers["Authorization"] = "Bearer " + self._bearer()
        r = self.s.post(API_URL, json={"operationName": op, "variables": variables,
                                       "query": query}, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def _bearer(self):
        tok = self.store.get("bearer")
        if tok and self.store.get("bearer_exp", 0) > time.time() + 120:
            return tok
        d = self._gql("Authentication", Q_AUTH, {}, auth=False)
        a = (d.get("data") or {}).get("authentication")
        if not a or not a.get("token"):
            raise FilmstribenError("Could not get an API token from filmstriben.dk")
        self.store.set(bearer=a["token"], bearer_exp=time.time() + int(a.get("expireIn", 3600)))
        return a["token"]

    # -- login
    def login(self):
        if not self.cpr or not self.pin or not self.agency:
            raise AuthError("Enter municipality, CPR number and PIN in the add-on settings")
        self.log("login: requesting OAuth URL")
        d = self._gql("AuthenticateUser", Q_AUTH_USER, {
            "errorRedirect": SITE + "/#authenticationFailed=true",
            "successRedirect": SITE + "/#applyScroll=true",
            "preferWithoutMitID": False})
        url = ((d.get("data") or {}).get("authenticateUserWithRedirect") or {}).get("url")
        if not url:
            raise FilmstribenError("API did not return a login URL")
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        post_url, libfield = parse_login_form(r.text, r.url)
        data = {libfield: self.municipality or self.agency, "agency": self.agency,
                "loginBibDkUserId": self.cpr, "pincode": self.pin}
        origin = "{0.scheme}://{0.netloc}".format(urlparse(post_url))
        self.log("login: submitting credentials to " + urlparse(post_url).netloc)
        resp = self.s.post(post_url, data=data, allow_redirects=False, timeout=30,
                           headers={"Origin": origin, "Referer": r.url})
        for _ in range(10):
            if resp.status_code not in (301, 302, 303, 307, 308):
                raise AuthError("Login was rejected - check municipality, CPR and PIN")
            loc = urljoin(resp.url, resp.headers.get("Location", ""))
            # NB: the OAuth "state" parameter embeds the error-redirect URL too, so
            # only the fragment of a redirect that lands on the site counts.
            parsed = urlparse(loc)
            if parsed.netloc.endswith("filmstriben.dk") and "authenticationFailed" in parsed.fragment:
                raise AuthError("Login failed - check municipality, CPR and PIN")
            auth = parse_auth_redirect(loc)
            if auth and auth.get("token"):
                exp = time.time() + min(float(auth.get("expiresIn", 0)) / 1000.0, 86400 * 30)
                self.store.set(token=auth["token"], token_exp=exp)
                self.log("login: ok")
                return
            resp = self.s.get(loc, allow_redirects=False, timeout=30,
                              headers={"Referer": resp.url})
        raise AuthError("Login redirect chain did not finish")

    def _token(self):
        if not self.store.get("token") or self.store.get("token_exp", 0) < time.time() + 3600:
            self.login()
        return self.store.get("token")

    def _authed(self, op, query, variables):
        for attempt in (0, 1):
            v = dict(variables)
            v.update(token=self._token(), refreshToken=None)
            d = self._gql(op, query, v)
            msgs = " ".join(e.get("message", "") for e in d.get("errors") or [])
            if msgs and _AUTH_ERR.search(msgs):
                if attempt == 0:
                    self.store.drop("token", "token_exp")
                    continue
                raise AuthError(msgs)
            return d
        raise AuthError("Authentication failed")

    # -- data
    def favorites(self):
        d = self._authed("GetCommonUserInformation", Q_FAVORITES, {})
        user = (d.get("data") or {}).get("user")
        if user is None:
            raise AuthError("Not logged in")
        ids = []
        for el in (user.get("favorites") or {}).get("elements") or []:
            mid = (el.get("movie") or {}).get("id")
            if mid is not None:
                ids.append(int(mid))
        return ids

    def content(self, ids):
        by_id = {}
        for i in range(0, len(ids), 40):
            d = self._gql("GetContent", Q_CONTENT, {"recordIds": ids[i:i + 40], "portal": PORTAL})
            for m in (d.get("data") or {}).get("movies") or []:
                by_id[int(m["recordId"])] = m
        return [by_id[i] for i in ids if i in by_id]

    def points(self):
        try:
            d = self._authed("GetCommonUserInformation", Q_POINTS, {})
            return (((d.get("data") or {}).get("user") or {}).get("points") or {}).get("month")
        except Exception:
            return None

    def available(self, record_id):
        d = self._gql("CheckLoanAvailability", Q_AVAILABLE, {"recordId": record_id, "portal": PORTAL})
        return bool(((d.get("data") or {}).get("movie") or {}).get("availableForLoan"))

    def restore(self, record_id):
        """Play URL if the film is already borrowed, otherwise None."""
        d = self._authed("RestoreMovieRent", Q_RESTORE, {"recordId": record_id, "portal": PORTAL})
        return ((d.get("data") or {}).get("movie") or {}).get("moviePlaylist")

    def rent(self, record_id):
        d = self._authed("RentMovie", Q_RENT, {"recordId": record_id, "portal": PORTAL})
        url = ((d.get("data") or {}).get("movie") or {}).get("rentMovie")
        if not url:
            msgs = "; ".join(e.get("message", "") for e in d.get("errors") or []) or "unknown error"
            raise NotAvailable("Borrowing failed: " + msgs)
        return url

    # -- playback
    def stream_info(self, play_url):
        r = self.s.get(play_url, timeout=30, headers={"Referer": SITE + "/"})
        r.raise_for_status()
        info = parse_play_page(r.text)
        info["license_url"] = "%s?AxDrmMessage=%s" % (
            LICENSE_URL.format(tenant=info["tenant"]), info["jwt"])
        info["subtitles"] = []
        try:
            m = self.s.get(info["manifest"], timeout=30, headers={"Referer": STREAM_SITE + "/"})
            if m.ok:
                info["subtitles"] = parse_mpd_subtitles(m.text, info["manifest"])
        except Exception as e:  # subtitles are optional
            self.log("subtitle lookup failed: %s" % e)
        return info
