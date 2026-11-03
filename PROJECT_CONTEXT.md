# KONE Service Report Downloader — Project Context

## What this app does

A publicly deployed Streamlit web app that automates downloading PDF service reports from the KONE customer portal. Users upload `.msg` email files (Outlook), the app extracts the portal URL from each email, launches a headless Chromium browser (Playwright) server-side, navigates to each URL, clicks the download button, and bundles all collected PDFs into a single ZIP file for download. No installation required on the user's device — everything runs on Streamlit Cloud.

**Live URL:** see `README.md`
**GitHub:** https://github.com/mahdy095/kone-report-downloader

---

## Repository structure

```
KONE_Report_Downloader/
├── app.py                    # Single-file Streamlit app (~1,000 lines)
├── requirements.txt          # Python dependencies
├── packages.txt.disabled     # Old apt list, disabled (see "packages.txt is DISABLED" below)
├── runtime.txt               # Python 3.11
├── README.md                 # Short overview + how to run locally
├── .streamlit/
│   └── config.toml           # maxUploadSize, theme colours
└── .gitignore
```

---

## Deployment environment

- **Platform:** Streamlit Cloud (free tier)
- **OS:** Debian Trixie (NOT Ubuntu — important for package names)
- **Python:** 3.11 (pinned via `runtime.txt`)
- **Auto-deploy:** pushes to `main` branch trigger redeployment automatically
- **No secrets needed** — app has no API key

### packages.txt is DISABLED (2026-09-08) — apt is broken on Community Cloud

Streamlit Community Cloud aborts the deployment of any repo that contains a
`packages.txt`:

```
E: Release file for http://deb.debian.org/debian-security/dists/bullseye-security/InRelease is expired
   installer returned a non-zero exit code
   Error during processing dependencies!
```

The platform image still carries `bullseye-security` (and `packages.microsoft.com/debian/11`)
in its sources list. Debian bullseye LTS ended 2026-08-31, so that Release file is
permanently expired, `apt-get update` exits non-zero, and Streamlit treats the whole
dependency step as failed. Nothing in the repo can fix apt's sources — restarting the
app does not help either.

Fix: the file was renamed to `packages.txt.disabled` (apt is then never invoked) and
`_install_chromium()` in `app.py` installs Chromium's shared libraries at runtime
**without root**:

1. `playwright install chromium` as before.
2. `ldd` the Chromium/headless-shell binary — if nothing is missing, stop here (the
   base image may already carry the libs, so this costs nothing).
3. Otherwise `apt-cache depends --recurse` for the dependency closure and
   `apt-get download` for the `.deb` files — both read-only apt operations that a
   non-root user may run, and neither touches `apt-get update`.
4. `dpkg-deb -x` each archive into `~/.cache/chromium-sysdeps`, then expose
   `usr/lib/x86_64-linux-gnu` (etc.) through `LD_LIBRARY_PATH` before Playwright
   starts. Playwright inherits `os.environ`, so the background thread sees it.
5. `ldd` again; if libraries are still missing the app shows the full log in the
   "Technical details" expander instead of failing silently.

If Streamlit ever repairs the image, the old file can be restored verbatim — the
runtime path then becomes a no-op because step 2 finds nothing missing.

### packages.txt, historical (system libs for Chromium on Debian Trixie)

Critical lesson: Streamlit Cloud runs Debian Trixie which uses `*t64` renamed packages. Do **not** include `libglib2.0-0` — it conflicts with the Trixie `libglib2.0-0t64` variant pulled in by `libatk1.0-0`. The working set:

```
libnss3
libnspr4
libatk1.0-0
libatk-bridge2.0-0
libcups2
libdrm2
libxkbcommon0
libgbm1
libasound2
libx11-xcb1
libxcomposite1
libxdamage1
libxfixes3
libxrandr2
```

### requirements.txt

```
streamlit>=1.50.0                     # width="stretch", download_button(on_click="ignore")
playwright>=1.40.0
extract-msg>=0.28.0
pandas>=2.0.0
openpyxl>=3.1.0
pypdf==6.16.2                         # collective-report split; pinned: detection reads its layout-mode text spacing
azure-ai-documentintelligence==1.0.2  # OCR of pages without a text layer (same version as the ZÜS PDF Renamer)
```

### .streamlit/config.toml

```toml
[server]
maxUploadSize = 200

[theme]
primaryColor = "#6366f1"
backgroundColor = "#f5f7fa"
secondaryBackgroundColor = "#ffffff"
textColor = "#1f2937"
font = "sans serif"
```

---

## Playwright on Streamlit Cloud

Playwright's Chromium browser binary must be downloaded at runtime (it cannot be committed to git). The pattern that works:

```python
@st.cache_resource(show_spinner=False)
def _install_chromium():
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True, timeout=180,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()
```

`@st.cache_resource` ensures this runs only once per deployment (not on every page load). This call downloads the browser binary only; the system-level libs are installed at runtime (see "packages.txt is DISABLED").

**Launch flags required on Linux:**
```python
browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
```

---

## asyncio + threading pattern (critical for Streamlit)

Streamlit's main thread cannot run `asyncio.run()` directly. The pattern used:

```python
def _thread_runner(jobs, temp_dir, state):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_async(jobs, temp_dir, state))
    except Exception as exc:
        state["error"] = str(exc)
        state["done"] = True
    finally:
        loop.close()

# Start the thread from the Streamlit main thread:
threading.Thread(target=_thread_runner, args=(...), daemon=True).start()
st.rerun()
```

The background thread writes progress into a shared `state` dict stored in `st.session_state`. The Streamlit main thread then polls it in a blocking `while not state["done"]: time.sleep(0.5)` loop, updating `st.empty()` placeholder containers each iteration. This keeps the websocket alive and streams live updates to the browser.

---

## App flow / UI states

The app has three mutually exclusive UI states, controlled by `st.session_state`:

### State 1: UPLOAD (default)
- `st.file_uploader` with `accept_multiple_files=True`, `label_visibility="collapsed"`, `type=["msg"]`
- Max 50 files enforced with a warning
- On upload: extract URL from each .msg, show preview table (`MSG File`, `URL Found`)
- Show 3 metrics: Files Uploaded / URLs Extracted / No URL (skipped)
- `🚀 Start Download` button → transitions to PROCESSING state

### State 2: PROCESSING
- Blocking `while not shared["done"]: time.sleep(0.5)` loop in main thread
- `st.progress()` bar updated each iteration
- `st.empty()` container refreshed with live results dataframe
- On completion → `st.session_state.done = True` → `st.rerun()` → transitions to DONE state

### State 3: DONE
- Summary metrics (Processed / PDFs saved / Skipped / Failed)
- Full results dataframe
- `st.download_button` for the ZIP file
- **✂️ Collective Reports** section — only when a downloaded PDF is a collective report (see below):
  table of collective PDFs, `✂️ Split Collective Reports` button, then a second download button
  `kone_reports_split.zip`. Both ZIPs stay available.
- `🔄 Start New Batch` button clears session state and reruns

### Session state keys
```python
"processing"   # bool — background thread is running
"done"         # bool — processing finished, show results
"shared_state" # dict — live progress shared with background thread
"temp_dir"     # str  — tempfile.mkdtemp() path for downloaded PDFs
"jobs"         # list — [(address, url, filename), ...]
"split_done"   # bool — collective reports were split into temp_dir/split
"split_summary" # dict — counts of the split (reports, covers, whole, repeats, failed) for the result message
```
All defaults live in `_STATE_DEFAULTS`; "Start New Batch" resets every key from it.

### shared_state dict structure
```python
{
    "current": int,    # how many jobs completed so far
    "total":   int,    # total jobs
    "status":  str,    # human-readable current job description
    "results": list,   # list of result dicts (appended as jobs complete)
    "done":    bool,   # True when all jobs finish
    "error":   str|None,
}
```

Each result dict:
```python
{
    "msg_file": str,   # original .msg filename
    "address":  str,   # building address (derived from filename)
    "status":   str,   # "ok" | "expired" | "no_button" | "no_download" | "timeout" | "error"
    "files":    list,  # [{"filename": str, "size_kb": int,
                       #   "plan": dict|None, "read_error": str|None}, ...]  ← see Collective reports
    "count":    int,   # number of PDFs downloaded for this job
}
```

---

## URL extraction from .msg files (3-strategy approach)

KONE service emails come in multiple formats. The `extract_url()` function tries three strategies in order:

```python
def extract_url(msg_bytes: bytes) -> str | None:
    # Write bytes to a temp file, open with extract_msg, read htmlBody

    # Strategy 1: original KONE email — plain text Jobdetails link
    # <a href="https://click.hello.kone.com/...">Jobdetails</a>
    re.search(r'<a\s+href=["\']([^"\']+)["\'][^>]*>\s*Jobdetails\s*</a>', html)

    # Strategy 2: forwarded via Outlook SafeLinks — real URL in `originalsrc` attribute
    # Outlook adds originalsrc="https://click.hello.kone.com/..." to preserve original URL
    re.search(r'originalsrc=["\']([^"\']*click\.hello\.kone\.com[^"\']*)["\']', html)

    # Strategy 3: SafeLinks with Jobdetails text inside a <span>
    # Decode the `url=` parameter from the safelinks.protection.outlook.com href
    re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(?:<[^>]+>)*\s*Jobdetails\s*(?:</[^>]+>)*</a>', html)
    # then: urlparse → parse_qs → unquote the `url` parameter
```

**Key insight for forwarded emails:** When Outlook wraps links in SafeLinks (`eur02.safelinks.protection.outlook.com/?url=...`), it also adds an `originalsrc` attribute on the `<a>` tag containing the original URL. Strategy 2 exploits this — it is faster and more reliable than decoding the SafeLinks URL.

Address extraction from filename:
```python
address = uf.name.replace("Ihr Service-Update _ ", "").replace(".msg", "").strip()
# e.g. "Ihr Service-Update _ Musterstraße 1.msg" → "Musterstraße 1"
# Also handles "WG_ Ihr Service-Update _ ..." (forwarded) via strip()
```

---

## Playwright download logic

Single browser session, single page, sequential jobs:

```python
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context(accept_downloads=True)  # critical
    page = await ctx.new_page()

    for address, url, filename in jobs:
        # 1. Attach download listener BEFORE navigating
        page.on("download", on_download_callback)

        # 2. Navigate to KONE portal URL
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # 3. Check for expired link
        if "redirect_error" in page.url: return "expired"

        # 4. Wait for download button text to appear
        await page.wait_for_selector("text=Den Report runterladen", timeout=25_000)

        # 5. Click the download icon image
        await page.click("img[src*='KONE_download']", timeout=10_000)

        # 6. Poll until no new download for 3 consecutive seconds (max 20s)
        # One page can trigger multiple sequential PDFs

        # 7. Save each collected download to temp_dir
        await dl.save_as(os.path.join(temp_dir, filename))

        # 8. Remove listener before next job
        page.remove_listener("download", on_download_callback)
```

**Why single page:** Reusing the same page across jobs avoids browser context overhead and keeps the session alive. The download listener is attached/removed per job to prevent cross-contamination.

**Multi-PDF handling:** A single KONE job can trigger multiple sequential PDF downloads. The polling loop waits for 3 seconds of silence before moving on. If multiple files: filenames get `_1`, `_2` suffix appended.

**Output filename format:**
```
{safe_name(address)}_{original_suggested_filename}.pdf
# e.g. "Musterstraße 1_ServiceReport_12345678_Planned Maintenance.pdf"
```
Two emails can give the same name (same address after the `WG_`/`FW_` prefix is stripped, same KONE file name):
the later one gets `_2`, `_3` … so no download overwrites another (since 03.11 — before, the second silently
replaced the first in the ZIP).

---

## Collective reports (split step, added 2026-11-03)

Since Oct 2026 KONE often sends **one PDF per site visit** instead of one per order:

| Pages | Content |
|---|---|
| 1 (cover, always German) | "Site Visit Service Report": visit header + table **List of performed works** (`Auftragsnr. \| Anlagennummer \| Status \| Code Besuchsart`) + visit signature |
| then, per order | "Service Report": header + details block with the order number; runs until the next report starts, ends with its own signature block (a trailing blank page may belong to it) |

The report pages come in **two label languages**, also mixed within one batch (seen 03.11):

| Field | German pages | English pages |
|---|---|---|
| order (start of a report) | `Auftragsnr.  000000000001` (block "Arbeitsdetails" / "Auftragsdetails") | `Service Order  000000000002` + `Number` on the next line (block "Work details") |
| street | `Straße` | `Street` |
| lift number | `Vermögenswert` | `Equipment` (first one; the second "Equipment Record Type" = Elevator) |
| reason (fallback for the visit type) | `Auftragsgrund` | `Customer Facing Description` |

The **cover can list orders that have no report pages** in the PDF: status seen `Geplant` (planned Störung),
`Angekommen` (Inspektion in progress). Status `Unterschrift` did have a report. So the status says nothing reliable
about presence. Report types seen: Planned Maintenance (1–3 pages), Repair (1–2), Störung (2), Inspektion. Order
numbers are 12 characters, digits or `9B…`. A cover with only one report = a single report (not split). The same
email can arrive twice → byte-identical PDFs.

**Detection + plan** — `collective_parts(pdf_path, di)` → plan dict `{parts, cover, problem, not_in_pdf, ocr_pages}`,
run right after each PDF is saved, in a worker thread with a 180 s timeout (`_check_collective`, so a slow PDF or OCR
call cannot stall the browser). Stored on the file record as `plan`; a PDF that cannot be read at all gets
`read_error` (logged + own warning in the UI — never silently "single").

The split is **anchored on order numbers and PDF structure, not on label wording**. Three independent witnesses:
1. **Order numbers.** Cover = the leading pages holding the order table, recognised by its **column layout** (pypdf
   layout text, a line splitting on 2+ spaces into ≥ 4 columns: order no. + all-digit Anlagennummer + status +
   Besuchsart). A report starts on the first page after the cover where **one line holds the order number and its lift
   number** (the details-block row `Auftragsnr. … Vermögenswert …` / `Service Order … Equipment …` in every
   language; 41 of 41 real starts). No "first mention" fallback: a mere mention of an order elsewhere is never a start.
2. **Fonts (merge structure).** KONE merges one PDF per report; each brings its own font subset tags
   (`ABCDEF+ArialMT`). A page sharing **no** tag with the previous font-carrying page = first page of a merged PDF
   (`_merge_boundaries`). Matched the cut points in 22 of 22 real PDFs. When present, these boundaries must equal each
   report's first page with fonts (OCR'd image pages carry none), otherwise refused.
3. **Labels** (`_ORDER_START`: `Auftragsnr.` / `Service Order`): a page a label recognises must belong to that order's
   report. Without a cover the labels are the anchor (a details block repeated on the next page of the same order is
   not a new report), checked against witness 2.

Refused (PDF stays whole, reason shown): witnesses disagree; two orders on one start page; one order starting twice;
an order whose status on the cover is not `Geplant`/`Angekommen` (`_OPEN_STATUSES`) but has no report in the PDF;
pages without a text layer that OCR could not read (unless a cover lists exactly the one report found). **Only when
the font witness is missing** (no boundaries in the PDF) the stricter text checks apply: no page between cover and
first report, and no order number outside its own report.

**OCR for pages without a text layer** — Azure Document Intelligence `prebuilt-read` (`_ocr_layout_texts`), same
resource and secret names as the ZÜS PDF Renamer: **`AZURE_DI_ENDPOINT` / `AZURE_DI_KEY`** in Streamlit secrets (or
environment variables). Words are regrouped into visual lines and wide gaps written as several spaces, so OCR text
goes through exactly the same parsing as a text page. Without credentials such a PDF is refused with a clear reason.

Cover orders without report pages → `not_in_pdf` ("9B0000000001 (Störung, Geplant)") → UI column
"Listed on cover, no report yet".

Tests (scratch scripts, 03.11): 22 real PDFs → 28 report files **byte-identical** to the end-to-end-verified version;
all labels switched off → same cuts for all 22; 32-case suite on real PDFs with real fonts (phantom mentions of open
orders, mentions of other orders, conflicting labels, image-only start / continuation / cover / every page with and
without OCR, no cover, unrecognised label without cover, merged singles, cover moved to the end, missing finished
report, re-generated copy with new font tags) and 16 synthetic cases without fonts — all pass.

**Split** — `split_collective(pdf, plan, out_dir, address, seen)`, only when the user clicks the button. Output in
`temp_dir/split/` = the full set: every collective PDF cut into its reports **plus its cover as its own file**
(`<address>_Site Visit Service Report.pdf` — the cover can hold the only customer signature, seen 1 of 11), every
other PDF copied unchanged. Each PDF is split all-or-nothing (pieces built in memory first); a PDF whose split fails
is copied whole and listed in a warning — the rest still splits. Report file name = KONE's single-report pattern +
order number, with the lift's **own street from the report** (the email subject only names one address of the visit):
```
{Straße}_ServiceReport_{Anlagennummer}_{Code Besuchsart}_{Auftragsnr}.pdf
# e.g. "Musterstraße 3_ServiceReport_12345678_Planned Maintenance_000000000001.pdf"
```
Fallbacks without a cover row: lift number / reason text from the report page (then the name shows e.g.
"Regelmäßige Wartung" instead of KONE's code); street → email address. **Duplicates** are recognised by content
(`_content_key`: page text + image data, computed on a separate reader so the copied pages stay byte-identical), so a
report delivered by two emails — even re-generated with new font tags — is written once; a different version of the
same order gets `_2` (order of upload, not "newer").

Verified: 3 German samples + 19 real emails of 02–03.11.2026 end-to-end against the live portal (the samples hold
customer data and are kept outside the repo). **Dutch collective reports: not seen yet** — the number and font
witnesses do not depend on language, so they should split as long as the cover table keeps its column layout; only
the street in the file name falls back to the email address until the Dutch street label is added to `_LABELS`.

---

## CSS / Design system

Follows the same design language as the companion Lift Components app:

| Element | Value |
|---|---|
| Font | Inter (Google Fonts) |
| Hero gradient | `#6366f1` → `#4f46e5` → `#4338ca` |
| Page background | `linear-gradient(135deg, #f5f7fa, #c3cfe2)` |
| Primary button | indigo gradient + `box-shadow` |
| Download button | green gradient (`#10b981` → `#059669`) |
| Card | white, `border-radius: 16px`, `box-shadow` |
| Info box | indigo-tinted, left border `#6366f1` |
| Warn box | yellow-tinted, left border `#f59e0b` |
| Sidebar | **hidden** (`display: none`) — no sidebar in this app |

**Critical CSS fix — Material Icons font:**
```css
* { font-family: 'Inter', sans-serif !important; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }
```
The global `*` override kills the Material Icons font, causing icon glyphs (e.g. `upload`) to render as literal text and overlap with button labels. The second rule restores it for icon spans.

**File uploader label fix:**
```python
st.file_uploader(..., label_visibility="collapsed")
```
Without this, the label text renders above the dropzone AND inside it, appearing doubled.

---

## Known gotchas & lessons learned

1. **Debian Trixie ≠ Ubuntu** — Streamlit Cloud uses Debian Trixie. Package names differ from Ubuntu docs. `libglib2.0-0` causes a dependency conflict; omit it from `packages.txt`.

2. **`playwright install chromium` must run at Python startup** — the pip package installs the Python wrapper only. The browser binary must be downloaded separately via subprocess, cached with `@st.cache_resource`.

3. **`asyncio.new_event_loop()` in a thread** — on Linux (unlike Windows), `asyncio.new_event_loop()` works directly. No `ProactorEventLoop` needed (that's Windows-only).

4. **Blocking the Streamlit main thread is intentional** — the `while not done: sleep(0.5)` loop keeps the websocket alive and allows `st.empty()` containers to stream updates to the browser in real time.

5. **Temp dir cleanup** — PDFs are written to `tempfile.mkdtemp()`. On "Start New Batch", `shutil.rmtree()` cleans up the previous temp dir. On Streamlit Cloud, the container is ephemeral anyway, but explicit cleanup prevents disk exhaustion within a session.

6. **SafeLinks forwarded emails** — when a KONE email is forwarded through Outlook, all links are wrapped in `eur02.safelinks.protection.outlook.com`. Outlook preserves the original URL in an `originalsrc` attribute. Always check `originalsrc` before trying to decode the SafeLinks `href`.

7. **`accept_downloads=True` on browser context** — required for Playwright to intercept file downloads. Without it, downloads are silently ignored.

8. **No secrets / no API key** — this app requires no Streamlit secrets. Nothing to configure on Streamlit Cloud beyond the repo/branch/file.
