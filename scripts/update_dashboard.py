#!/usr/bin/env python3
"""
Daily App Store Connect Dashboard Updater
Fetches analytics for PatchPal (iOS + Android) and MealSight,
generates data.json, updates GitHub Gist.
"""
import json
import time
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
import jwt  # PyJWT with cryptography backend

# === CONFIG ===
KEY_ID = os.environ["ASC_KEY_ID"]
ISSUER_ID = os.environ["ASC_ISSUER_ID"]
PRIVATE_KEY = os.environ["ASC_PRIVATE_KEY"]
GIST_ID = "a4c54ade850bc740fdfe8bb583a85648"
GIST_PAT = os.environ["GIST_PAT"]
PATCHPAL_APP_ID = "6741104775"
MEALSIGHT_APP_ID = "6743397801"
ANDROID_PATCHPAL_PACKAGE = "com.patchpal.app"
BASE_URL = "https://api.appstoreconnect.apple.com"

def generate_jwt():
    """Generate App Store Connect JWT token."""
    now = int(time.time())
    payload = {
        "iss": ISSUER_ID,
        "iat": now,
        "exp": now + 1200,  # 20 min
        "aud": "appstoreconnect-v1",
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="ES256", headers={"kid": KEY_ID})

def api_get(token, path):
    """Make authenticated GET request to App Store Connect API."""
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"API Error {e.code} for {path}: {e.read().decode()}", file=sys.stderr)
        return None

def fetch_analytics(token, app_id, metric, start_date, end_date, frequency="DAILY", dimension=None):
    """Fetch analytics metrics from App Store Connect Analytics API."""
    path = (
        f"/v1/apps/{app_id}/analyticsReportRequests"
    )
    # Use the newer Sales and Trends / Analytics API
    # Try the metrics endpoint
    params = (
        f"filter[measureType]={metric}"
        f"&filter[frequency]={frequency}"
        f"&filter[startDate]={start_date}"
        f"&filter[endDate]={end_date}"
    )
    if dimension:
        params += f"&filter[dimensionType]={dimension}"
    metrics_path = f"/v1/apps/{app_id}/analyticsMeasurements?{params}"
    return api_get(token, metrics_path)

def fetch_app_info(token, app_id):
    """Fetch app info and version status."""
    data = api_get(token, f"/v1/apps/{app_id}?include=appStoreVersions")
    return data

def fetch_sales_reports(token, app_id, start_date, end_date):
    """
    Fetch analytics using the App Store Connect API v1 analytics reports.
    Uses the /v1/analyticsReportRequests endpoint.
    """
    path = f"/v1/apps/{app_id}/analyticsReportRequests"
    return api_get(token, path)

def safe_div(a, b, decimals=1):
    """Safe division with rounding."""
    if b == 0:
        return 0
    return round(a / b, decimals)

def fetch_metrics_via_reports(token, app_id, report_type, start_date, end_date):
    """
    Use the App Analytics API to fetch metrics.
    App Store Connect provides analytics through report requests.
    """
    path = f"/v1/apps/{app_id}?fields[apps]=bundleId,name"
    return api_get(token, path)

def fetch_android_info():
    """
    Fetch Android PatchPal app info.
    Uses Google Play Developer API if GOOGLE_PLAY_SERVICE_ACCOUNT_JSON secret is set.
    Otherwise returns static 'in_review' status.
    Once the app is live, set up the secret to get real install counts and status.
    """
    package = ANDROID_PATCHPAL_PACKAGE
    try:
        service_account_json = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
        if service_account_json:
            try:
                from google.oauth2 import service_account as gsa
                import googleapiclient.discovery
                creds = gsa.Credentials.from_service_account_info(
                    json.loads(service_account_json),
                    scopes=["https://www.googleapis.com/auth/androidpublisher"]
                )
                service = googleapiclient.discovery.build(
                    "androidpublisher", "v3", credentials=creds, cache_discovery=False
                )
                # Create a temporary edit to read track info, then delete it
                edit = service.edits().insert(packageName=package, body={}).execute()
                edit_id = edit["id"]
                tracks_result = service.edits().tracks().list(
                    packageName=package, editId=edit_id
                ).execute()
                service.edits().delete(packageName=package, editId=edit_id).execute()

                # Parse production track status
                status = "in_review"
                for track in tracks_result.get("tracks", []):
                    if track["track"] == "production":
                        releases = track.get("releases", [])
                        if releases:
                            s = releases[-1].get("status", "")
                            if s == "completed":
                                status = "live"
                            elif s in ("draft", "inProgress", "halted"):
                                status = s
                print(f"Android Play API status: {status}")
                return {
                    "status": status,
                    "package": package,
                    "installs": 0,
                    "source": "api"
                }
            except Exception as e:
                print(f"Google Play API error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Android info setup error: {e}", file=sys.stderr)

    # Default: return static known status
    return {
        "status": "in_review",
        "package": package,
        "installs": 0,
        "source": "static"
    }

def build_data_json(token):
    """Build the complete data.json structure."""
    today = datetime.now(timezone.utc)
    end_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")

    print(f"Fetching data for period: {start_date} to {end_date}")

    # === Fetch iOS app data ===
    pp_info = fetch_app_info(token, PATCHPAL_APP_ID)
    ms_info = fetch_app_info(token, MEALSIGHT_APP_ID)

    # Try to get analytics reports
    pp_reports = fetch_sales_reports(token, PATCHPAL_APP_ID, start_date, end_date)

    # === Fetch Android PatchPal data ===
    android_info = fetch_android_info()

    # Parse app versions from included data
    pp_versions = []
    ms_versions = []
    if pp_info and "included" in pp_info:
        pp_versions = [v for v in pp_info["included"] if v["type"] == "appStoreVersions"]
    if ms_info and "included" in ms_info:
        ms_versions = [v for v in ms_info["included"] if v["type"] == "appStoreVersions"]

    # Determine PatchPal iOS version info
    pp_live_version = "Unknown"
    pp_next_version = "Unknown"
    pp_next_status = "Unknown"
    for v in pp_versions:
        attrs = v.get("attributes", {})
        state = attrs.get("appVersionState", "") or attrs.get("appStoreState", "")
        version = attrs.get("versionString", "")
        if state == "READY_FOR_SALE":
            pp_live_version = version
        elif state in ("PREPARE_FOR_SUBMISSION", "WAITING_FOR_REVIEW", "IN_REVIEW"):
            pp_next_version = version
            status_map = {
                "PREPARE_FOR_SUBMISSION": "Prepare for Submission",
                "WAITING_FOR_REVIEW": "Waiting for Review",
                "IN_REVIEW": "In Review",
            }
            pp_next_status = status_map.get(state, state)

    # Determine MealSight status
    ms_is_live = False
    ms_status = "Unknown"
    ms_version = "Unknown"
    ms_app_name = "MealSight - AI Food Scanner"
    for v in ms_versions:
        attrs = v.get("attributes", {})
        state = attrs.get("appVersionState", "") or attrs.get("appStoreState", "")
        version = attrs.get("versionString", "")
        if state == "READY_FOR_SALE":
            ms_is_live = True
            ms_version = version
            ms_status = "Ready for Sale"
        elif state in ("WAITING_FOR_REVIEW", "IN_REVIEW", "PREPARE_FOR_SUBMISSION"):
            ms_version = version
            status_map = {
                "PREPARE_FOR_SUBMISSION": "Prepare for Submission",
                "WAITING_FOR_REVIEW": "Waiting for Review",
                "IN_REVIEW": "In Review",
            }
            ms_status = status_map.get(state, state)

    # Try to fetch analytics data via the reports API
    analytics_data = None
    try:
        report_req_url = f"/v1/apps/{PATCHPAL_APP_ID}/analyticsReportRequests"
        existing_reports = api_get(token, report_req_url)
        if existing_reports and "data" in existing_reports:
            for report in existing_reports["data"]:
                report_id = report["id"]
                instances_url = f"/v1/analyticsReportRequests/{report_id}/reports"
                instances = api_get(token, instances_url)
                if instances:
                    analytics_data = instances
                    break
    except Exception as e:
        print(f"Analytics reports error: {e}", file=sys.stderr)

    # Build dates array for charts
    dates = []
    date_cursor = today - timedelta(days=30)
    for i in range(30):
        d = date_cursor + timedelta(days=i)
        if i == 0:
            dates.append(d.strftime("%b %-d"))
        else:
            dates.append(d.strftime("%-d"))

    # Build Android status label
    android_status_map = {
        "live": "Live on Play Store",
        "in_review": "In Review",
        "draft": "Draft",
        "inProgress": "Rollout in Progress",
        "halted": "Rollout Halted",
    }
    android_status = android_info.get("status", "in_review")
    android_status_label = android_status_map.get(android_status, "In Review")

    # If we couldn't get detailed analytics, use the previous data as fallback
    prev_data = None
    try:
        gist_raw_url = f"https://gist.githubusercontent.com/sushantwason/{GIST_ID}/raw/data.json"
        req = urllib.request.Request(gist_raw_url + f"?_={int(time.time())}")
        with urllib.request.urlopen(req) as resp:
            prev_data = json.loads(resp.read().decode())
        print("Loaded previous gist data as fallback")
    except Exception as e:
        print(f"Could not fetch previous data: {e}", file=sys.stderr)

    # Build android_patchpal data block
    android_data = {
        "package": ANDROID_PATCHPAL_PACKAGE,
        "status": android_status,
        "statusLabel": android_status_label,
        "installs": android_info.get("installs", 0),
        "platform": "android",
        "source": android_info.get("source", "static"),
    }

    # Use previous data as base if available, update what we can
    if prev_data:
        data = prev_data
        data["lastUpdated"] = today.isoformat()
        # Update date period
        start_dt = today - timedelta(days=30)
        data["dataPeriod"] = f"{start_dt.strftime('%b %-d')} - {(today - timedelta(days=1)).strftime('%b %-d, %Y')}"
        # Update iOS app status
        if pp_live_version != "Unknown":
            data["patchpal"]["appStatus"]["liveVersion"] = pp_live_version
        if pp_next_version != "Unknown":
            data["patchpal"]["appStatus"]["nextVersion"] = pp_next_version
            data["patchpal"]["appStatus"]["nextVersionStatus"] = pp_next_status
        # Update MealSight status
        data["mealsight"]["isLive"] = ms_is_live
        if ms_status != "Unknown":
            data["mealsight"]["status"] = ms_status
        if ms_version != "Unknown":
            data["mealsight"]["version"] = f"iOS {ms_version}"
        # Always refresh snapshot24h date to yesterday (App Store Connect has ~2-3 day lag)
        data["patchpal"]["snapshot24h"]["date"] = (today - timedelta(days=1)).strftime("%b %-d")
        # Update Android: always refresh statusLabel; only override status if API source
        if "android_patchpal" not in data:
            data["android_patchpal"] = android_data
        else:
            data["android_patchpal"]["statusLabel"] = android_status_label
            data["android_patchpal"]["source"] = android_info.get("source", "static")
            if android_info.get("source") == "api":
                data["android_patchpal"]["status"] = android_status
                data["android_patchpal"]["installs"] = android_info.get("installs", 0)
    else:
        # Build from scratch with what we have
        data = {
            "lastUpdated": today.isoformat(),
            "dataPeriod": f"{(today - timedelta(days=30)).strftime('%b %-d')} - {(today - timedelta(days=1)).strftime('%b %-d, %Y')}",
            "patchpal": {
                "snapshot24h": {
                    "date": (today - timedelta(days=1)).strftime("%b %-d"),
                    "installs": 0,
                    "sessions": 0,
                    "avgOpensPerUser": 0,
                    "avgOpensChange": "0%",
                    "sessionsNote": "Opt-in Only",
                    "installsAvg7d": 0,
                    "sessionsAvg7d": 0,
                    "totalSessions": 0,
                    "activeDevices": 0,
                },
                "keyInsight": "Dashboard data will populate as analytics become available from the App Store Connect API.",
                "metrics": {
                    "impressions": {"value": 0, "change": "0%"},
                    "pageViews": {"value": 0, "change": "0%"},
                    "conversionRate": {"value": "0%", "change": "0%", "sub": "Daily Average"},
                    "totalDownloads": {"value": 0, "change": "0%"},
                    "proceeds": {"value": "$0", "change": "0%"},
                    "sessionsPerDevice": {"value": 0, "change": "0%", "sub": "Opt-in Only"},
                    "crashes": {"value": 0, "change": "0%"},
                    "retention": {"value": "---", "change": "Not enough data", "sub": "Need more opt-in users"},
                },
                "funnel": {"impressions": 0, "pageViews": 0, "downloads": 0},
                "charts": {"dates": dates, "downloads": [0]*30, "pageViews": [0]*30, "sessions": [0]*30},
                "downloadsBySource": {"labels": ["App Referrer", "App Store Search", "Web Referrer", "App Store Browse", "Other"], "data": [0,0,0,0,0]},
                "downloadsByTerritory": {"labels": ["United States", "United Kingdom", "Australia", "Canada", "India"], "data": [0,0,0,0,0]},
                "downloadsByDevice": {"labels": ["iPhone", "iPad"], "data": [0,0]},
                "appStatus": {
                    "liveVersion": pp_live_version,
                    "nextVersion": pp_next_version,
                    "nextVersionStatus": pp_next_status,
                    "platform": "iOS",
                    "primaryDevice": "iPhone",
                },
            },
            "mealsight": {
                "isLive": ms_is_live,
                "appName": ms_app_name,
                "version": f"iOS {ms_version}",
                "status": ms_status,
                "submittedDate": "Unknown",
                "draftSubmissions": "Unknown",
                "previousAttempts": "Unknown",
            },
            "android_patchpal": android_data,
            "strategy": {
                "overview": "Analytics data is being collected. Strategy recommendations will appear once sufficient data is available.",
                "immediate": [],
                "mediumTerm": [],
                "longTerm": [],
                "mealsightPreLaunch": [],
            },
        }

    return data

def update_gist(data_json):
    """Update the GitHub Gist with new data."""
    url = f"https://api.github.com/gists/{GIST_ID}"
    payload = json.dumps({
        "files": {
            "data.json": {
                "content": json.dumps(data_json, indent=2)
            }
        }
    }).encode()
    req = urllib.request.Request(url, data=payload, method="PATCH", headers={
        "Authorization": f"token {GIST_PAT}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Gist updated successfully (HTTP {resp.status})")
            return True
    except urllib.error.HTTPError as e:
        print(f"Failed to update gist: {e.code} {e.read().decode()}", file=sys.stderr)
        return False

def generate_email_html(data):
    """Generate a self-contained HTML email version of the dashboard."""
    today = datetime.now(timezone.utc)
    day_name = today.strftime("%A")
    date_str = today.strftime("%B %-d, %Y")
    updated = data.get("lastUpdated", today.isoformat())

    pp = data.get("patchpal", {})
    snap = pp.get("snapshot24h", {})
    metrics = pp.get("metrics", {})
    app_status = pp.get("appStatus", {})
    ms = data.get("mealsight", {})
    android = data.get("android_patchpal", {})
    strategy = data.get("strategy", {})

    def metric_color(change_str):
        if not change_str or change_str == "0%":
            return "#8888aa"
        if change_str.startswith("+"):
            return "#4ade80"
        if change_str.startswith("-"):
            return "#f87171"
        return "#8888aa"

    def fmt_val(v):
        if isinstance(v, (int, float)):
            return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
        return str(v)

    # Build metric rows for the KPI table
    kpi_items = [
        ("Impressions", metrics.get("impressions", {})),
        ("Page Views", metrics.get("pageViews", {})),
        ("Conversion Rate", metrics.get("conversionRate", {})),
        ("Total Downloads", metrics.get("totalDownloads", {})),
        ("Proceeds", metrics.get("proceeds", {})),
        ("Sessions / Device", metrics.get("sessionsPerDevice", {})),
        ("Crashes", metrics.get("crashes", {})),
        ("Retention", metrics.get("retention", {})),
    ]
    kpi_rows = ""
    for i in range(0, len(kpi_items), 2):
        left_name, left_data = kpi_items[i]
        left_val = fmt_val(left_data.get("value", "---"))
        left_chg = left_data.get("change", "")
        left_color = metric_color(left_chg)
        right_name, right_data = kpi_items[i + 1] if i + 1 < len(kpi_items) else ("", {})
        right_val = fmt_val(right_data.get("value", "---"))
        right_chg = right_data.get("change", "")
        right_color = metric_color(right_chg)
        kpi_rows += f"""
        <tr>
          <td style="padding:12px 16px;border-bottom:1px solid #1a1a3e;">
            <span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#8888aa;">{left_name}</span><br>
            <span style="font-size:24px;font-weight:700;color:#f0f0ff;">{left_val}</span>
            <span style="font-size:12px;color:{left_color};margin-left:8px;">{left_chg}</span>
          </td>
          <td style="padding:12px 16px;border-bottom:1px solid #1a1a3e;">
            <span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#8888aa;">{right_name}</span><br>
            <span style="font-size:24px;font-weight:700;color:#f0f0ff;">{right_val}</span>
            <span style="font-size:12px;color:{right_color};margin-left:8px;">{right_chg}</span>
          </td>
        </tr>"""

    # Strategy items (immediate priorities)
    priority_rows = ""
    for item in strategy.get("immediate", [])[:4]:
        priority_rows += f"""
        <tr>
          <td style="padding:10px 16px;border-bottom:1px solid #1a1a3e;">
            <span style="display:inline-block;background:#f87171;color:#0a0a1a;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;margin-right:8px;">HIGH</span>
            <span style="color:#f0f0ff;font-weight:600;font-size:14px;">{item.get('title', '')}</span>
            <br><span style="color:#8888aa;font-size:12px;margin-top:4px;display:inline-block;">{item.get('description', '')[:120]}</span>
          </td>
        </tr>"""

    # Funnel data
    funnel = pp.get("funnel", {})
    funnel_imp = funnel.get("impressions", 0)
    funnel_pv = funnel.get("pageViews", 0)
    funnel_dl = funnel.get("downloads", 0)

    # Bar widths for funnel (relative to impressions)
    max_funnel = max(funnel_imp, 1)
    imp_pct = 100
    pv_pct = max(int(funnel_pv / max_funnel * 100), 5) if funnel_pv else 5
    dl_pct = max(int(funnel_dl / max_funnel * 100), 5) if funnel_dl else 5

    # Android status styling
    android_status = android.get("status", "in_review")
    android_status_label = android.get("statusLabel", "In Review")
    android_installs = android.get("installs", 0)
    if android_status == "live":
        android_badge_bg = "#4ade80"
        android_badge_color = "#0a0a1a"
    elif android_status == "in_review":
        android_badge_bg = "#f59e0b"
        android_badge_color = "#0a0a1a"
    else:
        android_badge_bg = "#64748b"
        android_badge_color = "#f0f0ff"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Daily Dashboard - {date_str}</title>
</head>
<body style="margin:0;padding:0;background:#0a0a1a;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a1a;">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

  <!-- HEADER -->
  <tr><td style="background:linear-gradient(135deg,#1a1a3e,#0d0d2b);padding:28px 30px;border-radius:16px 16px 0 0;border-bottom:1px solid rgba(255,255,255,0.06);">
    <h1 style="margin:0;font-size:22px;font-weight:700;color:#7c8cf8;">Sushant's Daily Dashboard</h1>
    <p style="margin:4px 0 0;color:#8888aa;font-size:13px;">{day_name}, {date_str}</p>
    <p style="margin:2px 0 0;color:#666680;font-size:11px;">Source: App Store Connect &middot; Updated: {updated[:16].replace('T', ' ')} UTC</p>
  </td></tr>

  <!-- 24H SNAPSHOT -->
  <tr><td style="background:#0d0d20;padding:24px 30px;">
    <p style="font-size:13px;font-weight:600;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin:0 0 16px;">PatchPal iOS &mdash; Last 24h ({snap.get('date', '')})</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="25%" style="text-align:center;padding:12px 4px;background:rgba(255,255,255,0.03);border-radius:12px;">
          <span style="font-size:28px;font-weight:700;color:#f0f0ff;">{snap.get('installs', 0)}</span><br>
          <span style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8888aa;">Installs</span><br>
          <span style="font-size:10px;color:#666680;">7d avg: {snap.get('installsAvg7d', 0)}/day</span>
        </td>
        <td width="4"></td>
        <td width="25%" style="text-align:center;padding:12px 4px;background:rgba(255,255,255,0.03);border-radius:12px;">
          <span style="font-size:28px;font-weight:700;color:#f0f0ff;">{snap.get('sessions', 0)}</span><br>
          <span style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8888aa;">Sessions</span><br>
          <span style="font-size:10px;color:#666680;">7d avg: {snap.get('sessionsAvg7d', 0)}/day</span>
        </td>
        <td width="4"></td>
        <td width="25%" style="text-align:center;padding:12px 4px;background:rgba(255,255,255,0.03);border-radius:12px;">
          <span style="font-size:28px;font-weight:700;color:#f0f0ff;">{snap.get('avgOpensPerUser', 0)}</span><br>
          <span style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8888aa;">Avg Opens/User</span><br>
          <span style="font-size:10px;color:{metric_color(snap.get('avgOpensChange', ''))};">{snap.get('avgOpensChange', '')}</span>
        </td>
        <td width="4"></td>
        <td width="25%" style="text-align:center;padding:12px 4px;background:rgba(255,255,255,0.03);border-radius:12px;">
          <span style="font-size:28px;font-weight:700;color:#f0f0ff;">{snap.get('activeDevices', 0)}</span><br>
          <span style="font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8888aa;">Active Devices</span><br>
          <span style="font-size:10px;color:#666680;">{snap.get('totalSessions', 0)} total sessions</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- KEY INSIGHT -->
  <tr><td style="background:#0d0d20;padding:0 30px 20px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.15);border-radius:12px;padding:16px 20px;">
        <p style="margin:0 0 4px;font-size:13px;font-weight:700;color:#f87171;">Key Insight</p>
        <p style="margin:0;font-size:13px;color:#c0c0dd;line-height:1.5;">{pp.get('keyInsight', 'No insight available.')[:300]}</p>
      </td></tr>
    </table>
  </td></tr>

  <!-- KPI GRID -->
  <tr><td style="background:#0d0d20;padding:0 30px 24px;">
    <p style="font-size:13px;font-weight:600;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin:0 0 12px;">Key Metrics (30-day)</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:rgba(255,255,255,0.02);border-radius:12px;overflow:hidden;">
      {kpi_rows}
    </table>
  </td></tr>

  <!-- CONVERSION FUNNEL -->
  <tr><td style="background:#0d0d20;padding:0 30px 24px;">
    <p style="font-size:13px;font-weight:600;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin:0 0 12px;">Conversion Funnel</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding:6px 0;">
          <span style="color:#8888aa;font-size:11px;">Impressions</span><br>
          <div style="background:rgba(124,140,248,0.3);border-radius:6px;height:24px;width:{imp_pct}%;margin-top:4px;">
            <div style="background:#7c8cf8;border-radius:6px;height:24px;width:100%;text-align:center;line-height:24px;font-size:11px;color:#fff;font-weight:600;">{funnel_imp:,}</div>
          </div>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 0;">
          <span style="color:#8888aa;font-size:11px;">Page Views</span><br>
          <div style="background:rgba(167,139,250,0.2);border-radius:6px;height:24px;width:{pv_pct}%;margin-top:4px;">
            <div style="background:#a78bfa;border-radius:6px;height:24px;width:100%;text-align:center;line-height:24px;font-size:11px;color:#fff;font-weight:600;">{funnel_pv:,}</div>
          </div>
        </td>
      </tr>
      <tr>
        <td style="padding:6px 0;">
          <span style="color:#8888aa;font-size:11px;">Downloads</span><br>
          <div style="background:rgba(74,222,128,0.2);border-radius:6px;height:24px;width:{dl_pct}%;margin-top:4px;">
            <div style="background:#4ade80;border-radius:6px;height:24px;width:100%;text-align:center;line-height:24px;font-size:11px;color:#0a0a1a;font-weight:600;">{funnel_dl:,}</div>
          </div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- PATCHPAL iOS APP STATUS -->
  <tr><td style="background:#0d0d20;padding:0 30px 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:rgba(255,255,255,0.02);border-radius:12px;overflow:hidden;">
      <tr>
        <td style="padding:14px 16px;border-bottom:1px solid #1a1a3e;">
          <span style="color:#8888aa;font-size:11px;">PATCHPAL iOS STATUS</span><br>
          <span style="color:#f0f0ff;font-weight:600;">v{app_status.get('liveVersion', '?')}</span>
          <span style="display:inline-block;background:#4ade80;color:#0a0a1a;padding:2px 10px;border-radius:10px;font-size:10px;font-weight:700;margin-left:8px;">LIVE</span>
        </td>
        <td style="padding:14px 16px;border-bottom:1px solid #1a1a3e;">
          <span style="color:#8888aa;font-size:11px;">NEXT VERSION</span><br>
          <span style="color:#f0f0ff;font-weight:600;">v{app_status.get('nextVersion', '---')}</span>
          <span style="color:#facc15;font-size:11px;margin-left:8px;">{app_status.get('nextVersionStatus', '')}</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- PATCHPAL ANDROID -->
  <tr><td style="background:#0d0d20;padding:0 30px 24px;">
    <p style="font-size:13px;font-weight:600;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin:0 0 12px;">PatchPal Android</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:rgba(255,255,255,0.02);border-radius:12px;overflow:hidden;">
      <tr>
        <td style="padding:14px 16px;border-bottom:1px solid #1a1a3e;">
          <span style="color:#8888aa;font-size:11px;">PACKAGE</span><br>
          <span style="color:#f0f0ff;font-weight:600;font-size:13px;">com.patchpal.app</span>
          <span style="display:inline-block;background:#3b82f6;color:#fff;padding:1px 8px;border-radius:8px;font-size:9px;font-weight:700;margin-left:8px;letter-spacing:0.5px;">ANDROID</span>
        </td>
        <td style="padding:14px 16px;border-bottom:1px solid #1a1a3e;">
          <span style="color:#8888aa;font-size:11px;">STATUS</span><br>
          <span style="display:inline-block;background:{android_badge_bg};color:{android_badge_color};padding:3px 12px;border-radius:10px;font-size:11px;font-weight:700;">{android_status_label}</span>
        </td>
      </tr>
      <tr>
        <td style="padding:12px 16px;" colspan="2">
          <span style="color:#8888aa;font-size:11px;">INSTALLS</span>&nbsp;
          <span style="color:#f0f0ff;font-size:14px;font-weight:600;">{android_installs:,}</span>
          <span style="color:#64748b;font-size:11px;margin-left:12px;">Android version of PatchPal &mdash; currently in review on Google Play</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- MEALSIGHT -->
  <tr><td style="background:#0d0d20;padding:0 30px 24px;">
    <p style="font-size:13px;font-weight:600;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin:0 0 12px;">MealSight</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:rgba(255,255,255,0.02);border-radius:12px;overflow:hidden;">
      <tr>
        <td style="padding:14px 16px;">
          <span style="color:#8888aa;font-size:11px;">APP</span><br>
          <span style="color:#f0f0ff;font-weight:600;">{ms.get('appName', 'MealSight')}</span>
        </td>
        <td style="padding:14px 16px;">
          <span style="color:#8888aa;font-size:11px;">VERSION</span><br>
          <span style="color:#f0f0ff;font-weight:600;">{ms.get('version', '---')}</span>
        </td>
        <td style="padding:14px 16px;">
          <span style="color:#8888aa;font-size:11px;">STATUS</span><br>
          <span style="color:#facc15;font-weight:600;">{ms.get('status', '---')}</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- TOP PRIORITIES (only shown if items exist) -->
  {"" if not strategy.get("immediate") else f'''<tr><td style="background:#0d0d20;padding:0 30px 24px;">
    <p style="font-size:13px;font-weight:600;color:#a78bfa;text-transform:uppercase;letter-spacing:1.5px;margin:0 0 12px;">Top Priorities</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:rgba(255,255,255,0.02);border-radius:12px;overflow:hidden;">
      {priority_rows}
    </table>
  </td></tr>'''}

  <!-- FOOTER -->
  <tr><td style="background:#0d0d20;padding:20px 30px 28px;border-radius:0 0 16px 16px;text-align:center;">
    <a href="https://sushantwason.github.io/app-dashboard/" style="display:inline-block;background:rgba(124,140,248,0.15);color:#7c8cf8;padding:10px 24px;border-radius:10px;text-decoration:none;font-size:14px;font-weight:600;">View Full Dashboard &rarr;</a>
    <p style="margin:16px 0 0;color:#666680;font-size:11px;">Sent automatically by GitHub Actions</p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    return html

def main():
    print("=== Daily App Dashboard Updater ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")

    # Generate JWT
    token = generate_jwt()
    print("JWT token generated")

    # Build data
    data = build_data_json(token)
    print("Data JSON built")

    # Update gist
    success = update_gist(data)
    if success:
        print("Dashboard updated successfully!")
    else:
        print("Failed to update dashboard", file=sys.stderr)
        sys.exit(1)

    # Generate email HTML
    email_html = generate_email_html(data)
    email_path = "dashboard_email.html"
    with open(email_path, "w") as f:
        f.write(email_html)
    print(f"Email HTML written to {email_path}")

if __name__ == "__main__":
    main()
