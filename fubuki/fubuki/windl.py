"""Download Windows and UEFI Shell ISOs from Microsoft, the way Rufus's
Download button does.

This is a port of Fido (github.com/pbatard/Fido, GPLv3, Pete Batard), the
PowerShell script Rufus runs to get an ISO link out of Microsoft's servers.
The version table and every URL, parameter and header here come from Fido;
only the language it is written in changed. Microsoft's download flow needs
a per-edition session that is waved through two "protection" endpoints
before it will hand back the SKU list and, from a SKU, the ISO link.

Nothing here touches a disk: it returns a URL, and download() streams it to
a file. The window offers the same version / release / edition / language
choice Fido's dialog does.
"""

import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid

from .util import human_size, UsbError

from .i18n import _

# --- Fido's constants (Fido.ps1) -------------------------------------------

ORG_ID = "y6jn8c31"
PROFILE_ID = "606624d44113"
INSTANCE_ID = "560dc9f3-1aa5-4a2f-b63c-9e18f8d0e175"
TIMEOUT = 30

# The version table, transcribed from Fido's $WindowsVersions (Fido 1.71,
# 2026-10). Update it when Fido does; Microsoft retires product ids. Each Windows
# entry is (name, query-name, [releases]); each release is (label,
# [(edition, [product-edition-ids])]). ARM64 gets a second id, so ids are a
# list. UEFI Shell entries download straight from a GitHub release.
WINDOWS_VERSIONS = [
    {
        "name": "Windows 11", "query": "windows11", "kind": "windows",
        "releases": [
            {"label": "26H2 (Build 26300.9457 - 2026.09)", "editions": [
                ("Windows 11 Home/Pro/Edu", [3813, 3816]),
                ("Windows 11 Home China", [3814, 3817]),
                ("Windows 11 Pro China", [3815, 3818]),
            ]},
        ],
    },
    {
        "name": "Windows 10", "query": "Windows10ISO", "kind": "windows",
        "releases": [
            {"label": "22H2 v1 (Build 19045.2965 - 2023.05)", "editions": [
                ("Windows 10 Home/Pro/Edu", [2618]),
                ("Windows 10 Home China", [2378]),
            ]},
        ],
    },
    {
        "name": "UEFI Shell 2.2", "query": "UEFI_SHELL 2.2", "kind": "uefi",
        "releases": [
            {"label": lbl, "editions": [("Release", [0]), ("Debug", [1])]}
            for lbl in (
                "26H1 (edk2-stable202602)", "25H2 (edk2-stable202511)",
                "25H1 (edk2-stable202505)", "24H2 (edk2-stable202411)",
                "24H1 (edk2-stable202405)", "23H2 (edk2-stable202311)",
                "23H1 (edk2-stable202305)", "22H2 (edk2-stable202211)",
                "22H1 (edk2-stable202205)", "21H2 (edk2-stable202108)",
                "21H1 (edk2-stable202105)", "20H2 (edk2-stable202011)",
            )
        ],
    },
    {
        "name": "UEFI Shell 2.0", "query": "UEFI_SHELL 2.0", "kind": "uefi",
        "releases": [{"label": "4.632 [20100426]", "editions": [("Release", [0])]}],
    },
]

ARCH_FROM_TYPE = {0: "x86", 1: "x64", 2: "ARM64"}


def _ua():
    # Fido lets PowerShell send its default agent; a browser-like one is
    # safest against Microsoft's checks.
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class _Session:
    """A urllib opener that keeps cookies, like Fido's -SessionVariable."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, headers=None, method="GET"):
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", _ua())
        req.add_header("Accept", "*/*")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with self.opener.open(req, timeout=TIMEOUT) as r:
            return r.read(), r.headers


def _log(log, msg):
    if log:
        log(msg)


# --- the menu --------------------------------------------------------------

def versions():
    return [{"index": i, "name": v["name"], "kind": v["kind"]}
            for i, v in enumerate(WINDOWS_VERSIONS)]


def releases(version_index):
    v = WINDOWS_VERSIONS[version_index]
    return [{"index": i, "label": r["label"]} for i, r in enumerate(v["releases"])]


def editions(version_index, release_index, locale="en-US"):
    v = WINDOWS_VERSIONS[version_index]
    r = v["releases"][release_index]
    out = []
    for i, (name, ids) in enumerate(r["editions"]):
        # Fido hides the China editions unless the locale is Chinese.
        if "China" in name and not locale.lower().startswith("zh"):
            continue
        out.append({"index": i, "name": name, "ids": ids})
    return out


# --- the Microsoft session dance -------------------------------------------

def _query_locale(locale):
    return locale if locale else "en-US"


def _clear_session(session, session_id, query_locale, log):
    """vlscppe + ov-df: the two 'protection' hops Microsoft added. Without
    them getskuinformationbyproductedition returns an error."""
    # 1) whitelist the session id
    url = ("https://vlscppe.microsoft.com/tags?org_id=%s&session_id=%s"
           % (ORG_ID, session_id))
    _log(log, "Priming download session...")
    try:
        session.get(url)
    except Exception:
        pass  # Fido ignores redirects/errors here; the cookie is what counts

    # 2) mdt.js -> w and rticks
    url = ("https://ov-df.microsoft.com/mdt.js?instanceId=%s&PageId=si&session_id=%s"
           % (INSTANCE_ID, session_id))
    try:
        body, _h = session.get(url)
        text = body.decode("utf-8", "replace")
        w = re.search(r"[?&]w=([A-F0-9]+)", text)
        rticks = re.search(r'rticks\="?\+?(\d+)', text)
        if w and rticks:
            reply = ("https://ov-df.microsoft.com/?session_id=%s&CustomerId=%s&PageId=si&w=%s&mdt=%d&rticks=%s"
                     % (session_id, INSTANCE_ID, w.group(1),
                        int(time.time() * 1000), rticks.group(1)))
            try:
                session.get(reply)
            except Exception:
                pass
    except Exception:
        # ov-df is best-effort in Fido too; the SKU request retries anyway.
        pass


def _sku_error_message(session, query_locale, session_id):
    """Fido's 715-123130 handling: Microsoft returns a generic 'error' with
    type 9 when the IP is banned or region-blocked. Fetch the real message."""
    msg = ("Your IP address has been banned by Microsoft for issuing too many "
           "ISO download requests or for belonging to a sanctioned region. Try "
           "again later. If you believe this is an error, contact Microsoft "
           "referring to message code 715-123130 and session ID " + str(session_id) + ".")
    try:
        url = "https://www.microsoft.com/%s/software-download/windows11" % query_locale
        body, _h = session.get(url)
        text = body.decode("utf-8", "replace").replace("\n", "").replace("\r", "")
        m = re.search(r'<input id="msg-01" type="hidden" value="(.*?)"/>', text)
        if m and "715-123130" in m.group(1):
            v = m.group(1).replace("&lt;", "<")
            v = re.sub(r"<[^>]+>", "", v)
            v = re.sub(r"\s+", " ", v)
            return v + " " + str(session_id) + "."
    except Exception:
        pass
    return msg


def languages(version_index, edition, locale="en-US", log=None, cancel=None):
    """Ask Microsoft for the languages of an edition. Returns
    [{"name","display","data":[{"session_index","sku_id"}]}]. Each product
    edition id (x64 + ARM64) is its own session."""
    v = WINDOWS_VERSIONS[version_index]
    if v["kind"] == "uefi":
        return {"languages": [{"name": "en-us", "display": "English (US)",
                               "data": [{"session_index": 0, "sku_id": None}]}], "_sessions": []}
    query_locale = _query_locale(locale)
    sessions = []
    languages = {}   # language name -> {"display", "data":[...]}
    for session_index, edition_id in enumerate(edition["ids"]):
        if cancel:
            cancel.check()
        session = _Session()
        session_id = uuid.uuid4()
        sessions.append((session, session_id))
        _clear_session(session, session_id, query_locale, log)

        url = ("https://www.microsoft.com/software-download-connector/api/"
               "getskuinformationbyproductedition"
               "?profile=%s&productEditionId=%s&SKU=undefined"
               "&friendlyFileName=undefined&Locale=%s&sessionID=%s"
               % (PROFILE_ID, edition_id, query_locale, session_id))
        _log(log, "Requesting languages for edition %s..." % edition_id)
        data = _request_json_with_retry(session, url, None, log, cancel, session_id, query_locale)
        for sku in data.get("Skus", []):
            name = sku.get("Language")
            if name not in languages:
                languages[name] = {"display": sku.get("LocalizedLanguage", name), "data": []}
            languages[name]["data"].append({"session_index": session_index, "sku_id": sku.get("Id")})
    if not languages:
        raise UsbError(_("Microsoft returned no languages for this edition"))
    # Keep the sessions alive for the download-link step.
    out = []
    for name, info in languages.items():
        out.append({"name": name, "display": info["display"], "data": info["data"]})
    out.sort(key=lambda x: x["display"])
    return {"languages": out, "_sessions": sessions}


def _request_json_with_retry(session, url, headers, log, cancel, session_id, query_locale):
    last = None
    for attempt in range(3):
        if cancel:
            cancel.check()
        if attempt:
            time.sleep(2)
        try:
            body, _h = session.get(url, headers=headers)
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception as e:
            last = str(e)
            continue
        errors = data.get("Errors")
        if errors:
            if errors[0].get("Type") == 9:
                raise UsbError(_sku_error_message(session, query_locale, session_id))
            last = errors[0].get("Value") or "Microsoft returned an error"
            continue
        return data
    raise UsbError(_("Microsoft did not answer (%s)") % (last or "no response"))


def download_links(version_index, release_index, edition, language, log=None, cancel=None, sessions=None):
    """Turn a chosen language into ISO links, one per architecture. Returns
    [{"arch","url"}]. `sessions` comes from languages()['_sessions']."""
    v = WINDOWS_VERSIONS[version_index]
    if v["kind"] == "uefi":
        return _uefi_links(v, release_index, edition, log)
    query_locale = _query_locale("en-US")
    if not sessions:
        raise UsbError(_("the download session expired; pick the language again"))
    links = []
    ref = "https://www.microsoft.com/software-download/windows11"
    for entry in language["data"]:
        if cancel:
            cancel.check()
        session, session_id = sessions[entry["session_index"]]
        url = ("https://www.microsoft.com/software-download-connector/api/"
               "GetProductDownloadLinksBySku"
               "?profile=%s&productEditionId=undefined&SKU=%s"
               "&friendlyFileName=undefined&Locale=%s&sessionID=%s"
               % (PROFILE_ID, entry["sku_id"], query_locale, session_id))
        _log(log, "Requesting the ISO link...")
        data = _request_json_with_retry(session, url, {"Referer": ref}, log, cancel, session_id, query_locale)
        for opt in data.get("ProductDownloadOptions", []):
            arch = ARCH_FROM_TYPE.get(opt.get("DownloadType"), "Unknown")
            links.append({"arch": arch, "url": opt.get("Uri")})
    if not links:
        raise UsbError(_("Microsoft returned no download links"))
    return links


def _uefi_links(v, release_index, edition, log):
    rel = v["releases"][release_index]
    tag = rel["label"].split(" ")[0]
    shell_version = v["query"].split(" ")[1]
    base = "https://github.com/pbatard/UEFI-Shell/releases/download/" + tag
    suffix = "-RELEASE.iso" if edition["ids"][0] == 0 else "-DEBUG.iso"
    link = "%s/UEFI-Shell-%s-%s%s" % (base, shell_version, tag, suffix)
    archs = "x64"
    try:
        import xml.etree.ElementTree as ET
        with urllib.request.urlopen(base + "/Version.xml", timeout=TIMEOUT) as r:
            root = ET.fromstring(r.read())
        found = [a.text for a in root.iter("arch") if a.text]
        if found:
            archs = ", ".join(found)
    except Exception:
        pass
    return [{"arch": archs, "url": link}]


# --- the actual download ---------------------------------------------------

def filename_for(url):
    """The ISO name Microsoft's link carries, for the default save name."""
    m = re.search(r"/([^/?]+\.iso)", url)
    if m:
        return urllib.parse.unquote(m.group(1))
    return "windows.iso"


def head_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", _ua())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return int(r.headers.get("Content-Length") or 0) or None
    except Exception:
        return None


def download(url, dest, emitter=None, cancel=None, log=None, chunk=1024 * 1024):
    """Stream `url` to `dest`, reporting progress on phase 'download'."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _ua())
    tmp = dest + ".part"
    _log(log, "Downloading %s" % os.path.basename(dest))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        total = int(r.headers.get("Content-Length") or 0) or None
        done = 0
        with open(tmp, "wb") as f:
            while True:
                if cancel:
                    cancel.check()
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if emitter:
                    emitter.progress("download", (done / total) if total else None,
                                     "%s%s" % (human_size(done),
                                               " / " + human_size(total) if total else ""))
    os.replace(tmp, dest)
    _log(log, "Downloaded %s" % human_size(done))
    return {"path": dest, "size": done}
