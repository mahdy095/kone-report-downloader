"""
KONE Service Report Downloader — Streamlit Web App
Upload .msg notification emails → download all PDF reports → get a ZIP file.
"""

import re
import asyncio
import threading
import tempfile
import io
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
    return re.sub(r'[<>:"/\\|?*]', "-", text)


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


async def _download_job(page, address: str, url: str, temp_dir: str):
    """
    Navigate to a KONE Jobdetails page and download all triggered PDFs.
    Returns (list_of_saved_file_info, status_string).
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
        await page.click("img[src*='KONE_download']", timeout=10_000)

        # Poll until no new download arrives for 3 consecutive seconds (max 20 s total).
        deadline, poll = 20, 0.5
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

        if not collected:
            return [], "no_download"

        saved = []
        for i, dl in enumerate(collected):
            suggested = dl.suggested_filename or f"report_{i+1}.pdf"
            if len(collected) > 1:
                stem, ext = suggested.rsplit(".", 1) if "." in suggested else (suggested, "pdf")
                suggested = f"{stem}_{i+1}.{ext}"
            fname = f"{safe_name(address)}_{suggested}"
            save_path = os.path.join(temp_dir, fname)
            await dl.save_as(save_path)
            size_kb = os.path.getsize(save_path) // 1024
            saved.append({"filename": fname, "size_kb": size_kb})

        return saved, "ok"

    except PWTimeout:
        return [], "timeout"
    except Exception:
        return [], "error"
    finally:
        page.remove_listener("download", _on_dl)


async def _run_async(jobs, temp_dir: str, state: dict):
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

            files, status = await _download_job(page, address, url, temp_dir)
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


def _thread_runner(jobs, temp_dir: str, state: dict):
    """Entry point for the background thread — owns its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async(jobs, temp_dir, state))
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

def _init_state():
    defaults = {
        "processing":   False,
        "done":         False,
        "shared_state": None,
        "temp_dir":     None,
        "jobs":         None,
    }
    for k, v in defaults.items():
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
        st.dataframe(_results_df(results), use_container_width=True, hide_index=True)

        zip_buf = _build_zip(st.session_state.temp_dir)
        if zip_buf:
            st.markdown("---")
            st.download_button(
                label=f"⬇️  Download all PDFs — {pdf_count} file(s) in ZIP",
                data=zip_buf,
                file_name="kone_reports.zip",
                mime="application/zip",
            )
        else:
            st.markdown('<div class="warn-box">⚠️ No PDF files were downloaded successfully.</div>',
                        unsafe_allow_html=True)

        st.markdown("---")
        if st.button("🔄 Start New Batch"):
            if st.session_state.temp_dir and os.path.exists(st.session_state.temp_dir):
                shutil.rmtree(st.session_state.temp_dir, ignore_errors=True)
            for k in ("processing", "done", "shared_state", "temp_dir", "jobs"):
                st.session_state[k] = {"processing": False, "done": False,
                                        "shared_state": None, "temp_dir": None,
                                        "jobs": None}[k]
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
                                 use_container_width=True, hide_index=True)
            time.sleep(0.5)

        progress_bar.progress(1.0)
        status_text.markdown("**✅ Done!**")

        if shared["results"]:
            with results_container.container():
                st.dataframe(_results_df(shared["results"]),
                             use_container_width=True, hide_index=True)

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

    st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

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
            args=(valid_jobs, temp_dir, shared_state),
            daemon=True,
        ).start()
        st.rerun()


main()
