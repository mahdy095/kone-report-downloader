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
@st.cache_resource(show_spinner=False)
def _install_chromium():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=180,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return False, str(exc)

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

def extract_url(msg_bytes: bytes) -> str | None:
    """Extract the Jobdetails URL from raw .msg bytes."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
            tmp.write(msg_bytes)
            tmp_path = tmp.name
        msg = _extract_msg.Message(tmp_path)
        html = (msg.htmlBody or b"").decode("utf-8", errors="ignore")
        match = re.search(
            r'<a\s+href=["\']([^"\']+)["\'][^>]*>\s*Jobdetails\s*</a>',
            html, re.IGNORECASE,
        )
        return match.group(1) if match else None
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def safe_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "-", text)


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

        try:
            await page.wait_for_selector("text=Den Report runterladen", timeout=25_000)
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
            "MSG File":    r["msg_file"],
            "Address":     r["address"],
            "Status":      label,
            "PDFs":        r["count"],
            "Files":       files_str,
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
        address = uf.name.replace("Ihr Service-Update _ ", "").replace(".msg", "").strip()
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
            f'<div class="warn-box">⚠️ {invalid_count} file(s) contain no Jobdetails link '
            f'and will be skipped.</div>',
            unsafe_allow_html=True,
        )

    if not valid_jobs:
        st.error("No valid URLs found. Make sure you're uploading KONE service-update emails with a Jobdetails link.")
        return

    st.markdown("---")
    col_btn, col_est = st.columns([1, 3])
    with col_btn:
        start = st.button("🚀 Start Download", type="primary")
    with col_est:
        est_min = max(1, len(valid_jobs) * 30 // 60)
        st.markdown(
            f"<br>Estimated time: <strong>~{est_min}–{est_min * 2} minutes</strong> "
            f"for {len(valid_jobs)} file(s)",
            unsafe_allow_html=True,
        )

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
