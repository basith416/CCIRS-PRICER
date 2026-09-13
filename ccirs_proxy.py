"""
CCIRS Proxy Server
==================
Extends the existing ccil_proxy.py pattern (from the INR IRS Pricer) to serve
all three curves the CCIRS pricer needs, from a single Render/Railway deploy:

  GET /inr_irs    -> CCIL MIBOR OIS live table   (used as the "INR IRS" curve)
  GET /mod_mifor  -> CCIL MODIFIED MIFOR live table
  GET /sofr       -> BlueGamma public "3 days ago" USD SOFR swap snapshot
  GET /all        -> all three in one call (what the pricer actually calls)
  GET /raw_ccil   -> DEBUG: full raw CCIL JSON, unmodified, so you can see the
                     exact key name CCIL uses for the Modified MIFOR array and
                     lock it into MODMIFOR_KEYS below if the guess is wrong.

WHY THIS SHAPE
--------------
CCIL's "Interbank INR Interest Rate Swaps – Real Time Market Watch" page
(https://www.ccilindia.com/interbank-inr-interest-rate-swaps) renders THREE
live tables off the SAME Liferay portlet call: MIBOR OIS, Intentional Spread
Trades, and MODIFIED MIFOR. Your existing IRS Pricer proxy already fetches
this portlet and parses `resultMiborOis`. This script reuses that exact call
and additionally parses whichever key holds the Modified MIFOR rows.

I could not confirm the live JSON key name for the Modified MIFOR array from
here (the page needs a POST with session cookies to return data, which isn't
reachable from a sandboxed fetch). MODMIFOR_KEYS below lists the most likely
candidates in order and the code will use the first one that's present. If
none match, hit /raw_ccil once after deploying, find the correct key from the
printed top-level keys, and add it to MODMIFOR_KEYS[0].

BLUEGAMMA CAVEAT
----------------
BlueGamma's live SOFR swap rates are paywalled ("Unlock ->" on the public
page) — this script does NOT attempt to get live rates. It scrapes the
publicly-visible "3 days ago" column only, which is the most recent number
BlueGamma shows without a login. The pricer must tag this LAGGED, not LIVE.
Scraping this page programmatically may sit outside BlueGamma's Rate Data
Terms (https://www.bluegamma.io/legal/rate-data-terms) even though the data
itself is publicly rendered — worth a read before relying on this in
production. If you get a BlueGamma API key later, replace fetch_sofr() with
a call to https://api.bluegamma.io/v1/swap_rate and this becomes a genuine
LIVE tier instead of a 3-day-lagged one.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request
import json
import re
import os

CCIL_URL = (
    'https://www.ccilindia.com/interbank-inr-interest-rate-swaps'
    '?p_p_id=CcilRealTimeMarketWatchMainPageAjax_CcilRealTimeMarketWatchMainPageAjaxPortlet_INSTANCE_qown'
    '&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view'
    '&p_p_resource_id=mainReport&p_p_cacheability=cacheLevelPage'
)

BLUEGAMMA_URL = 'https://www.bluegamma.io/usd-swap-rates'

TENORS = ['1M', '2M', '3M', '6M', '9M', '1Y', '2Y', '3Y', '4Y', '5Y', '7Y', '10Y']

# Ordered guesses for the Modified MIFOR array's JSON key — first hit wins.
# Confirm/replace via /raw_ccil after first deploy.
MODMIFOR_KEYS = [
    'resultModMifor', 'resultMmfor', 'resultModifiedMifor',
    'resultMIFOR', 'resultMifor', 'resultMMFOR',
]
OIS_KEYS = ['resultMiborOis']


def _fetch_ccil_raw():
    req = urllib.request.Request(
        CCIL_URL, method="POST",
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://www.ccilindia.com/interbank-inr-interest-rate-swaps',
            'User-Agent': 'Mozilla/5.0',
        })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data


def _extract_rate_rows(raw_array):
    """Same row-shape used by the existing IRS pricer proxy, plus: skip any
    tenor where CCIL genuinely has no usable number today (both the live
    weighted-average and the previous-close are null/zero) — returning a
    fake 0% rate for an untraded tenor would badly distort the curve."""
    rows = []
    for r in raw_array:
        tenor = r.get('ismy_trad_mrty')
        if tenor not in TENORS:
            continue
        warr = float(r.get('ismy_drvt_warr') or 0)
        prev = float(r.get('ismy_prev_lrrt') or r.get('ismy_drvt_prcls') or 0)
        rate = warr if warr > 0 else prev
        if rate <= 0:
            continue  # no data for this tenor today — leave it out, don't fabricate 0%
        rows.append({
            'tenor': tenor,
            'rate': rate,
            'prev_close': prev if prev > 0 else None,
            'volume': float(r.get('ismy_drvt_ttrvl') or r.get('ismy_drvt_volm') or 0),
            'trades': int(r.get('ismy_drvt_notrd') or r.get('ismy_trad_cntt') or 0),
            'source': 'LIVE' if warr > 0 else 'PREV_CLOSE',
        })
    return rows


def fetch_ccil_curve(candidate_keys):
    data = _fetch_ccil_raw()
    for key in candidate_keys:
        if key in data:
            raw = data[key]
            if isinstance(raw, str):
                raw = json.loads(raw)
            rows = _extract_rate_rows(raw)
            if rows:
                return {'ok': True, 'rates': rows, 'key_used': key}
    return {'ok': False, 'error': f'none of {candidate_keys} found/populated',
            'available_keys': list(data.keys())}


def fetch_sofr_raw_html():
    """Debug helper: returns a slice of BlueGamma's actual page HTML around
    the rates table, so the real markup can be inspected and the scraper in
    fetch_sofr_3day() rewritten against it instead of guessed."""
    req = urllib.request.Request(
        BLUEGAMMA_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode('utf-8', errors='ignore')
    idx = html.find('Tenor')
    if idx == -1:
        idx = 0
    snippet = html[max(0, idx - 1000): idx + 12000]
    return {'ok': True, 'total_length': len(html), 'snippet': snippet}


def fetch_sofr_3day():
    """
    Scrapes the publicly visible '3 days ago' column from BlueGamma's USD
    swap rates page. This is NOT live — see module docstring caveat.
    Row structure (confirmed against the actual page): tenor sits in
    <a class="gr-tenor-link">1 Month</a>, followed by 5 <td> cells —
    Live (a locked button, no number), 3-days-ago, 1-week-ago, 1-month-ago,
    1-year-ago. We want the second cell (3-days-ago).
    """
    req = urllib.request.Request(
        BLUEGAMMA_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode('utf-8', errors='ignore')

    row_re = re.compile(
        r'class="gr-tenor-link">(\d+)\s*(Month|Year)</a>.*?<td[^>]*>.*?</td>\s*<td[^>]*>([\d.]+)%</td>',
        re.DOTALL)
    unit_map = {'Month': 'M', 'Year': 'Y'}
    rows = []
    for m in row_re.finditer(html):
        num, unit, rate = m.groups()
        rows.append({
            'tenor': f'{num}{unit_map[unit]}',
            'rate': float(rate),
            'source': '3-DAY LAGGED (BlueGamma public snapshot)',
        })
    if not rows:
        return {'ok': False, 'error': 'no rows parsed — BlueGamma page layout may have changed'}
    return {'ok': True, 'rates': rows, 'as_of': 'T-3 business days (public snapshot only)'}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def handle_request(self):
        path = urlparse(self.path).path
        try:
            if path == '/inr_irs':
                body = fetch_ccil_curve(OIS_KEYS)
            elif path == '/mod_mifor':
                body = fetch_ccil_curve(MODMIFOR_KEYS)
            elif path == '/sofr':
                body = fetch_sofr_3day()
            elif path == '/raw_ccil':
                body = _fetch_ccil_raw()
            elif path == '/raw_sofr':
                body = fetch_sofr_raw_html()
            elif path == '/all':
                body = {
                    'inr_irs': fetch_ccil_curve(OIS_KEYS),
                    'mod_mifor': fetch_ccil_curve(MODMIFOR_KEYS),
                    'sofr': fetch_sofr_3day(),
                }
            else:
                body = {'ok': False, 'error': 'unknown route',
                        'routes': ['/inr_irs', '/mod_mifor', '/sofr', '/all', '/raw_ccil', '/raw_sofr']}
            payload = json.dumps(body, default=str).encode()
            self.send_response(200)
        except Exception as e:
            payload = json.dumps({'ok': False, 'error': str(e)}).encode()
            self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print('CCIRS proxy running on port', port)
    HTTPServer(("", port), Handler).serve_forever()
