"""
KONE Service Report Downloader — Streamlit Web App
Upload .msg notification emails → download all PDF reports → get a ZIP file.
"""

import re
import json
import base64
import asyncio
import threading
import tempfile
import io
import hashlib
import zipfile
import os
import time
import sys
import subprocess
import shutil
from pathlib import Path

import streamlit as st
import pandas as pd
import extract_msg as _extract_msg
from pypdf import PdfReader, PdfWriter

# ── Chromium installation (cached, runs once per deployment) ──────────────────
# Streamlit Community Cloud's apt step (packages.txt) is unusable: the platform
# image still lists the EOL bullseye-security repo, whose Release file expired,
# so `apt-get update` exits non-zero and the whole deployment is refused.
# The shared libraries Chromium needs are therefore installed here at runtime,
# without root: apt resolves and downloads the .deb files (read-only operations
# a normal user may run), they are unpacked into a private prefix, and that
# prefix is exposed to Chromium through LD_LIBRARY_PATH.

_LIB_PREFIX = Path.home() / ".cache" / "chromium-sysdeps"

# Debian Trixie names — the *t64 variants, see PROJECT_CONTEXT.md
_CHROMIUM_SYSDEPS = [
    "libnss3", "libnspr4", "libatk1.0-0t64", "libatk-bridge2.0-0t64",
    "libatspi2.0-0t64", "libcups2t64", "libdrm2", "libxkbcommon0", "libgbm1",
    "libasound2t64", "libx11-xcb1", "libxcomposite1", "libxdamage1",
    "libxfixes3", "libxrandr2", "libpango-1.0-0", "libcairo2", "libdbus-1-3",
]


def _lib_env() -> dict:
    """LD_LIBRARY_PATH pointing at the private prefix (empty if nothing there)."""
    dirs = [
        str(p) for p in (
            _LIB_PREFIX / "usr" / "lib" / "x86_64-linux-gnu",
            _LIB_PREFIX / "lib" / "x86_64-linux-gnu",
            _LIB_PREFIX / "usr" / "lib",
        ) if p.is_dir()
    ]
    if not dirs:
        return {}
    current = os.environ.get("LD_LIBRARY_PATH", "")
    return {"LD_LIBRARY_PATH": ":".join(dirs + ([current] if current else []))}


def _chromium_binaries() -> list:
    root = Path.home() / ".cache" / "ms-playwright"
    found = list(root.glob("chromium-*/chrome-linux*/chrome"))
    found += list(root.glob("chromium_headless_shell-*/chrome-linux*/headless_shell"))
    return [p for p in found if p.is_file()]


def _missing_libs(binaries) -> list:
    """Shared libraries Chromium asks for but cannot find, via ldd."""
    missing = set()
    for binary in binaries:
        out = subprocess.run(
            ["ldd", str(binary)], capture_output=True, text=True, timeout=60,
            env={**os.environ, **_lib_env()},
        ).stdout
        for line in out.splitlines():
            if "not found" in line:
                missing.add(line.split("=>")[0].strip())
    return sorted(missing)


def _install_sysdeps() -> str:
    """Unpack Chromium's shared libraries into _LIB_PREFIX without root."""
    log = []
    debs = _LIB_PREFIX / "debs"
    debs.mkdir(parents=True, exist_ok=True)

    # Full dependency closure — apt-cache is read-only, no root needed
    dep = subprocess.run(
        ["apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests",
         "--no-conflicts", "--no-breaks", "--no-replaces", "--no-enhances",
         *_CHROMIUM_SYSDEPS],
        capture_output=True, text=True, timeout=180,
    )
    names = sorted({
        line.strip() for line in dep.stdout.splitlines()
        if line and not line[0].isspace() and "<" not in line
    })
    if not names:
        return "apt-cache depends returned nothing:\n" + (dep.stdout + dep.stderr).strip()
    log.append(f"resolving {len(names)} packages")

    # apt-get download writes into the cwd and needs no root either
    for i in range(0, len(names), 40):
        chunk = names[i:i + 40]
        res = subprocess.run(
            ["apt-get", "download", *chunk], cwd=str(debs),
            capture_output=True, text=True, timeout=900,
        )
        if res.returncode != 0:
            # one bad name must not sink the whole chunk
            for name in chunk:
                subprocess.run(
                    ["apt-get", "download", name], cwd=str(debs),
                    capture_output=True, text=True, timeout=300,
                )

    archives = list(debs.glob("*.deb"))
    log.append(f"downloaded {len(archives)} .deb files")
    for deb in archives:
        subprocess.run(
            ["dpkg-deb", "-x", str(deb), str(_LIB_PREFIX)],
            capture_output=True, text=True, timeout=120,
        )
    return "\n".join(log)


@st.cache_resource(show_spinner=False)
def _install_chromium():
    log = []
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300,
        )
        log.append((result.stdout + result.stderr).strip())
        if result.returncode != 0:
            return False, "\n".join(log)

        # The ldd / .deb dance below is only meaningful on the Linux host of
        # Streamlit Cloud; a local Windows or macOS run stops here.
        if sys.platform != "linux":
            return True, "\n".join(log)

        os.environ.update(_lib_env())
        binaries = _chromium_binaries()
        if not binaries:
            log.append("No Chromium binary found under ~/.cache/ms-playwright")
            return False, "\n".join(log)

        missing = _missing_libs(binaries)
        if missing:
            log.append("Missing shared libraries: " + ", ".join(missing))
            log.append(_install_sysdeps())
            os.environ.update(_lib_env())
            missing = _missing_libs(binaries)
            if missing:
                log.append("Still missing after local install: " + ", ".join(missing))
                return False, "\n".join(log)
            log.append(f"Shared libraries installed into {_LIB_PREFIX}")
        return True, "\n".join(log)
    except Exception as exc:
        log.append(str(exc))
        return False, "\n".join(log)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="KONE Report Downloader",
    page_icon="📥",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { font-family: 'Inter', sans-serif !important; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

[data-testid="stAppViewContainer"] {
    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
    min-height: 100vh;
}

[data-testid="stSidebar"] { display: none !important; }
[data-testid="collapsedControl"] { display: none !important; }

.hero-section {
    background: linear-gradient(135deg, #6366f1 0%, #4f46e5 50%, #4338ca 100%);
    border-radius: 20px;
    padding: 52px 40px;
    margin-bottom: 28px;
    text-align: center;
    box-shadow: 0 20px 60px rgba(99, 102, 241, 0.3);
}
.hero-section h1 { color: white; font-size: 2.4rem; font-weight: 700; margin: 0 0 10px 0; letter-spacing: -0.5px; }
.hero-section p  { color: rgba(255,255,255,0.85); font-size: 1.1rem; margin: 0; }

.card {
    background: white;
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 20px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.06);
    border: 1px solid rgba(0,0,0,0.05);
}

.info-box {
    background: linear-gradient(135deg, #ede9fe, #e0e7ff);
    border-left: 4px solid #6366f1;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 12px 0;
    font-size: 0.92rem;
    color: #3730a3;
    line-height: 1.6;
}
.warn-box {
    background: #fefce8;
    border-left: 4px solid #f59e0b;
    border-radius: 8px;
    padding: 14px 18px;
    margin: 12px 0;
    font-size: 0.92rem;
    color: #78350f;
}

.badge {
    display: inline-block;
    padding: 3px 11px;
    border-radius: 20px;
    font-size: 0.78rem;
    font-weight: 600;
}
.badge-ok   { background: #d1fae5; color: #065f46; }
.badge-skip { background: #fef3c7; color: #92400e; }
.badge-fail { background: #fee2e2; color: #991b1b; }

.status-dot {
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    margin-right: 7px;
    vertical-align: middle;
}
.dot-green  { background: #10b981; }
.dot-red    { background: #ef4444; }
.dot-yellow { background: #f59e0b; }

/* Buttons */
.stButton > button {
    background: linear-gradient(135deg, #6366f1, #4f46e5) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 12px 28px !important;
    font-weight: 600 !important;
    font-size: 1rem !important;
    box-shadow: 0 4px 15px rgba(99, 102, 241, 0.35) !important;
    transition: all 0.2s ease !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(99, 102, 241, 0.5) !important;
}
.stDownloadButton > button {
    background: linear-gradient(135deg, #10b981, #059669) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 14px 32px !important;
    font-weight: 600 !important;
    font-size: 1.05rem !important;
    box-shadow: 0 4px 15px rgba(16, 185, 129, 0.4) !important;
    width: 100% !important;
}

/* Progress bar */
.stProgress > div > div > div { background-color: #6366f1 !important; }

/* Dataframe */
[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; }

/* File uploader */
[data-testid="stFileUploaderDropzone"] {
    border: 2px dashed #a5b4fc !important;
    border-radius: 14px !important;
    background: rgba(238, 242, 255, 0.5) !important;
}
</style>
""", unsafe_allow_html=True)

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_FILES = 50

STATUS_META = {
    "ok":          ("✓ Downloaded",  "badge-ok"),
    "expired":     ("⊘ Link Expired","badge-skip"),
    "no_button":   ("⊘ No Button",  "badge-skip"),
    "no_download": ("⊘ No File",    "badge-skip"),
    "timeout":     ("✗ Timeout",    "badge-fail"),
    "error":       ("✗ Error",      "badge-fail"),
}

# ── Core helpers ──────────────────────────────────────────────────────────────

# The report link's visible text differs by portal language:
#   German → "Jobdetails"   |   Dutch → "Statusinformatie"
REPORT_LINK_TEXTS = ("jobdetails", "statusinformatie")


def _url_from_anchor_attrs(attrs: str) -> str | None:
    """Extract the real KONE URL from an <a> tag's attributes."""
    from urllib.parse import urlparse, parse_qs, unquote
    # Forwarded via Outlook SafeLinks — the original URL is in `originalsrc`.
    o = re.search(r'originalsrc=["\']([^"\']*click\.hello\.kone\.com[^"\']*)["\']', attrs, re.IGNORECASE)
    if o:
        return o.group(1)
    h = re.search(r'href=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
    if h:
        url = h.group(1).replace("&amp;", "&")
        if "safelinks.protection.outlook.com" in url:
            inner = parse_qs(urlparse(url).query).get("url", [None])[0]
            if inner:
                return unquote(inner)
        return url
    return None


def extract_url(msg_bytes: bytes) -> str | None:
    """
    Extract the KONE service-report URL from raw .msg bytes.
    Works for both German ("Jobdetails") and Dutch ("Statusinformatie") emails,
    whether sent directly or forwarded through Outlook SafeLinks.
    """
    tmp_path = None
    msg = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
            tmp.write(msg_bytes)
            tmp_path = tmp.name
        msg = _extract_msg.Message(tmp_path)
        html = (msg.htmlBody or b"").decode("utf-8", errors="ignore")

        # Primary: find the anchor whose visible text is the report link
        # ("Jobdetails" / "Statusinformatie") and pull its URL. This targets the
        # correct link even when the email has several KONE links (footer, etc.).
        for a in re.finditer(r'<a\b([^>]*)>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
            attrs, inner = a.group(1), a.group(2)
            text = re.sub(r'<[^>]+>', '', inner).strip().lower()
            if text in REPORT_LINK_TEXTS:
                url = _url_from_anchor_attrs(attrs)
                if url:
                    return url

        # Fallback: first KONE click-link preserved in an `originalsrc` attribute.
        m = re.search(
            r'originalsrc=["\']([^"\']*click\.hello\.kone\.com[^"\']*)["\']',
            html, re.IGNORECASE,
        )
        if m:
            return m.group(1)

        return None
    except Exception:
        return None
    finally:
        # Close the message first so the temp file can be removed on Windows too.
        if msg is not None:
            try:
                msg.close()
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def safe_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", text)


def address_from_filename(filename: str) -> str:
    """
    Derive the building address from a .msg filename, handling both languages
    and forwarded/replied prefixes:
      "Ihr Service-Update _ Eifelplatz 17.msg"              -> "Eifelplatz 17"
      "FW_ Servicebezoek afgerond _ Stationsplein 52.msg"  -> "Stationsplein 52"
    """
    name = re.sub(r'\.msg$', '', filename, flags=re.IGNORECASE)
    name = re.sub(r'^(FW|FWD|WG|RE|AW)_\s*', '', name, flags=re.IGNORECASE)   # forward/reply prefixes
    name = re.sub(r'^(Ihr Service-Update|Servicebezoek afgerond)\s*_\s*', '', name, flags=re.IGNORECASE)
    return name.strip()


def _reports_from_aura(body: str) -> list:
    """
    PDFs embedded in the portal's getServiceReport Aura response, as
    [(filename, pdf_bytes), ...]. The server returns each report as base64 in
    `documentBody`; the page itself only wraps that in a download link.
    """
    from urllib.parse import unquote
    data = json.loads(body[body.index("{"):])          # tolerate a while(1); prefix
    reports = []
    for action in data.get("actions", []):
        if action.get("state") != "SUCCESS":
            continue
        for rep in action.get("returnValue") or []:
            b64 = unquote(rep.get("documentBody") or "")  # the page decodes it the same way
            if b64:
                reports.append((rep.get("name") or "report.pdf", base64.b64decode(b64)))
    return reports


# ── Collective reports ────────────────────────────────────────────────────────
# Since Oct 2026 KONE often sends one PDF per site visit instead of one per
# order: a "Site Visit Service Report" cover page listing every order of the
# visit, then one "Service Report" per order. KONE builds that PDF by merging
# one PDF per report. The cover is German; the report pages come with German or
# English labels. The cover can also list orders that are not finished yet
# (status "Geplant", "Angekommen") and have no report pages in the PDF.
#
# Where each report starts is decided by independent witnesses, the first two
# of which do not depend on any label wording:
#   1. order numbers: the cover table is read by its column layout, and a report
#      starts on the page whose details block names its order and its lift on
#      one line;
#   2. fonts: every merged report brings its own font subset tags, so a page
#      that shares no tag with the page before is the first page of a report;
#   3. labels ("Auftragsnr." / "Service Order"), where recognised.
# A PDF is cut only when the witnesses agree; otherwise it stays whole and the
# user is told why. Pages without a text layer are read with Azure Document
# Intelligence (OCR) when its credentials are configured.

_ORDER_NO    = re.compile(r'[0-9A-Z]{6,}')
_ORDER_START = re.compile(r'^\s*(?:Auftragsnr\.|Service Order)\s+([0-9A-Z]{6,})\b', re.MULTILINE)
_LABELS = {   # report-page labels: German, English
    "street":     ("Straße", "Street"),
    "asset":      ("Vermögenswert", "Equipment"),
    "visit_type": ("Auftragsgrund", "Customer Facing"),
}
# Cover statuses seen on orders that have no report pages yet. A cover order
# with any other status ("Abgeschlossen", "Unterschrift", …) must have a report.
_OPEN_STATUSES = {"geplant", "angekommen"}


def _layout_field(text: str, labels: tuple) -> str:
    """Value right of the first label found in pypdf's layout text ("Straße      Musterstraße 1")."""
    for label in labels:
        m = re.search(rf'{re.escape(label)} {{2,}}(\S.*?)(?: {{2,}}|$)', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""


def _mentions(text: str, order: str) -> bool:
    """True when the order number stands in the text on its own, not inside a longer number."""
    return re.search(rf'(?<![0-9]){re.escape(order)}(?![0-9])', text) is not None


def _font_tags(page) -> frozenset:
    """Subset tags of the fonts a page uses ("ABCDEF" of "ABCDEF+ArialMT")."""
    tags = set()
    try:
        res = page.get("/Resources")
        fonts = res.get_object().get("/Font") if res else None
        for ref in (fonts.get_object().values() if fonts else []):
            base = str(ref.get_object().get("/BaseFont") or "")
            if "+" in base:
                tags.add(base.lstrip("/").split("+")[0])
    except Exception:
        pass
    return frozenset(tags)


def _merge_boundaries(page_tags: list) -> list:
    """
    Pages that share no font tag with the last page before them that used fonts:
    the first page of each PDF KONE merged in. Pages without fonts are skipped.
    """
    bounds, last = [], None
    for i, tags in enumerate(page_tags):
        if not tags:
            continue
        if last is not None and not (tags & last):
            bounds.append(i)
        last = tags
    return bounds


def _ocr_layout_texts(pdf_path: str, page_idxs: list, di: dict) -> dict:
    """
    {page index: text} of the given pages, read by Azure Document Intelligence
    ("prebuilt-read") and rebuilt line by line in the style of pypdf's layout
    mode: words of one visual line joined by a space, a wide gap written as
    several spaces (so "Auftragsnr.    000000000001" reads like a text page).
    """
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential

    client = DocumentIntelligenceClient(endpoint=di["endpoint"], credential=AzureKeyCredential(di["key"]))
    with open(pdf_path, "rb") as fh:
        poller = client.begin_analyze_document(
            "prebuilt-read", io.BytesIO(fh.read()), pages=",".join(str(i + 1) for i in page_idxs))
    texts = {}
    for page in poller.result().pages or []:
        words = []
        for w in page.words or []:
            xs, ys = w.polygon[0::2], w.polygon[1::2]
            words.append({"x0": min(xs), "x1": max(xs), "y": (min(ys) + max(ys)) / 2,
                          "h": max(ys) - min(ys), "text": w.content})
        words.sort(key=lambda w: w["y"])
        lines = []
        for w in words:   # same visual line = vertical centres within half a word height
            if lines and abs(w["y"] - lines[-1][0]["y"]) < 0.5 * max(w["h"], lines[-1][0]["h"]):
                lines[-1].append(w)
            else:
                lines.append([w])
        out = []
        for line in lines:
            line.sort(key=lambda w: w["x0"])
            text = line[0]["text"]
            for prev, w in zip(line, line[1:]):
                char_w = (prev["x1"] - prev["x0"]) / max(len(prev["text"]), 1)
                text += ("    " if w["x0"] - prev["x1"] > 2 * char_w else " ") + w["text"]
            out.append(text)
        texts[page.page_number - 1] = "\n".join(out)
    return texts


def _cover_table(texts: list, labelled: dict) -> tuple:
    """
    (cover, n_cover): the order table on the leading pages, recognised by its
    column layout rather than its labels
    (Auftragsnr. | Anlagennummer | Status | Code Besuchsart), and how many
    leading pages hold it.
    """
    cover, n_cover = {}, 0
    for i, text in enumerate(texts):
        rows = [cols for cols in (re.split(r' {2,}', line.strip()) for line in text.splitlines())
                if len(cols) >= 4 and _ORDER_NO.fullmatch(cols[0]) and cols[1].isdigit()]
        if not rows or i in labelled:
            break
        for cols in rows:
            cover[cols[0]] = {"asset": cols[1].lstrip("0"), "status": cols[2], "visit_type": cols[3]}
        n_cover = i + 1
    return cover, n_cover


def collective_parts(pdf_path: str, di: dict | None = None) -> dict:
    """
    Split plan for a KONE PDF:
      {"parts":  [{"order", "asset", "visit_type", "street", "pages"}, ...],
       "cover":  [page indices of the cover],
       "problem": None or why the PDF is not cut,
       "not_in_pdf": cover orders without report pages, e.g. "9B0000000001 (Störung, Geplant)",
       "ocr_pages": [page indices read by OCR]}
    "parts" is empty for a single report and whenever there is a problem —
    better not to cut than to cut wrong. `di` = Azure Document Intelligence
    credentials {"endpoint", "key"} for pages without a text layer.
    """
    reader = PdfReader(pdf_path)
    texts  = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    plan   = {"parts": [], "cover": [], "problem": None, "not_in_pdf": [], "ocr_pages": []}

    no_text = [i for i, t in enumerate(texts) if not t.strip()]
    unread = []   # pages without text that OCR could not cover
    if no_text and di:
        try:
            for i, text in _ocr_layout_texts(pdf_path, no_text, di).items():
                texts[i] = text
            plan["ocr_pages"] = no_text
        except Exception as exc:
            print(f"[OCR] {os.path.basename(pdf_path)}: {type(exc).__name__}: {exc}", flush=True)
            unread = no_text
    elif no_text:
        unread = no_text

    def refuse(reason: str) -> dict:
        plan["parts"], plan["cover"], plan["problem"] = [], [], reason
        return plan

    labelled = {}   # page index -> order number named by a report label
    for i, text in enumerate(texts):
        m = _ORDER_START.search(text)
        if m:
            labelled[i] = m.group(1)
    cover, n_cover = _cover_table(texts, labelled)

    if cover:
        # Witness 1: the first page after the cover with a line naming the order and its lift
        starts = []
        for o, c in cover.items():
            hit = next((i for i in range(n_cover, len(texts)) if c["asset"] and any(
                _mentions(line, o) and _mentions(line, c["asset"]) for line in texts[i].splitlines())), None)
            if hit is not None:
                starts.append((hit, o))
        starts.sort()
    else:
        # No cover: the labels are the only anchor; a details block repeated on the
        # next page of the same report is not a new report
        starts = []
        for i, o in sorted(labelled.items()):
            if not starts or starts[-1][1] != o:
                starts.append((i, o))
    orders = [o for _, o in starts]
    first  = starts[0][0] if starts else len(texts)
    plan["not_in_pdf"] = [f'{o} ({c["visit_type"]}, {c["status"]})' for o, c in cover.items() if o not in orders]

    owner = {}   # page index -> order of the report it belongs to
    for n, (start, order) in enumerate(starts):
        for i in range(start, starts[n + 1][0] if n + 1 < len(starts) else len(texts)):
            owner[i] = order

    if len({i for i, _ in starts}) != len(starts):
        return refuse("two orders start on the same page")
    if len(set(orders)) != len(orders):
        return refuse("an order starts two reports")
    # Unread pages can hide a report — only a cover listing exactly the one report
    # found proves a single report
    if unread and not (len(cover) == 1 and len(starts) == 1):
        return refuse(f"page(s) {', '.join(str(i + 1) for i in unread)} have no text layer and "
                      + ("OCR failed" if di else "OCR (Azure Document Intelligence) is not configured"))
    # Witness 3: a recognised label must name the report its page belongs to
    for i, o in labelled.items():
        if owner.get(i) != o:
            return refuse(f"page {i + 1} is labelled as order {o}, the order numbers disagree")
    # Every finished order on the cover must have its report in the PDF
    for o, c in cover.items():
        if o not in orders and c["status"].lower() not in _OPEN_STATUSES:
            return refuse(f'order {o} is "{c["status"]}" on the cover, but its report was not found')

    # Witness 2: the merge boundaries in the PDF's fonts must be exactly the report
    # starts — counted at each report's first page with fonts, since an OCR'd
    # (image-only) page has none; the PDF's very first font page is no boundary
    page_tags = [_font_tags(page) for page in reader.pages]
    bounds = _merge_boundaries(page_tags)
    if bounds:
        font_pages = [i for i, tags in enumerate(page_tags) if tags]
        expected = []
        for _, order in starts:
            fp = next((i for i, o in sorted(owner.items()) if o == order and page_tags[i]), None)
            if fp is not None and fp != font_pages[0]:
                expected.append(fp)
        if bounds != sorted(expected):
            return refuse("the PDF's structure shows reports starting on page(s) "
                          f"{', '.join(str(i + 1) for i in bounds)}, the order numbers on "
                          f"{', '.join(str(i + 1) for i in sorted(expected)) or 'none'}")
    else:
        # No font witness: fall back to stricter text checks — no page between the
        # cover and the first report, no order number outside its own report
        if cover and first > n_cover and len(starts) >= 2:
            pages = f"page {first}" if first == n_cover + 1 else f"pages {n_cover + 1}–{first}"
            return refuse(f"{pages} belong(s) to no report")
        for i in range(first, len(texts)):
            for o in set(cover) | set(orders):
                if owner.get(i) != o and _mentions(texts[i], o):
                    return refuse(f"order {o} appears on page {i + 1}, outside its own report")

    if len(starts) < 2:
        return plan   # a single report

    plan["cover"] = list(range(first))
    for start, order in starts:
        head = texts[start]
        c = cover.get(order, {})
        plan["parts"].append({
            "order":      order,
            "asset":      c.get("asset") or _layout_field(head, _LABELS["asset"]),
            "visit_type": c.get("visit_type") or _layout_field(head, _LABELS["visit_type"]),
            "street":     _layout_field(head, _LABELS["street"]),
            "pages":      [i for i, o in owner.items() if o == order],
        })
    return plan


def _content_key(reader, pages: list) -> str:
    """Fingerprint of what the pages show (text + images), the same for a re-generated copy."""
    h = hashlib.sha1()
    for i in pages:
        page = reader.pages[i]
        h.update((page.extract_text() or "").encode("utf-8"))
        try:
            xobjects = page["/Resources"].get_object().get("/XObject") or {}
            for ref in xobjects.get_object().values():
                obj = ref.get_object()
                if obj.get("/Subtype") == "/Image":
                    h.update(hashlib.sha1(obj.get_data()).digest())
        except Exception:
            pass
    return h.hexdigest()


def split_collective(pdf_path: str, plan: dict, out_dir: str, address: str, seen: dict) -> list:
    """
    Write one PDF per report of `plan` plus one for the cover into out_dir and
    return [(file name, kind, written), ...] with kind "report" or "cover".
    Reports are named like KONE's single reports plus the order number:
      "Musterstraße 3_ServiceReport_12345678_Planned Maintenance_000000000001.pdf"
    using the lift's own street from the report (`address` is the fallback);
    the cover becomes "<address>_Site Visit Service Report.pdf".
    `seen` maps content fingerprints to file names across the whole run: a
    report that another email already delivered is written once
    (written = False); a different version under the same name gets "_2".
    All pieces are built before the first file is written, so a PDF that fails
    leaves nothing half-split behind.
    """
    reader = PdfReader(pdf_path)
    probe  = PdfReader(pdf_path)   # reading text/images caches data on the page objects and
                                   # would alter the copied pages, so fingerprints use their own reader
    pieces = [("report", p["pages"], [p["street"] or address, "ServiceReport",
                                      p["asset"], p["visit_type"], p["order"]])
              for p in plan["parts"]]
    if plan["cover"]:
        pieces.append(("cover", plan["cover"], [address, "Site Visit Service Report"]))

    built = []   # (kind, fingerprint, file stem, pdf bytes or None for a repeat)
    for kind, pages, bits in pieces:
        key = _content_key(probe, pages)
        if key in seen or any(key == b[1] for b in built):
            built.append((kind, key, None, None))
            continue
        writer = PdfWriter()
        for i in pages:
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        built.append((kind, key, safe_name("_".join(b for b in bits if b))[:150].rstrip(" ."), buf.getvalue()))

    names = []
    for kind, key, stem, data in built:
        if data is None:
            names.append((seen[key], kind, False))
            continue
        path, n = Path(out_dir) / f"{stem}.pdf", 2
        while path.exists():
            path, n = Path(out_dir) / f"{stem}_{n}.pdf", n + 1
        path.write_bytes(data)
        seen[key] = path.name
        names.append((path.name, kind, True))
    return names


async def _check_collective(save_path: str, di: dict | None) -> tuple:
    """
    (plan, read_error) for a downloaded PDF. The check runs in a worker thread so
    a slow PDF (or OCR call) cannot stall the browser, and is given up after a
    timeout. A PDF that cannot be checked is reported, never silently treated as
    a single report.
    """
    try:
        plan = await asyncio.wait_for(asyncio.to_thread(collective_parts, save_path, di), timeout=180)
        return plan, None
    except Exception as exc:
        reason = "check timed out" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
        print(f"[collective check] {os.path.basename(save_path)}: {reason}", flush=True)
        return None, reason


async def _download_job(page, address: str, url: str, temp_dir: str, di: dict | None = None):
    """
    Navigate to a KONE Jobdetails page and save all PDFs behind its download icon.
    Returns (list_of_saved_file_info, status_string).

    Since Oct 2026 the portal's own download button no longer works: its script
    builds a `data:` link, which Salesforce Lightning Web Security blocks, so the
    browser never starts a download. The PDFs are therefore read straight from
    the getServiceReport response the click triggers. A real browser download
    is still accepted as a fallback in case KONE changes the page.
    """
    from playwright.async_api import TimeoutError as PWTimeout

    collected = []

    def _on_dl(dl):
        collected.append(dl)

    page.on("download", _on_dl)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        if "redirect_error" in page.url:
            return [], "expired"

        # Wait for the download icon itself — it is the same image on the German
        # ("Den Report runterladen") and Dutch ("Download het servicerapport")
        # portals, so this is language-independent.
        try:
            await page.wait_for_selector("img[src*='KONE_download']", timeout=25_000)
        except PWTimeout:
            return [], "no_button"

        await page.wait_for_timeout(1_500)
        async with page.expect_response(
            lambda r: "getServiceReport" in r.url, timeout=30_000
        ) as resp_info:
            await page.click("img[src*='KONE_download']", timeout=10_000)
        response = await resp_info.value
        reports = _reports_from_aura(await response.text())

        files = []   # (suggested_name, save(path) — sync or async)
        if reports:
            for name, pdf in reports:
                files.append((name, lambda path, pdf=pdf: Path(path).write_bytes(pdf)))
        else:
            # Fallback: a browser download, should the button ever work again.
            # Poll until no new download arrives for 3 consecutive seconds (max 10 s).
            deadline, poll = 10, 0.5
            stable_for = last_count = 0
            for _ in range(int(deadline / poll)):
                await asyncio.sleep(poll)
                if len(collected) > last_count:
                    last_count = len(collected)
                    stable_for = 0
                else:
                    stable_for += poll
                    if stable_for >= 3 and last_count > 0:
                        break
            for i, dl in enumerate(collected):
                files.append((dl.suggested_filename or f"report_{i+1}.pdf", dl.save_as))

        if not files:
            return [], "no_download"

        saved = []
        for i, (suggested, save) in enumerate(files):
            suggested = safe_name(suggested)
            if len(files) > 1:
                stem, ext = suggested.rsplit(".", 1) if "." in suggested else (suggested, "pdf")
                suggested = f"{stem}_{i+1}.{ext}"
            fname = f"{safe_name(address)}_{suggested}"
            # Two emails can give the same name (same address once "WG_"/"FW_" is
            # stripped, same KONE file name): never let one overwrite the other
            stem, ext = os.path.splitext(fname)
            n = 2
            while os.path.exists(os.path.join(temp_dir, fname)):
                fname, n = f"{stem}_{n}{ext}", n + 1
            save_path = os.path.join(temp_dir, fname)
            result = save(save_path)
            if asyncio.iscoroutine(result):
                await result
            size_kb = os.path.getsize(save_path) // 1024
            plan, read_error = await _check_collective(save_path, di)
            saved.append({"filename": fname, "size_kb": size_kb, "plan": plan, "read_error": read_error})

        return saved, "ok"

    except PWTimeout:
        return [], "timeout"
    except Exception:
        return [], "error"
    finally:
        page.remove_listener("download", _on_dl)


async def _run_async(jobs, temp_dir: str, state: dict, di: dict | None = None):
    """Process all jobs sequentially inside a single Playwright browser session."""
    from playwright.async_api import async_playwright

    state["total"] = len(jobs)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(accept_downloads=True)
        page = await ctx.new_page()

        for i, (address, url, filename) in enumerate(jobs):
            state["current"] = i + 1
            state["status"] = f"[{i+1}/{len(jobs)}] {address}"

            files, status = await _download_job(page, address, url, temp_dir, di)
            state["results"].append({
                "msg_file": filename,
                "address": address,
                "status": status,
                "files": files,
                "count": len(files),
            })

            await asyncio.sleep(0.5)

        await ctx.close()
        await browser.close()

    state["done"] = True


def _thread_runner(jobs, temp_dir: str, state: dict, di: dict | None = None):
    """Entry point for the background thread — owns its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async(jobs, temp_dir, state, di))
    except Exception as exc:
        state["error"] = str(exc)
        state["done"] = True
    finally:
        loop.close()

# ── UI helpers ────────────────────────────────────────────────────────────────

def _results_df(results: list) -> pd.DataFrame:
    rows = []
    for r in results:
        label, _ = STATUS_META.get(r["status"], ("? Unknown", "badge-skip"))
        files_str = (
            ", ".join(f"{f['filename']} ({f['size_kb']} KB)" for f in r["files"])
            if r["files"] else "—"
        )
        rows.append({
            "MSG File": r["msg_file"],
            "Status":   label,
            "PDFs":     r["count"],
            "Files":    files_str,
        })
    return pd.DataFrame(rows)


def _di_credentials() -> dict | None:
    """
    Azure Document Intelligence endpoint + key for OCR of pages without a text
    layer: Streamlit secrets AZURE_DI_ENDPOINT / AZURE_DI_KEY (same names as the
    ZÜS PDF Renamer), else environment variables of the same names. None = no OCR.
    """
    creds = {}
    for key, name in (("endpoint", "AZURE_DI_ENDPOINT"), ("key", "AZURE_DI_KEY")):
        value = ""
        try:
            value = st.secrets.get(name, "")   # raises when there is no secrets file at all
        except Exception:
            pass
        creds[key] = value or os.environ.get(name, "")
    return creds if all(creds.values()) else None


def _build_zip(temp_dir: str) -> io.BytesIO | None:
    pdfs = list(Path(temp_dir).glob("*.pdf"))
    if not pdfs:
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in pdfs:
            zf.write(p, p.name)
    buf.seek(0)
    return buf

# ── Session state init ────────────────────────────────────────────────────────

_STATE_DEFAULTS = {
    "processing":   False,
    "done":         False,
    "shared_state": None,
    "temp_dir":     None,
    "jobs":         None,
    "split_done":   False,
    "split_summary": None,
}


def _init_state():
    for k, v in _STATE_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ── Main app ──────────────────────────────────────────────────────────────────

def main():
    chromium_ok, chromium_log = _install_chromium()
    _init_state()

    # ── Hero ──────────────────────────────────────────────────────────────────
    st.markdown("""
<div class="hero-section">
    <h1>📥 KONE Service Report Downloader</h1>
</div>
""", unsafe_allow_html=True)

    if not chromium_ok:
        st.error("⚠️ Chromium browser could not be initialised. The app cannot process files.")
        with st.expander("Technical details"):
            st.code(chromium_log)
        return

    # ── DONE state ────────────────────────────────────────────────────────────
    if st.session_state.done:
        shared   = st.session_state.shared_state
        results  = shared["results"]
        pdf_count  = sum(r["count"] for r in results)
        ok_count   = sum(1 for r in results if r["status"] == "ok")
        skip_count = sum(1 for r in results if r["status"] in ("expired", "no_button", "no_download"))
        fail_count = sum(1 for r in results if r["status"] in ("timeout", "error"))

        st.success("✅ Processing complete!")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Processed",    len(results))
        c2.metric("✓ PDFs saved", pdf_count)
        c3.metric("⊘ Skipped",   skip_count)
        c4.metric("✗ Failed",    fail_count)

        st.markdown("### Results")
        st.dataframe(_results_df(results), width="stretch", hide_index=True)

        zip_buf = _build_zip(st.session_state.temp_dir)
        if zip_buf:
            st.markdown("---")
            st.download_button(
                label=f"⬇️  Download all PDFs — {pdf_count} file(s) in ZIP",
                data=zip_buf,
                file_name="kone_reports.zip",
                mime="application/zip",
                on_click="ignore",
            )
        else:
            st.markdown('<div class="warn-box">⚠️ No PDF files were downloaded successfully.</div>',
                        unsafe_allow_html=True)

        # ── Optional post step: split collective reports ──────────────────────
        files      = [(r, f) for r in results for f in r["files"]]
        collective = [(r, f) for r, f in files if f.get("plan") and f["plan"]["parts"]]
        unclear    = [f for _, f in files if f.get("plan") and f["plan"]["problem"]]
        unchecked  = [f for _, f in files if f.get("read_error")]
        if collective or unclear or unchecked:
            st.markdown("---")
            st.markdown("### ✂️ Collective Reports")
            if collective:
                orders = {p["order"] for _, f in collective for p in f["plan"]["parts"]}
                n_ocr = sum(len(f["plan"]["ocr_pages"]) for _, f in collective)
                st.markdown(
                    f'<div class="info-box">{len(collective)} PDF(s) are collective reports '
                    f'holding <strong>{len(orders)} individual reports</strong>. Splitting gives one '
                    f'PDF per order (Auftragsnr.) plus each visit\'s cover page, which can hold the '
                    f'customer signature. The collective PDFs above stay available.'
                    + (f' {n_ocr} page(s) without a text layer were read by OCR.' if n_ocr else '')
                    + '</div>',
                    unsafe_allow_html=True,
                )
                rows = [{
                    "Collective PDF":                 f["filename"],
                    "Reports":                        len(f["plan"]["parts"]),
                    "Orders (Auftragsnr.)":           ", ".join(p["order"] for p in f["plan"]["parts"]),
                    "Listed on cover, no report yet": ", ".join(f["plan"]["not_in_pdf"]) or "—",
                } for _, f in collective]
                if not any(f["plan"]["not_in_pdf"] for _, f in collective):
                    for row in rows:
                        del row["Listed on cover, no report yet"]
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            if unclear:
                st.markdown(
                    f'<div class="warn-box">⚠️ {len(unclear)} PDF(s) could not be split safely and stay '
                    f'whole — please split them by hand: '
                    + "; ".join(f'{f["filename"]} ({f["plan"]["problem"]})' for f in unclear)
                    + '</div>',
                    unsafe_allow_html=True,
                )
            if unchecked:
                st.markdown(
                    f'<div class="warn-box">⚠️ {len(unchecked)} PDF(s) could not be checked for '
                    f'collective reports and stay whole: '
                    + "; ".join(f'{f["filename"]} ({f["read_error"]})' for f in unchecked)
                    + '</div>',
                    unsafe_allow_html=True,
                )

            split_dir = os.path.join(st.session_state.temp_dir, "split")
            if collective and not st.session_state.split_done:
                if st.button("✂️ Split Collective Reports"):
                    os.makedirs(split_dir, exist_ok=True)
                    # The split ZIP is the full set: collective PDFs cut into their
                    # reports + cover, every other PDF copied as it is.
                    summary = {"reports": 0, "covers": 0, "whole": 0, "repeats": 0,
                               "collective": len(collective), "failed": []}
                    seen = {}
                    for r, f in files:
                        src = os.path.join(st.session_state.temp_dir, f["filename"])
                        if f.get("plan") and f["plan"]["parts"]:
                            try:
                                for _, kind, written in split_collective(
                                        src, f["plan"], split_dir, r["address"], seen):
                                    summary[("reports" if kind == "report" else "covers")
                                            if written else "repeats"] += 1
                                continue
                            except Exception as exc:
                                print(f"[split] {f['filename']}: {type(exc).__name__}: {exc}", flush=True)
                                summary["failed"].append(f'{f["filename"]} ({type(exc).__name__}: {exc})')
                        stem, ext = os.path.splitext(f["filename"])
                        dst, n = Path(split_dir) / f["filename"], 2
                        while dst.exists():
                            dst, n = Path(split_dir) / f"{stem}_{n}{ext}", n + 1
                        shutil.copy2(src, dst)
                        summary["whole"] += 1
                    st.session_state.split_summary = summary
                    st.session_state.split_done = True
                    st.rerun()

            if st.session_state.split_done:
                s = st.session_state.split_summary
                split_count = len(list(Path(split_dir).glob("*.pdf")))
                st.success(
                    f"✅ Split done: {s['reports']} report(s) cut from {s['collective'] - len(s['failed'])} "
                    f"collective PDF(s), {s['covers']} cover page(s) and {s['whole']} PDF(s) copied "
                    f"unchanged — {split_count} file(s). This ZIP is the complete set, so you only "
                    f"need this one."
                    + (f" {s['repeats']} identical copy(ies) from repeated emails were saved once."
                       if s["repeats"] else "")
                )
                if s["failed"]:
                    st.markdown(
                        f'<div class="warn-box">⚠️ {len(s["failed"])} PDF(s) could not be split and are in '
                        f'the ZIP whole: ' + "; ".join(s["failed"]) + '</div>',
                        unsafe_allow_html=True,
                    )
                st.download_button(
                    label=f"⬇️  Download individual reports — {split_count} file(s) in ZIP",
                    data=_build_zip(split_dir),
                    file_name="kone_reports_split.zip",
                    mime="application/zip",
                    key="download_split",
                    on_click="ignore",
                )

        st.markdown("---")
        if st.button("🔄 Start New Batch"):
            if st.session_state.temp_dir and os.path.exists(st.session_state.temp_dir):
                shutil.rmtree(st.session_state.temp_dir, ignore_errors=True)
            for k, v in _STATE_DEFAULTS.items():
                st.session_state[k] = v
            st.rerun()
        return

    # ── PROCESSING state ──────────────────────────────────────────────────────
    if st.session_state.processing:
        shared = st.session_state.shared_state
        total  = shared["total"] or len(st.session_state.jobs)

        st.markdown("### ⏳ Downloading Reports…")
        st.markdown(
            '<div class="info-box">🔄 Processing is running. '
            'Please <strong>keep this tab open</strong> until complete.</div>',
            unsafe_allow_html=True,
        )

        progress_bar      = st.progress(0.0)
        status_text       = st.empty()
        results_container = st.empty()

        while not shared["done"]:
            current = shared["current"]
            pct     = current / total if total > 0 else 0.0
            progress_bar.progress(min(pct, 1.0))
            status_text.markdown(f"**{shared['status']}**")

            if shared["results"]:
                with results_container.container():
                    st.dataframe(_results_df(shared["results"]),
                                 width="stretch", hide_index=True)
            time.sleep(0.5)

        progress_bar.progress(1.0)
        status_text.markdown("**✅ Done!**")

        if shared["results"]:
            with results_container.container():
                st.dataframe(_results_df(shared["results"]),
                             width="stretch", hide_index=True)

        if shared.get("error"):
            st.error(f"An unexpected error occurred: {shared['error']}")

        st.session_state.done       = True
        st.session_state.processing = False
        st.rerun()
        return

    # ── UPLOAD state ─────────────────────────────────────────────────────────
    st.markdown("### 📂 Upload .msg Files")

    uploaded = st.file_uploader(
        "Drag & drop your KONE service-update emails here (max 50)",
        type=["msg"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )

    if not uploaded:
        return

    if len(uploaded) > MAX_FILES:
        st.markdown(
            f'<div class="warn-box">⚠️ You uploaded {len(uploaded)} files. '
            f'Only the first {MAX_FILES} will be processed.</div>',
            unsafe_allow_html=True,
        )
        uploaded = uploaded[:MAX_FILES]

    # ── URL extraction preview ────────────────────────────────────────────────
    st.markdown("### 📋 File Preview")

    jobs         = []
    preview_rows = []

    for uf in uploaded:
        address = address_from_filename(uf.name)
        url     = extract_url(uf.getvalue())
        jobs.append((address, url, uf.name))
        preview_rows.append({
            "MSG File":  uf.name,
            "URL Found": "✓ Yes" if url else "✗ No",
        })

    st.dataframe(pd.DataFrame(preview_rows), width="stretch", hide_index=True)

    valid_jobs    = [(a, u, f) for a, u, f in jobs if u is not None]
    invalid_count = len(jobs) - len(valid_jobs)

    c1, c2, c3 = st.columns(3)
    c1.metric("Files Uploaded",  len(uploaded))
    c2.metric("URLs Extracted",  len(valid_jobs))
    c3.metric("No URL (skipped)", invalid_count)

    if invalid_count > 0:
        st.markdown(
            f'<div class="warn-box">⚠️ {invalid_count} file(s) contain no KONE report link '
            f'(Jobdetails / Statusinformatie) and will be skipped.</div>',
            unsafe_allow_html=True,
        )

    if not valid_jobs:
        st.error("No valid URLs found. Make sure you're uploading KONE service emails "
                 "(German 'Jobdetails' or Dutch 'Statusinformatie' report link).")
        return

    st.markdown("---")
    start = st.button("🚀 Start Download", type="primary")

    if start:
        temp_dir = tempfile.mkdtemp()
        shared_state = {
            "current": 0, "total": 0,
            "status": "Starting…",
            "results": [], "done": False, "error": None,
        }
        st.session_state.processing   = True
        st.session_state.done         = False
        st.session_state.shared_state = shared_state
        st.session_state.temp_dir     = temp_dir
        st.session_state.jobs         = valid_jobs

        threading.Thread(
            target=_thread_runner,
            args=(valid_jobs, temp_dir, shared_state, _di_credentials()),
            daemon=True,
        ).start()
        st.rerun()


main()
