from flask import Flask, request, jsonify
from flask_cors import CORS
import pyodbc
import os
import struct
import urllib.request
import urllib.parse
import json
import time

app = Flask(__name__)
CORS(app)

# ── Database config ────────────────────────────────────────────────────────
# AEM sales data — Azure Synapse Serverless
SYNAPSE_SERVER   = "hcmasynapse-ondemand.sql.azuresynapse.net"
SYNAPSE_DATABASE = "DBP"

# Dealer branch/location data — Azure SQL Managed Instance (FlexGen)
FLEXGEN_SERVER   = "phcmaeastushcmadb01.public.fa91ef67dad9.database.windows.net,3342"
FLEXGEN_DATABASE = "FlexGenCompanyL"

# Azure Maps key — used for geocoding dealer addresses
AZURE_MAPS_KEY = os.environ.get(
    "AZURE_MAPS_KEY",
    "D2rj4Lkx4masQx5oO7bANUDBBDm8qLSgAAbH3jW7QdyRkS4CIbcpJQQJ99BEACYeBjFTRtVWAAAgAZMP4Mo0"
)

# Nations included in the map — Latin America is excluded
ALLOWED_NATIONS = ("'United States'", "'Canada'", "'Puerto Rico'")
NATION_FILTER   = f"Nation IN ({', '.join(ALLOWED_NATIONS)})"

# In-memory geocode cache — survives for the lifetime of the app instance.
# Key: "POSTALCODE_STATE"  Value: (latitude, longitude)
_geocode_cache = {}


# ── Auth token ─────────────────────────────────────────────────────────────
def get_token():
    """Get an Azure AD access token using Service Principal credentials."""
    tenant_id     = os.environ.get("SP_TENANT_ID")
    client_id     = os.environ.get("SP_CLIENT_ID")
    client_secret = os.environ.get("SP_CLIENT_SECRET")

    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://database.windows.net/.default"
    }).encode()

    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


# ── Connections ────────────────────────────────────────────────────────────
def _build_conn_synapse(database):
    """Builds a pyodbc connection to Synapse Serverless using Azure AD token auth."""
    token        = get_token()
    token_bytes  = token.encode("UTF-16-LE")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    SQL_COPT_SS_ACCESS_TOKEN = 1256
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={SYNAPSE_SERVER};"
        f"DATABASE={database};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def _build_conn_flexgen():
    """Builds a pyodbc connection to FlexGenCompanyL on the Azure SQL Managed Instance."""
    token        = get_token()
    token_bytes  = token.encode("UTF-16-LE")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    SQL_COPT_SS_ACCESS_TOKEN = 1256
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={FLEXGEN_SERVER};"
        f"DATABASE={FLEXGEN_DATABASE};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def get_connection():
    """Connect to DBP on Synapse — AEM sales data."""
    return _build_conn_synapse(SYNAPSE_DATABASE)


def get_connection_flexgen():
    """Connect to FlexGenCompanyL — dealer branch/location data."""
    return _build_conn_flexgen()


# ── Geocoding ──────────────────────────────────────────────────────────────
def geocode_address(city, state_region, postal_code):
    """
    Geocode a dealer address using Azure Maps.
    Uses postal code as primary lookup, falls back to city + state.
    Results cached in _geocode_cache — each postal code geocoded only once.
    Returns (latitude, longitude) or (None, None).
    """
    postal = (postal_code  or '').strip()
    state  = (state_region or '').strip()
    city   = (city         or '').strip()

    cache_key = f"{postal}_{state}".upper()
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    if postal:
        query = f"{postal} {state}".strip()
    elif city and state:
        query = f"{city}, {state}"
    else:
        _geocode_cache[cache_key] = (None, None)
        return None, None

    try:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://atlas.microsoft.com/search/address/json"
            f"?api-version=1.0"
            f"&query={encoded}"
            f"&countrySet=US,CA,PR"   # US, Canada, Puerto Rico only
            f"&limit=1"
            f"&subscription-key={AZURE_MAPS_KEY}"
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        results = data.get("results", [])
        if results:
            pos = results[0]["position"]
            lat, lon = round(pos["lat"], 6), round(pos["lon"], 6)
            _geocode_cache[cache_key] = (lat, lon)
            return lat, lon

    except Exception as e:
        print(f"[Geocode] Failed for '{query}': {e}")

    _geocode_cache[cache_key] = (None, None)
    return None, None


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check — confirms API is running and shows geocode cache size."""
    return {
        "status":            "healthy",
        "service":           "aem-data-api",
        "geocode_cache_size": len(_geocode_cache)
    }, 200


@app.route("/api/testconnection", methods=["GET"])
def test_connection():
    """Tests the DBP (Synapse) database connection."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT SYSTEM_USER, CURRENT_USER")
                row = cursor.fetchone()
                return {"success": True, "system_user": row[0], "current_user": row[1]}, 200
    except Exception as e:
        return {"success": False, "error": str(e)}, 500


@app.route("/api/testconnection/flexgen", methods=["GET"])
def test_connection_flexgen():
    """Tests the FlexGenCompanyL (Managed Instance) database connection."""
    try:
        with get_connection_flexgen() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT SYSTEM_USER, CURRENT_USER, DB_NAME()")
                row = cursor.fetchone()
                return {
                    "success":      True,
                    "system_user":  row[0],
                    "current_user": row[1],
                    "database":     row[2]
                }, 200
    except Exception as e:
        return {"success": False, "error": str(e)}, 500


@app.route("/api/getAEMData", methods=["GET", "POST"])
def get_aem_data():
    """
    Returns AEM third-party retail sales data from DBP.
    Source view: silver.outbound_thirdPartyRtl

    Filters applied automatically:
      - Nation restricted to United States, Canada, Puerto Rico only
      - Optional query-string filters: state, territory, parentdealerid,
        mfg_product_name, summary, date_from, date_to

    Date filter format: YYYY-MM  (e.g. date_from=2023-01&date_to=2024-12)
    """
    try:
        # Optional filter parameters
        state            = request.args.get("state",            None)
        territory        = request.args.get("territory",        None)
        parent_dealer_id = request.args.get("parentdealerid",  None)
        product_name     = request.args.get("mfg_product_name",None)
        summary          = request.args.get("summary",          None)
        date_from        = request.args.get("date_from",        None)  # YYYY-MM
        date_to          = request.args.get("date_to",          None)  # YYYY-MM

        query = f"""
            WITH ranked AS (
                -- Step 1: Aggregate by dealer + location + product + segment
                SELECT
                    [Nation],
                    [State],
                    [County],
                    [Territory],
                    [RBM],
                    [ParentDealerId],
                    [ParentDealerName],
                    [Mfg_Product_Name],
                    [Summary],
                    CAST([Latitude]  AS FLOAT) AS Latitude,
                    CAST([Longitude] AS FLOAT) AS Longitude,
                    SUM([MFG_Quantity])         AS loc_qty,
                    COUNT(DISTINCT [Dealer_Id]) AS loc_equip,
                    MIN([ReportDate])           AS loc_first,
                    MAX([ReportDate])           AS loc_last
                FROM [silver].[outbound_totRtl]
                WHERE {NATION_FILTER}
                  AND [Latitude]  IS NOT NULL
                  AND [Longitude] IS NOT NULL
                  AND [Latitude]  <> 'NULL'
                  AND [Longitude] <> 'NULL'
                  AND ISNUMERIC([Latitude])  = 1
                  AND ISNUMERIC([Longitude]) = 1
                GROUP BY
                    [Nation],[State],[County],[Territory],[RBM],
                    [ParentDealerId],[ParentDealerName],
                    [Mfg_Product_Name],[Summary],
                    CAST([Latitude] AS FLOAT),
                    CAST([Longitude] AS FLOAT)
            ),
            dealer_totals AS (
                -- Step 2: Grand total per dealer across ALL locations/products
                SELECT
                    ParentDealerId,
                    SUM(loc_qty)   AS Total_MFG_Quantity,
                    SUM(loc_equip) AS Equipment_Count,
                    MIN(loc_first) AS First_ReportDate,
                    MAX(loc_last)  AS Last_ReportDate
                FROM ranked
                GROUP BY ParentDealerId
                HAVING SUM(loc_qty) > 0
            ),
            best_loc AS (
                -- Step 3: Pick the single best location+product+segment per dealer
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY ParentDealerId
                        ORDER BY loc_qty DESC
                    ) AS rn
                FROM ranked
            )
            -- Step 4: Join best location with grand totals
            SELECT
                b.Nation, b.State, b.County, b.Territory, b.RBM,
                b.ParentDealerId, b.ParentDealerName,
                b.Mfg_Product_Name, b.Summary,
                b.Latitude, b.Longitude,
                t.Total_MFG_Quantity,   -- grand total across ALL locations/products
                t.Equipment_Count,      -- grand total equipment across ALL locations
                t.First_ReportDate,     -- earliest transaction across ALL locations
                t.Last_ReportDate       -- latest transaction across ALL locations
            FROM best_loc b
            JOIN dealer_totals t ON b.ParentDealerId = t.ParentDealerId
            WHERE b.rn = 1
        """

        conditions = []
        params     = []

        if state:
            conditions.append("State = ?")
            params.append(state)
        if territory:
            conditions.append("Territory = ?")
            params.append(territory)
        if parent_dealer_id:
            conditions.append("ParentDealerId = ?")
            params.append(parent_dealer_id)
        if product_name:
            conditions.append("Mfg_Product_Name = ?")
            params.append(product_name)
        if summary:
            conditions.append("Summary = ?")
            params.append(summary)
        if date_from:
            # date_from = "YYYY-MM" → filter ReportDate >= first day of that month
            conditions.append("ReportDate >= ?")
            params.append(date_from + "-01")
        if date_to:
            # date_to = "YYYY-MM" → filter ReportDate <= last possible day of that month
            conditions.append("ReportDate <= ?")
            params.append(date_to + "-31")

        if conditions:
            query += " AND " + " AND ".join(conditions)

        query += " ORDER BY Total_MFG_Quantity DESC"

        with get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                columns = [col[0] for col in cursor.description]
                results = [dict(zip(columns, row)) for row in cursor.fetchall()]
                return {"success": True, "count": len(results), "results": results}, 200

    except Exception as e:
        return {"success": False, "error": str(e)}, 500


@app.route("/api/getDealerLocations", methods=["GET"])
def get_dealer_locations():
    """
    Returns active dealer branch locations from FlexGenCompanyL.dbo.ArCustomer,
    geocoded to lat/lon using Azure Maps (postal code + state).
    Restricted to North America (US, Canada, Puerto Rico).

    Geocoding is cached in memory — each unique postal code geocoded only once.
    First call may be slow (2-5 min). Subsequent calls are fast from cache.
    """
    try:
        with get_connection_flexgen() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT
                        [Customer]        AS DealerID,
                        [ShortName]       AS DealerName,
                        [MasterAccount]   AS ParentDealerId,
                        [CustomerType],
                        [Branch],
                        [Area],
                        [ShipToAddr1]     AS Address1,
                        [ShipToAddr3]     AS Address2,
                        [ShipToAddr4]     AS City,
                        [ShipToAddr5]     AS StateRegion,
                        [ShipPostalCode]  AS PostalCode
                    FROM [dbo].[ArCustomer]
                    WHERE [CustomerOnHold] = 'N'
                      AND [TermsCode]      <> 'X'
                      AND [Collections]    <> 'ALL'
                      AND [ShortName]      <> 'USED EQUIPMENT DLR'
                      AND [ShortName]      <> 'NOT A DLR'
                      AND [ShortName]      <> 'CUSTOMER DIRECT'
                      AND [ShipPostalCode] IS NOT NULL
                      AND LEN(TRIM([ShipPostalCode])) > 0
                    ORDER BY [ShortName]
                """)
                columns = [col[0] for col in cursor.description]
                rows    = cursor.fetchall()

        results       = []
        skipped       = 0
        cache_hits    = 0
        geocode_calls = 0

        for row in rows:
            record    = dict(zip(columns, row))
            postal    = (record.get('PostalCode')  or '').strip()
            state     = (record.get('StateRegion') or '').strip()
            city      = (record.get('City')        or '').strip()
            cache_key = f"{postal}_{state}".upper()
            was_cached = cache_key in _geocode_cache

            lat, lon = geocode_address(city, state, postal)

            if lat is not None and lon is not None:
                record['Latitude']  = lat
                record['Longitude'] = lon
                results.append(record)
                if was_cached: cache_hits += 1
                else:
                    geocode_calls += 1
                    time.sleep(0.05)   # Rate-limit live geocode calls
            else:
                skipped += 1

        return {
            "success":       True,
            "count":         len(results),
            "skipped":       skipped,
            "cache_hits":    cache_hits,
            "geocode_calls": geocode_calls,
            "results":       results
        }, 200

    except Exception as e:
        return {"success": False, "error": str(e)}, 500


@app.route("/api/clearGeocodeCache", methods=["POST"])
def clear_geocode_cache():
    """
    Clears the in-memory geocode cache.
    Call this if dealer addresses have changed and you need fresh geocoding.
    """
    count = len(_geocode_cache)
    _geocode_cache.clear()
    return {"success": True, "cleared": count}, 200


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
