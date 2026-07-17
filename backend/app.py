"""
Pivot Table Agent - Backend
============================
A small Flask app that:
  1. Lets the user log in with Google (OAuth)
  2. Lists spreadsheet files in a specific Google Drive folder
  3. Reads the chosen file's data
  4. Sends the user's plain-English request + column headers to Groq
     to get back a "pivot spec" (rows/columns/values/aggregation)
  5. Builds the pivot with pandas
  6. Writes the pivot into a NEW TAB in the same Google Sheet
  7. Returns the pivot as JSON so the frontend can render it

Non-coder notes:
  - You do not need to understand every line here.
  - You DO need to fill in the values described in SETUP.md
    (Google OAuth credentials, folder ID, Groq API key) as
    environment variables before this will run.
"""

import os
import json
import re
import secrets
from datetime import datetime

import pandas as pd
import requests
from flask import Flask, redirect, request, session, jsonify, url_for, send_from_directory
from flask_cors import CORS

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration (all pulled from environment variables — see SETUP.md)
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
# Where Google sends the user back after login. Must match the Google Cloud
# Console "Authorized redirect URI" EXACTLY (see SETUP.md).
OAUTH_REDIRECT_URI = os.environ["OAUTH_REDIRECT_URI"]

# The Google Drive folder to look inside for files.
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

# Groq API key (https://console.groq.com/keys)
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# Flask needs a secret key to keep the login session secure.
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

app = Flask(__name__, static_folder="../frontend", static_url_path="")
app.secret_key = FLASK_SECRET_KEY
CORS(app, supports_credentials=True)


@app.route("/")
def serve_index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# OAuth: login flow
# ---------------------------------------------------------------------------

def _make_flow():
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI)


@app.route("/login")
def login():
    flow = _make_flow()
    # Google now requires PKCE for this client type. We generate the
    # code_verifier ourselves and stash it in the session so the SAME
    # value can be reused in /oauth2callback (a new Flow object is created
    # there and would otherwise have no idea what verifier was used here).
    flow.code_verifier = secrets.token_urlsafe(64)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    session["state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    flow = _make_flow()
    # Re-attach the code_verifier we generated in /login so it matches the
    # code_challenge Google saw during the authorization step. Without this,
    # token exchange fails with "invalid_grant: Missing code verifier".
    flow.code_verifier = session.get("code_verifier")
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    session["credentials"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    # Redirect back to the frontend page after login.
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/status")
def status():
    return jsonify({"logged_in": "credentials" in session})


def _get_credentials():
    if "credentials" not in session:
        return None
    return Credentials(**session["credentials"])


# ---------------------------------------------------------------------------
# List files in the configured Drive folder
# ---------------------------------------------------------------------------

@app.route("/list-files")
def list_files():
    creds = _get_credentials()
    if not creds:
        return jsonify({"error": "not_logged_in"}), 401

    drive = build("drive", "v3", credentials=creds)
    query = (
        f"'{DRIVE_FOLDER_ID}' in parents and trashed = false and "
        "(mimeType = 'application/vnd.google-apps.spreadsheet' or "
        "mimeType = 'text/csv' or "
        "mimeType = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')"
    )
    results = drive.files().list(q=query, fields="files(id, name, mimeType)").execute()
    files = results.get("files", [])
    return jsonify({"files": files})


# ---------------------------------------------------------------------------
# Fetch a file's data into a pandas DataFrame
# ---------------------------------------------------------------------------

def _fetch_dataframe(creds, file_id, mime_type):
    if mime_type == "application/vnd.google-apps.spreadsheet":
        sheets = build("sheets", "v4", credentials=creds)
        result = sheets.spreadsheets().values().get(
            spreadsheetId=file_id, range="A1:ZZ10000"
        ).execute()
        values = result.get("values", [])
        if not values:
            return pd.DataFrame(), sheets
        header, rows = values[0], values[1:]
        # Pad short rows so pandas doesn't choke on ragged data
        rows = [r + [""] * (len(header) - len(r)) for r in rows]
        df = pd.DataFrame(rows, columns=header)
        return df, sheets
    else:
        # CSV or xlsx: download raw bytes via Drive API
        drive = build("drive", "v3", credentials=creds)
        request_ = drive.files().get_media(fileId=file_id)
        import io
        buf = io.BytesIO()
        from googleapiclient.http import MediaIoBaseDownload
        downloader = MediaIoBaseDownload(buf, request_)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
        if mime_type == "text/csv":
            df = pd.read_csv(buf)
        else:
            df = pd.read_excel(buf)
        return df, None  # no Sheets API handle for non-Sheets files


# Try to coerce numeric-looking columns (Sheets API returns everything as strings)
def _coerce_numeric(df):
    for col in df.columns:
        coerced = pd.to_numeric(df[col], errors="coerce")
        if coerced.notna().sum() >= 0.8 * len(df):
            df[col] = coerced
    return df


# ---------------------------------------------------------------------------
# Ask Groq to turn the plain-English request into a pivot spec
# ---------------------------------------------------------------------------

def _get_pivot_spec(user_request, columns):
    system_prompt = (
        "You convert a user's plain-English request into a JSON pivot table spec. "
        "You will be given the available column names and the user's request. "
        "Respond with ONLY a JSON object, no other text, no markdown fences. "
        "The JSON must have this shape: "
        '{"index": ["col_a"], "columns": [], "values": ["col_b"], "aggfunc": "sum"} '
        "Rules: "
        "- 'index' = columns to group rows by (at least one, from the request). "
        "- 'columns' = columns to pivot into new columns (often empty list). "
        "- 'values' = numeric column(s) to aggregate. "
        "- 'aggfunc' = one of: sum, mean, count, min, max. "
        "- Only use column names from the provided list, exactly as given."
    )
    user_msg = f"Available columns: {columns}\nUser request: {user_request}"

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip accidental markdown fences just in case
    content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
    spec = json.loads(content)
    return spec


# ---------------------------------------------------------------------------
# Main endpoint: build the pivot
# ---------------------------------------------------------------------------

@app.route("/generate-pivot", methods=["POST"])
def generate_pivot():
    creds = _get_credentials()
    if not creds:
        return jsonify({"error": "not_logged_in"}), 401

    body = request.get_json(force=True)
    file_id = body.get("file_id")
    mime_type = body.get("mime_type")
    user_request = body.get("request", "").strip()

    if not file_id:
        return jsonify({"error": "no_file_selected", "message": "Please select a file."}), 400
    if not user_request:
        return jsonify({"error": "no_request", "message": "Please describe the pivot you want."}), 400

    df, sheets_handle = _fetch_dataframe(creds, file_id, mime_type)
    if df.empty:
        return jsonify({"error": "empty_file", "message": "That file has no data."}), 400
    df = _coerce_numeric(df)

    try:
        spec = _get_pivot_spec(user_request, list(df.columns))
    except Exception as e:
        return jsonify({"error": "spec_failed", "message": f"Could not interpret the request: {e}"}), 500

    try:
        pivot = pd.pivot_table(
            df,
            index=spec.get("index") or None,
            columns=spec.get("columns") or None,
            values=spec.get("values") or None,
            aggfunc=spec.get("aggfunc", "sum"),
        )
        pivot = pivot.reset_index()
        # Flatten multi-level columns if 'columns' was used
        pivot.columns = [str(c) if not isinstance(c, tuple) else "_".join(map(str, c)) for c in pivot.columns]
    except Exception as e:
        return jsonify({"error": "pivot_failed", "message": f"Could not build pivot: {e}", "spec": spec}), 500

    # Write back to a new tab, only possible for native Google Sheets files
    sheet_tab_url = None
    if sheets_handle is not None:
        try:
            tab_name = f"Pivot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            add_sheet_resp = sheets_handle.spreadsheets().batchUpdate(
                spreadsheetId=file_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
            ).execute()
            new_sheet_id = add_sheet_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

            values = [list(pivot.columns)] + pivot.astype(object).where(pd.notnull(pivot), "").values.tolist()
            sheets_handle.spreadsheets().values().update(
                spreadsheetId=file_id,
                range=f"{tab_name}!A1",
                valueInputOption="RAW",
                body={"values": values},
            ).execute()
            sheet_tab_url = f"https://docs.google.com/spreadsheets/d/{file_id}/edit#gid={new_sheet_id}"
        except Exception as e:
            # Non-fatal: we still return the pivot to the frontend even if write-back fails
            sheet_tab_url = None

    return jsonify({
        "pivot": pivot.to_dict(orient="records"),
        "columns": list(pivot.columns),
        "sheet_tab_url": sheet_tab_url,
        "spec_used": spec,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
