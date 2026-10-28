import argparse
import csv
import os
import sys
from datetime import date, timedelta
from urllib.parse import urlparse

from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    import google.auth.exceptions
except ImportError:
    pass
import pickle

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]
ROW_LIMIT = 25000  # Search Console API max rows per request
GA4_METRICS = ["sessions", "activeUsers", "screenPageViews", "engagementRate", "conversions"]


def get_credentials(client_secret_file, service_account_file, token_file):
    if service_account_file:
        return service_account.Credentials.from_service_account_file(
            service_account_file, scopes=SCOPES
        )

    creds = None
    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secret_file):
                sys.exit(
                    f"Missing OAuth client file: {client_secret_file}\n"
                    "Download it from Google Cloud Console (OAuth Client ID, "
                    "Desktop app type) or pass --service-account-file instead."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "wb") as f:
            pickle.dump(creds, f)

    return creds


def prompt_non_empty(label):
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print(f"{label} cannot be empty.")


def fetch_search_analytics(service, site_url, start_date, end_date, dimensions, search_type):
    """Fetch all rows for the date range, paginating past the 25k row limit."""
    all_rows = []
    start_row = 0

    while True:
        body = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "dimensions": dimensions,
            "type": search_type,
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "dataState": "all",
        }
        try:
            response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        except HttpError as e:
            sys.exit(f"Google API error: {e}")

        rows = response.get("rows", [])
        all_rows.extend(rows)
        print(f"  fetched {len(rows)} rows (total so far: {len(all_rows)})")

        if len(rows) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT

    return all_rows


def fetch_ga4_by_date_and_page(creds, property_id, start_date, end_date):
    """Fetch GA4 metrics grouped by (date, pagePath). Returns {(date_str, path): {metric: value}}."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Metric,
        RunReportRequest,
    )

    client = BetaAnalyticsDataClient(credentials=creds)
    result = {}
    offset = 0
    limit = 100000

    while True:
        request = RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date=start_date.isoformat(), end_date=end_date.isoformat())],
            dimensions=[Dimension(name="date"), Dimension(name="pagePath")],
            metrics=[Metric(name=m) for m in GA4_METRICS],
            limit=limit,
            offset=offset,
        )
        try:
            response = client.run_report(request)
        except Exception as e:
            sys.exit(
                f"GA4 API error: {e}\n"
                "Check that the Google Analytics Data API is enabled on the same "
                "Cloud project, and that this account/service account has at least "
                "Viewer access on the GA4 property."
            )

        for r in response.rows:
            raw_date = r.dimension_values[0].value  # YYYYMMDD
            path = r.dimension_values[1].value
            date_str = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            values = {m: v.value for m, v in zip(GA4_METRICS, r.metric_values)}
            result[(date_str, path)] = values

        print(f"  fetched {len(response.rows)} GA4 rows (total so far: {len(result)})")

        if len(response.rows) < limit:
            break
        offset += limit

    return result


def write_csv(rows, dimensions, output_path, ga4_data=None):
    header = dimensions + ["clicks", "impressions", "ctr", "position"]
    if ga4_data is not None:
        header += [f"ga_{m}" for m in GA4_METRICS]

    page_index = dimensions.index("page") if "page" in dimensions else None
    date_index = dimensions.index("date") if "date" in dimensions else None

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            keys = row.get("keys", [])
            out_row = keys + [
                row.get("clicks", 0),
                row.get("impressions", 0),
                row.get("ctr", 0),
                row.get("position", 0),
            ]

            if ga4_data is not None:
                ga_values = {}
                if page_index is not None and date_index is not None:
                    path = urlparse(keys[page_index]).path or "/"
                    ga_values = ga4_data.get((keys[date_index], path), {})
                out_row += [ga_values.get(m, 0) for m in GA4_METRICS]

            writer.writerow(out_row)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", help="Property exactly as shown in Search Console, e.g. https://www.example.com/ or sc-domain:example.com")
    parser.add_argument("--output", default=None, help="Output CSV path (default: <site>_gsc_last30days.csv)")
    parser.add_argument("--dimensions", nargs="+", default=["date", "query", "page", "country", "device"], help="Dimensions to break rows down by (default: date query page country device)")
    parser.add_argument("--search-type", default="web", choices=["web", "image", "video", "news", "discover", "googleNews"], help="Search type (default: web)")
    parser.add_argument("--end-date", default=None, help="End date YYYY-MM-DD (default: today - 3 days, to avoid incomplete data)")
    parser.add_argument("--days", type=int, default=30, help="Number of days to pull, ending at --end-date (default: 30)")
    parser.add_argument("--client-secret-file", default="client_secret.json", help="OAuth client secret JSON (Desktop app credentials)")
    parser.add_argument("--service-account-file", default=None, help="Path to a service account key JSON; if set, used instead of OAuth")
    parser.add_argument("--token-file", default="token.pickle", help="Where to cache the OAuth token (default: token.pickle)")
    parser.add_argument("--ga4-property-id", default=None, help="GA4 numeric Property ID; if set, merges in GA4 sessions/users/pageviews per date+page")
    args = parser.parse_args()

    website_name = prompt_non_empty("Website name / Search Console property")
    site = args.site or prompt_non_empty("Search Console site property")

    if args.ga4_property_id and ("page" not in args.dimensions or "date" not in args.dimensions):
        sys.exit("--ga4-property-id requires both 'page' and 'date' in --dimensions (they're in the default set).")

    end_date = date.fromisoformat(args.end_date) if args.end_date else date.today() - timedelta(days=3)
    start_date = end_date - timedelta(days=args.days - 1)

    output_path = args.output or f"{site.replace('https://', '').replace('http://', '').replace('sc-domain:', '').strip('/').replace('/', '_')}_gsc_last{args.days}days.csv"

    print(f"Website:    {website_name}")
    print(f"Site:       {site}")
    print(f"Date range: {start_date} to {end_date}")
    print(f"Dimensions: {args.dimensions}")
    print(f"Output:     {output_path}")

    creds = get_credentials(args.client_secret_file, args.service_account_file, args.token_file)
    service = build("searchconsole", "v1", credentials=creds)

    rows = fetch_search_analytics(service, site, start_date, end_date, args.dimensions, args.search_type)

    if not rows:
        print("No data returned for this range/site. Check that the account/service "
              "account has access to this property in Search Console.")
        return

    ga4_data = None
    if args.ga4_property_id:
        print(f"\nFetching GA4 data for property {args.ga4_property_id}...")
        ga4_data = fetch_ga4_by_date_and_page(creds, args.ga4_property_id, start_date, end_date)

    write_csv(rows, args.dimensions, output_path, ga4_data=ga4_data)
    print(f"\nDone. Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
