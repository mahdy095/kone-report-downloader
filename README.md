# KONE Service Report Downloader

Upload KONE service-update emails (`.msg`) and get every service-report PDF back as one ZIP.

- **Live:** https://kone-report-downloader-6vk8ycxgcqha4ux89sagmq.streamlit.app (Streamlit Community Cloud,
  redeploys on every push to `main`)
- **Emails:** German ("Jobdetails" link) and Dutch ("Statusinformatie" link), sent directly or forwarded
  (Outlook SafeLinks)
- **Collective reports:** when KONE sends one PDF for a whole site visit, the results page offers
  "Sammelberichte aufteilen". You get a second ZIP with one PDF per order (Auftragsnr.) plus each visit's cover
  page (`…_Deckblatt.pdf`, it can hold the customer signature), and the original ZIP stays available.
- **User interface:** German, written for non-technical users; technical reasons (why a PDF was not split, read
  errors) go to the server log only (Streamlit Cloud → Manage app → logs).
- **"🐞 Problem melden"** (bottom right, every screen) opens a new email in the default mail app (Outlook) to
  a.elmahdi@hundt-consult.de with lines for the description and "Füge bitte nach Möglichkeit Screenshots hinzu."

## OCR for scanned pages (optional)
Pages without a text layer are read with Azure Document Intelligence. Add the same two secrets the ZÜS PDF
Renamer uses to the app's Streamlit secrets (Community Cloud: app → Settings → Secrets; locally
`.streamlit/secrets.toml`, which is git-ignored):
```toml
AZURE_DI_ENDPOINT = "https://<resource>.cognitiveservices.azure.com/"
AZURE_DI_KEY      = "<key>"
```
Without them the app works as before; a collective PDF with unreadable pages is then left whole with a warning.

## Run locally
```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m streamlit run app.py
```

## Files
| File | What it is |
|---|---|
| `app.py` | The whole app (Streamlit UI + Playwright download + collective-report split) |
| `PROJECT_CONTEXT.md` | Full technical reference: deployment, URL extraction, portal download, collective-report split |
| `requirements.txt` / `runtime.txt` | Python packages (`pypdf` pinned, the split depends on its text layout) / Python 3.11 |
| `.streamlit/config.toml` | Upload limit + theme |
| `packages.txt.disabled` | Old apt list, disabled (Chromium libraries are installed at runtime, see PROJECT_CONTEXT.md) |
