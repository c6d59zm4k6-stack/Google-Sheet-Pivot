"""
Pivot Table Agent - Backend
============================
A small Flask app that:
  1. Lets the user log in with Google (OAuth)
  2. Lets the user browse their Drive and pick a file via the Google
     Picker popup (a native Google file-browser widget, not built by us)
  3. Reads the chosen file's data
  4. Sends the user's plain-English request + column headers to Groq
     to get back a "pivot spec" (rows/columns/values/aggregation)
  5. Builds the pivot with pandas
  6. Writes the pivot into a NEW TAB in the same Google Sheet
  7. Returns the pivot as JSON so the frontend can render it

Non-coder notes:
  - You do not need to understand every line here.
  - You DO need to fill in the values described in SETUP.md
    (Google OAuth credentials, a Google API key for the Picker, Groq
    API key) as environment variables before this will run.
"""

import os

# Google sometimes returns a granted scope list that differs slightly from
# what was requested (e.g. if the user unchecks a permission box on the
# consent screen, or Google reorders/normalizes the scope string). By default
# oauthlib treats ANY scope mismatch as a hard error and raises instead of
# just warning. This must be set before oauthlib's client code runs.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

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
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration (all pulled from environment variables — see SETUP.md)
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
# Where Google sends the user back after login. Must match the Google Cloud
# Console "Authorized redirect URI" EXACTLY (see SETUP.md).
OAUTH_REDIRECT_URI = os.environ["OAUTH_REDIRECT_URI"]

# A plain Google API key (NOT the OAuth client secret) used only to
# authorize the Google Picker widget in the browser. Create one at
# https://console.cloud.google.com/apis/credentials -> Create Credentials ->
# API key, and restrict it to the Google Picker API (and, for safety, to
# your Render domain under "Application restrictions -> HTTP referrers").
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

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
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
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

    data = dict(session["credentials"])
    expiry_str = data.pop("expiry", None)
    creds = Credentials(**data)
    if expiry_str:
        creds.expiry = datetime.fromisoformat(expiry_str)

    # Access tokens expire after about an hour. Refresh proactively so any
    # caller (our own API calls, or the token we hand to the Picker) always
    # gets a working one, rather than failing with a stale-token error.
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            session["credentials"]["token"] = creds.token
            session["credentials"]["expiry"] = creds.expiry.isoformat() if creds.expiry else None
        except Exception:
            pass  # fall through with whatever token we already had

    return creds


# ---------------------------------------------------------------------------
# Hand the frontend what it needs to open the Google Picker (file/folder
# browser popup). The Picker itself talks directly to Google from the
# browser — our backend just needs to supply a valid access token + API key.
# ---------------------------------------------------------------------------

@app.route("/picker-token")
def picker_token():
    creds = _get_credentials()
    if not creds:
        return jsonify({"error": "not_logged_in"}), 401
    if not GOOGLE_API_KEY:
        return jsonify({
            "error": "no_api_key",
            "message": "GOOGLE_API_KEY is not set on the server (see SETUP.md).",
        }), 500
    return jsonify({"access_token": creds.token, "api_key": GOOGLE_API_KEY})


# ---------------------------------------------------------------------------
# List the tabs in a Google Sheet, so the frontend can offer a tab picker
# for spreadsheets that have more than one.
# ---------------------------------------------------------------------------

@app.route("/sheet-tabs")
def sheet_tabs():
    creds = _get_credentials()
    if not creds:
        return jsonify({"error": "not_logged_in"}), 401
    file_id = request.args.get("file_id")
    if not file_id:
        return jsonify({"error": "no_file_id", "message": "Missing file_id."}), 400
    try:
        sheets = build("sheets", "v4", credentials=creds)
        meta = sheets.spreadsheets().get(
            spreadsheetId=file_id, fields="sheets.properties.title"
        ).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        return jsonify({"tabs": tabs})
    except Exception as e:
        return jsonify({"error": "sheets_error", "message": f"Could not read sheet tabs: {e}"}), 500


# ---------------------------------------------------------------------------
# Fetch a file's data into a pandas DataFrame
# ---------------------------------------------------------------------------

def _fetch_dataframe(creds, file_id, mime_type, sheet_tab=None):
    if mime_type == "application/vnd.google-apps.spreadsheet":
        sheets = build("sheets", "v4", credentials=creds)
        if sheet_tab:
            # A1 notation requires the sheet name in single quotes (needed if
            # it has spaces/special characters); a literal quote inside the
            # name itself must be doubled, per Sheets' escaping rule.
            safe_tab = sheet_tab.replace("'", "''")
            range_ = f"'{safe_tab}'!A1:ZZ10000"
        else:
            # No tab specified: Sheets API defaults to the first tab.
            range_ = "A1:ZZ10000"
        result = sheets.spreadsheets().values().get(
            spreadsheetId=file_id, range=range_
        ).execute()
        values = result.get("values", [])
        if not values:
            return pd.DataFrame(), sheets
        header, rows = values[0], values[1:]
        ncols = len(header)
        # Google's Sheets API trims trailing empty cells per row independently,
        # so rows can come back shorter than the header (pad with "") OR
        # longer than the header (extra unlabeled columns with stray data —
        # truncate them, since we have no header name to give them anyway).
        fixed_rows = []
        for r in rows:
            if len(r) < ncols:
                r = r + [""] * (ncols - len(r))
            elif len(r) > ncols:
                r = r[:ncols]
            fixed_rows.append(r)
        df = pd.DataFrame(fixed_rows, columns=header)
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
# Apply a list of {"column","op","value"} filters (combined with AND) to any
# DataFrame. Used both BEFORE aggregation (raw-row filters, e.g. "only
# Pithoragarh district") and AFTER aggregation (e.g. "only districts with
# more than 5 schools" — a "having" filter on the pivot result itself).
# ---------------------------------------------------------------------------

def _apply_filters(df, filters):
    if not filters:
        return df
    mask = pd.Series(True, index=df.index)
    for f in filters:
        col = f.get("column")
        op = f.get("op")
        val = f.get("value")
        if col not in df.columns:
            raise ValueError(f"Filter column '{col}' not found.")
        series = df[col]

        if op in (">", ">=", "<", "<="):
            series_num = pd.to_numeric(series, errors="coerce")
            val_num = float(val)
            if op == ">":
                cond = series_num > val_num
            elif op == ">=":
                cond = series_num >= val_num
            elif op == "<":
                cond = series_num < val_num
            else:
                cond = series_num <= val_num
        elif op == "==":
            cond = series.astype(str).str.strip().str.lower() == str(val).strip().lower()
        elif op == "!=":
            cond = series.astype(str).str.strip().str.lower() != str(val).strip().lower()
        elif op == "contains":
            cond = series.astype(str).str.contains(str(val), case=False, na=False)
        elif op == "in":
            values = val if isinstance(val, list) else [val]
            values_lower = [str(v).strip().lower() for v in values]
            cond = series.astype(str).str.strip().str.lower().isin(values_lower)
        else:
            raise ValueError(f"Unsupported filter operator '{op}'.")
        mask &= cond
    return df[mask]


# ---------------------------------------------------------------------------
# Normalize an aggfunc name (which may be a synonym the model used) into
# something pandas actually accepts, or a custom callable for the ones pandas
# doesn't have a built-in string for (like "range").
# ---------------------------------------------------------------------------

_AGGFUNC_ALIASES = {
    "sum": "sum", "total": "sum",
    "mean": "mean", "average": "mean", "avg": "mean",
    "median": "median",
    "min": "min", "minimum": "min",
    "max": "max", "maximum": "max",
    "std": "std", "stddev": "std", "std_dev": "std", "standard_deviation": "std",
    "var": "var", "variance": "var",
    "count": "count", "counta": "count",  # pandas' 'count' already counts any non-blank value, text included
    "nunique": "nunique", "unique_count": "nunique", "uniquecount": "nunique",
    "distinct": "nunique", "distinct_count": "nunique", "count_unique": "nunique",
    "prod": "prod", "product": "prod", "mult": "prod", "multiply": "prod",
    "first": "first", "last": "last",
    "any": "any", "all": "all",
    "range": "range",  # handled specially below — not a built-in pandas string
    "mode": "mode", "most_common": "mode", "most_frequent": "mode", "top_value": "mode",
}


def _resolve_aggfunc(name):
    key = str(name or "sum").strip().lower().replace(" ", "_").replace("-", "_")
    if key not in _AGGFUNC_ALIASES:
        allowed = sorted(set(_AGGFUNC_ALIASES.values()))
        raise ValueError(f"Unsupported aggregation '{name}'. Supported: {', '.join(allowed)}.")
    resolved = _AGGFUNC_ALIASES[key]
    if resolved == "range":
        return lambda s: s.max() - s.min()
    if resolved == "mode":
        return lambda s: s.mode().iloc[0] if not s.mode().empty else None
    return resolved


def _canonical_aggfunc_label(name):
    """The resolved canonical name only (e.g. 'nunique', 'range', 'mode'), for
    building deterministic, self-describing output column names."""
    key = str(name or "sum").strip().lower().replace(" ", "_").replace("-", "_")
    return _AGGFUNC_ALIASES.get(key, "sum")


# ---------------------------------------------------------------------------
# Ask Groq to turn the plain-English request into a pivot spec
# ---------------------------------------------------------------------------

def _get_pivot_spec(user_request, columns):
    system_prompt = (
        "You convert a user's plain-English data-analysis request into a JSON spec. "
        "You will be given the available column names and the user's request. "
        "Respond with ONLY a JSON object, no other text, no markdown fences. "
        "There are four possible \"operation\" types — pick exactly one:\n"
        "\n"
        "1) \"pivot\" — group rows and aggregate one column. Shape:\n"
        '   {"operation": "pivot", "index": ["col_a"], "columns": [], "values": ["col_b"], "aggfunc": "sum"}\n'
        "   'aggfunc' must be one of: sum, mean, median, min, max, std, var, count, "
        "nunique, prod, first, last, any, all, range, mode.\n"
        "   Guidance — average -> mean; standard deviation -> std; variance -> var; "
        "unique/distinct count -> nunique; plain row count (any column, incl. text) -> count; "
        "product of values -> prod; max-minus-min -> range; most common/most frequent value -> "
        "mode; any/all only make sense on True/False columns.\n"
        "   Output column naming: when 'values' has exactly one column and 'columns' is empty, "
        "the result column is named \"<value_column>_<aggfunc>\" (e.g. values=[\"Revenue\"], "
        "aggfunc=\"sum\" -> a column literally named \"Revenue_sum\"). Use that exact name if "
        "you need to 'sort' or filter ('having') by it.\n"
        "\n"
        "2) \"correlation\" — correlation between exactly two numeric columns, optionally per "
        "group. Shape:\n"
        '   {"operation": "correlation", "index": [], "values": ["col_a", "col_b"]}\n'
        "   'index' is an empty list for one overall correlation, or one or more columns to "
        "compute it separately within each group.\n"
        "   Output column naming: the result column is named \"corr_<col_a>_<col_b>\".\n"
        "\n"
        "3) \"ratio\" — divide one aggregated column by another, per group (e.g. \"average "
        "revenue per student\" = sum(revenue) / sum(students)). Shape:\n"
        '   {"operation": "ratio", "index": ["col_a"], "values": ["numerator_col", "denominator_col"], "aggfunc": "sum"}\n'
        "   'aggfunc' is how each side is aggregated before dividing (usually sum, sometimes mean).\n"
        "   Output column naming: the result column is named \"<numerator_col>_per_<denominator_col>\".\n"
        "\n"
        "4) \"diff\" — change from one period/step to the next, of an aggregated column (e.g. "
        "\"month-over-month change in revenue\"). Shape:\n"
        '   {"operation": "diff", "index": ["period_col"], "values": ["col_b"], "aggfunc": "sum"}\n'
        "   Only use this when the request is clearly about change over a sequence (time, "
        "rank, etc.) — 'index' should be the column defining that sequence.\n"
        "   Output column naming: the result column is named \"<col_b>_change\".\n"
        "\n"
        "General rules: only use column names from the provided list, exactly as given. "
        "Default to \"pivot\" unless the request clearly matches one of the other three.\n"
        "\n"
        "Optional fields, usable alongside ANY of the four operations above:\n"
        '- "filters": row-level filters applied to the raw data BEFORE aggregation, e.g. '
        "only include certain districts/dates/values. List of "
        '{"column": "col_name", "op": "==", "value": "X"}, combined with AND. '
        "'op' is one of: ==, !=, >, >=, <, <=, contains, in (value is a list for 'in').\n"
        '- "having": same shape as filters, but applied to the RESULT after aggregation '
        "(e.g. \"only districts with more than 5 schools\").\n"
        '- "sort": {"by": "column_name", "order": "asc"} or {"by": "column_name", "order": "desc"}. '
        "'by' must be EXACTLY a column name that appears in the final result — either an "
        "index/group column (its original name) or the aggregated value column (using the "
        "output naming rule documented above for that operation type).\n"
        '- "limit": an integer cap on the number of result rows, applied after sorting '
        "(e.g. \"top 5 districts by revenue\" -> sort desc by revenue + limit 5)."
    )
    user_msg = f"Available columns: {columns}\nUser request: {user_request}"

    # Debug visibility: print exactly what we send to and receive from Groq,
    # so it's inspectable in the Render logs when a result looks wrong.
    print(f"\n=== GROQ REQUEST ===\n{user_msg}", flush=True)

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
    print(f"=== GROQ RAW RESPONSE ===\n{content}", flush=True)
    # Strip accidental markdown fences just in case
    content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
    spec = json.loads(content)
    print(f"=== PARSED SPEC ===\n{spec}\n", flush=True)
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
    sheet_tab = body.get("sheet_tab")
    user_request = body.get("request", "").strip()

    if not file_id:
        return jsonify({"error": "no_file_selected", "message": "Please select a file."}), 400
    if not user_request:
        return jsonify({"error": "no_request", "message": "Please describe the pivot you want."}), 400

    df, sheets_handle = _fetch_dataframe(creds, file_id, mime_type, sheet_tab)
    if df.empty:
        return jsonify({"error": "empty_file", "message": "That file has no data."}), 400
    df = _coerce_numeric(df)

    try:
        spec = _get_pivot_spec(user_request, list(df.columns))
    except Exception as e:
        return jsonify({"error": "spec_failed", "message": f"Could not interpret the request: {e}"}), 500

    try:
        df = _apply_filters(df, spec.get("filters"))
        if df.empty:
            return jsonify({"error": "empty_after_filter", "message": "No rows match those filters.", "spec": spec}), 400
    except Exception as e:
        return jsonify({"error": "filter_failed", "message": f"Could not apply filters: {e}", "spec": spec}), 500

    try:
        operation = spec.get("operation", "pivot")
        idx = spec.get("index") or []
        vals = spec.get("values") or []

        if operation == "pivot":
            pivot = pd.pivot_table(
                df,
                index=idx or None,
                columns=spec.get("columns") or None,
                values=vals or None,
                aggfunc=_resolve_aggfunc(spec.get("aggfunc")),
            )
            pivot = pivot.reset_index()
            # Simple group+aggregate (no column-spread): rename the aggregated
            # value column to "<value>_<aggfunc>" per the convention documented
            # to the model. This both makes the header self-describing (it was
            # previously just "School Name" even when showing a COUNT) and lets
            # sort/having reliably reference this column by the name the model
            # was told to expect.
            if len(vals) == 1 and not spec.get("columns"):
                agg_label = _canonical_aggfunc_label(spec.get("aggfunc"))
                pivot = pivot.rename(columns={vals[0]: f"{vals[0]}_{agg_label}"})

        elif operation == "correlation":
            if len(vals) != 2:
                raise ValueError("Correlation needs exactly two columns in 'values'.")
            col_a, col_b = vals
            label = f"corr_{col_a}_{col_b}"
            if idx:
                pivot = (
                    df.groupby(idx)
                    .apply(lambda g: g[col_a].corr(g[col_b]))
                    .reset_index(name=label)
                )
            else:
                pivot = pd.DataFrame({label: [df[col_a].corr(df[col_b])]})

        elif operation == "ratio":
            if len(vals) != 2:
                raise ValueError("Ratio needs exactly two columns in 'values' (numerator, denominator).")
            num_col, denom_col = vals
            aggfunc = _resolve_aggfunc(spec.get("aggfunc"))
            label = f"{num_col}_per_{denom_col}"
            if idx:
                grouped = df.groupby(idx).agg(
                    _num=(num_col, aggfunc), _denom=(denom_col, aggfunc)
                )
                grouped[label] = grouped["_num"] / grouped["_denom"]
                pivot = grouped[[label]].reset_index()
            else:
                num = df[num_col].agg(aggfunc)
                denom = df[denom_col].agg(aggfunc)
                pivot = pd.DataFrame({label: [num / denom]})

        elif operation == "diff":
            if not idx:
                raise ValueError("Diff needs an 'index' column defining the sequence (e.g. a date/period column).")
            if len(vals) != 1:
                raise ValueError("Diff needs exactly one column in 'values'.")
            aggfunc = _resolve_aggfunc(spec.get("aggfunc"))
            grouped = df.groupby(idx)[vals[0]].agg(aggfunc)
            # Sort by real chronological order if the labels parse as dates, but keep
            # the ORIGINAL labels for display (parsing "Jan"/"Feb" defaults to a
            # nonsense year internally — we only want it for sort order, not to show).
            date_sort_used = False
            try:
                parsed = pd.to_datetime(grouped.index, errors="raise")
                grouped = grouped.iloc[parsed.argsort()]
                date_sort_used = True
            except Exception:
                grouped = grouped.sort_index()
            diffed = grouped.diff()
            pivot = diffed.reset_index()
            pivot.columns = [idx[0], f"{vals[0]}_change"]
            if not date_sort_used:
                pivot.attrs["order_warning"] = (
                    f"'{idx[0]}' wasn't recognized as a date, so rows are sorted "
                    "alphabetically rather than chronologically — double check the order."
                )

        else:
            raise ValueError(f"Unknown operation '{operation}'.")

        # Flatten multi-level columns if 'columns' was used (pivot operation only)
        pivot.columns = [str(c) if not isinstance(c, tuple) else "_".join(map(str, c)) for c in pivot.columns]

        # Post-aggregation filter ("having"), sort, and limit — apply in that
        # order so "top N" requests (sort + limit) work as expected.
        pivot = _apply_filters(pivot, spec.get("having"))

        sort_spec = spec.get("sort")
        if sort_spec:
            sort_by = sort_spec.get("by")
            if sort_by in pivot.columns:
                pivot = pivot.sort_values(
                    by=sort_by,
                    ascending=(sort_spec.get("order", "asc").lower() != "desc"),
                )
            else:
                # Don't fail the whole request over a bad sort column, but don't
                # silently ignore it either — the person asked for an order and
                # didn't get one, so say so.
                existing_warning = pivot.attrs.get("order_warning", "")
                pivot.attrs["order_warning"] = (
                    existing_warning
                    + f" Requested sort column '{sort_by}' wasn't found in the result "
                    f"(available: {', '.join(pivot.columns)}), so sorting was skipped."
                ).strip()

        limit = spec.get("limit")
        if isinstance(limit, int) and limit > 0:
            pivot = pivot.head(limit)

        pivot = pivot.reset_index(drop=True)
    except Exception as e:
        return jsonify({"error": "pivot_failed", "message": f"Could not build result: {e}", "spec": spec}), 500

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
        "warning": pivot.attrs.get("order_warning"),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
