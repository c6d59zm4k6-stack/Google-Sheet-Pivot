# Setup Guide — Pivot Table Agent

This guide assumes zero coding background. Follow it top to bottom, in order.
Total time: roughly 1–2 hours, mostly clicking through Google's setup screens.

---

## Part 1 — Get a Groq API key (5 minutes)

1. Go to https://console.groq.com/keys
2. Sign up / log in.
3. Click **Create API Key**, give it any name, copy the key somewhere safe.
   You will paste this into Render later as `GROQ_API_KEY`.

---

## Part 2 — Google Cloud project + OAuth (30–40 minutes)

This is the fiddliest part. Go slowly, it's mostly clicking, no coding.

### 2.1 Create a project
1. Go to https://console.cloud.google.com/
2. Top left, click the project dropdown → **New Project**.
3. Name it anything (e.g. "pivot-agent") → **Create**.

### 2.2 Enable the two APIs you need
1. In the search bar at the top, search **Google Drive API** → click it → **Enable**.
2. Search **Google Sheets API** → click it → **Enable**.

### 2.3 Configure the OAuth consent screen
1. In the left sidebar: **APIs & Services → OAuth consent screen**.
2. User type: **External** → Create.
3. Fill in: App name (anything), your email for support + developer contact.
4. Click through **Save and Continue** on the Scopes page (leave default, we'll rely on scopes requested in code).
5. On the **Test users** page, click **Add Users** and add your own Google email address. This matters — while the app is "in testing," only listed test users can log in.
6. Save and continue to finish.

### 2.4 Create OAuth credentials
1. Left sidebar: **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. Application type: **Web application**.
4. Name: anything.
5. Under **Authorized redirect URIs**, click **+ Add URI** and enter:
   ```
   https://YOUR-RENDER-APP-NAME.onrender.com/oauth2callback
   ```
   (You'll get your actual Render URL in Part 3 — come back and edit this afterward if needed. For now, you can also add `http://localhost:5000/oauth2callback` for local testing.)
6. Click **Create**. A popup shows your **Client ID** and **Client Secret** — copy both somewhere safe.

### 2.5 Share your Drive folder + get its ID
1. Open the Google Drive folder containing your spreadsheets.
2. Copy its ID from the URL: `https://drive.google.com/drive/folders/`**`THIS_PART_IS_THE_ID`**
3. Keep this ID handy — it's `DRIVE_FOLDER_ID`.

---

## Part 3 — Deploy the backend on Render (20–30 minutes)

1. Create a free account at https://render.com (you can sign up with GitHub).
2. Put this project's `backend/` and `frontend/` folders into a GitHub repository:
   - Easiest way: go to https://github.com/new, create a repo (e.g. `pivot-agent`), then use GitHub's web "Add file → Upload files" to upload everything in this project folder, keeping the same folder structure.
3. In Render: **New → Web Service**.
4. Connect your GitHub repo.
5. Settings:
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
6. Under **Environment Variables**, add each of these (values from Parts 1–2):
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `OAUTH_REDIRECT_URI` → `https://YOUR-RENDER-APP-NAME.onrender.com/oauth2callback`
   - `DRIVE_FOLDER_ID`
   - `GROQ_API_KEY`
   - `FLASK_SECRET_KEY` → any random long string you make up
7. Click **Create Web Service**. Wait for the build to finish (a few minutes).
8. Once live, copy your app's URL (e.g. `https://pivot-agent.onrender.com`).
9. **Go back to Google Cloud Console → Credentials → your OAuth client**, and make sure the redirect URI exactly matches `https://pivot-agent.onrender.com/oauth2callback` (edit if it was a placeholder before).

---

## Part 4 — Use it

1. Visit your Render URL in a browser.
2. Click **Sign in with Google** → log in with the same account you added as a test user in step 2.3.5.
3. Google will show a warning screen ("Google hasn't verified this app") because it's in testing mode — click **Advanced → Go to (your app name)**. This is expected and safe; it's your own app.
4. Pick a file from the dropdown, type your request (e.g. "total revenue by region and month"), click **Generate**.
5. The pivot appears above, and a link to the new tab in the Google Sheet appears below the table (only for native Google Sheets files, not CSV/xlsx).

---

## Notes / limitations of this simple version

- **Free Render tier sleeps** after ~15 minutes of no traffic. The first request after sleeping takes ~30 seconds to wake up — this is normal, not a bug.
- **Only Google Sheets get the "write to a new tab" feature.** CSV and Excel files uploaded to Drive will still generate a pivot in the browser, just without the Sheets write-back (there's no sheet to write a tab into).
- **"In testing" OAuth mode** limits login to the test users you explicitly added. Fine for personal/small-team use. If you want anyone to log in without that restriction, Google requires an app verification review — not worth it unless you're making this public.
- If something breaks, the most common causes are: redirect URI mismatch (must match *exactly*, including https vs http), or a missing/misspelled environment variable in Render.
