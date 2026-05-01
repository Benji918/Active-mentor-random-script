from playwright.sync_api import sync_playwright
import ntplib
from datetime import datetime, timezone, timedelta
import pytz
import time
import subprocess
import statistics
import os
import sys
import json
import email.utils

try:
    import requests
except Exception:
    requests = None

# Configuration
SLACK_WORKSPACE_URL = os.environ.get("SLACK_URL", "https://hng-14.slack.com/archives/C0AFU2RH486")
MESSAGE = os.environ.get("ACTIVE_MESSAGE", "Active")
WAT = pytz.timezone(os.environ.get("WAT_ZONE", "Africa/Lagos"))
NTP_SERVERS = [
    "pool.ntp.org",
    "time.google.com",
    "time.cloudflare.com",
    "time.windows.com",
    "time.apple.com",
]

# Tunables
SAMPLE_COUNT = 5
MAX_RTT_SAMPLES = 5
SAFETY_MARGIN_MS = float(os.environ.get("SAFETY_MARGIN_MS", 5.0))


def get_ntp_offset():
    """Return median NTP offset (seconds) relative to system clock."""
    client = ntplib.NTPClient()
    samples = []
    for server in NTP_SERVERS:
        for attempt in range(SAMPLE_COUNT):
            try:
                start = time.time()
                response = client.request(server, version=3, timeout=2)
                rtt = (time.time() - start)
                offset = response.offset
                samples.append((offset, rtt))
            except Exception:
                pass
    if samples:
        offsets = [s[0] for s in samples]
        return statistics.median(offsets)

    # fallback: systemctl timedatectl check
    try:
        result = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized"],
                                capture_output=True, text=True, timeout=2)
        if "yes" in result.stdout.lower():
            return 0.0
    except Exception:
        pass
    return 0.0


def parse_channel_id_from_url(url):
    # expect something like https://.../archives/C01234567
    try:
        parts = url.rstrip("/").split("/")
        idx = parts.index("archives")
        return parts[idx + 1]
    except Exception:
        return None


def get_slack_server_offset_with_token(token, channel_id, samples=5):
    """Use Slack Web API to estimate server clock offset (seconds).

    Returns median offset = slack_server_time - local_time.
    """
    if not requests:
        raise RuntimeError("requests not available; set SLACK_TOKEN or install requests")

    url = "https://slack.com/api/conversations.history"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "limit": 1}
    samples_out = []
    for i in range(samples):
        try:
            t0 = time.time()
            resp = requests.get(url, headers=headers, params=params, timeout=5)
            rtt = time.time() - t0
            # Slack servers typically set the Date header
            date_hdr = resp.headers.get("Date")
            if date_hdr:
                server_dt = email.utils.parsedate_to_datetime(date_hdr).astimezone(timezone.utc)
                server_ts = server_dt.timestamp()
                # approximate server time at request start by subtracting half RTT
                approx_server_now = server_ts - (rtt / 2.0)
                local_now = time.time()
                offset = approx_server_now - local_now
                samples_out.append((offset, rtt))
        except Exception:
            pass

    if samples_out:
        offsets = [s[0] for s in samples_out]
        return statistics.median(offsets)
    raise RuntimeError("Failed to sample Slack server time via Web API")


def get_slack_offset_via_browser(page, channel_id, samples=5):
    """Fallback: use page.evaluate to call Slack internal endpoint and estimate offset.

    This may be less accurate because response headers may be inaccessible.
    We attempt to use response Date header; if unavailable we infer offset from message ts.
    """
    samples_out = []
    for i in range(samples):
        try:
            # Use fetch to call Slack internal conversations.history
            script = f"""
            (async () => {{
                try {{
                    const res = await fetch('/api/conversations.history?channel={channel_id}&limit=1', {{ method: 'GET' }});
                    const body = await res.json().catch(() => null);
                    const date = res.headers.get('date');
                    return {{body, date}};
                }} catch (e) {{ return {{body:null,date:null}}; }}
            }})();
            """
            t0 = time.time()
            result = page.evaluate(script)
            rtt = time.time() - t0
            date_hdr = result.get("date") if isinstance(result, dict) else None
            body = result.get("body") if isinstance(result, dict) else None
            if date_hdr:
                server_dt = email.utils.parsedate_to_datetime(date_hdr).astimezone(timezone.utc)
                server_ts = server_dt.timestamp()
                approx_server_now = server_ts - (rtt / 2.0)
                offset = approx_server_now - time.time()
                samples_out.append((offset, rtt))
            elif body and isinstance(body, dict) and body.get('messages'):
                # Use message ts (string like '169...000.000') and assume message was recent
                msg_ts = float(body['messages'][0].get('ts', 0.0))
                # estimate server time as message ts + half RTT
                approx_server_now = msg_ts + (rtt / 2.0)
                offset = approx_server_now - time.time()
                samples_out.append((offset, rtt))
        except Exception:
            pass
        time.sleep(0.2)

    if samples_out:
        offsets = [s[0] for s in samples_out]
        return statistics.median(offsets)
    raise RuntimeError("Failed to sample Slack server time via browser")


def accurate_time_ns(offset_s):
    return time.time_ns() + int(offset_s * 1e9)


def accurate_now(offset_s):
    ts = accurate_time_ns(offset_s) / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def get_next_midnight_ns(offset_s):
    now = accurate_now(offset_s).astimezone(WAT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    # convert midnight WAT to UTC nanoseconds
    midnight_utc = midnight.astimezone(timezone.utc)
    return int(midnight_utc.timestamp() * 1e9)


def run():
    ntp_offset = get_ntp_offset()
    print(f"NTP offset: {ntp_offset*1000:.2f} ms")

    channel_id = parse_channel_id_from_url(SLACK_WORKSPACE_URL)
    if not channel_id:
        print("Failed to parse channel ID from SLACK_WORKSPACE_URL. Exiting.")
        return

    slack_token = os.environ.get('SLACK_TOKEN')
    slack_offset = None

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(user_data_dir="./slack_session", headless=False,
                                                       args=["--disable-extensions", "--no-sandbox"])
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(SLACK_WORKSPACE_URL, wait_until='networkidle')

        # Ensure logged in
        if not page.locator('[data-qa="message_input"]').first.is_visible():
            print("Please log in to Slack in the opened browser window.")
            try:
                page.wait_for_selector('[data-qa="message_input"]', timeout=0)
            except Exception:
                print("Login not detected; exiting.")
                return

        # Try using token-based method first
        if slack_token:
            try:
                slack_offset = get_slack_server_offset_with_token(slack_token, channel_id, samples=5)
                print(f"Slack server offset (via token): {slack_offset*1000:.2f} ms")
            except Exception as e:
                print("Token sampling failed, will fallback to in-browser sampling:", e)

        if slack_offset is None:
            try:
                slack_offset = get_slack_offset_via_browser(page, channel_id, samples=5)
                print(f"Slack server offset (via browser): {slack_offset*1000:.2f} ms")
            except Exception as e:
                print("Failed to sample Slack server offset:", e)
                print("Proceeding with NTP-only timing (less accurate relative to Slack server)")
                slack_offset = 0.0

        # Determine target nanosecond timestamp for midnight WAT (in UTC epoch ns)
        midnight_ns_utc = get_next_midnight_ns(ntp_offset)

        # Slack server time = local_time + slack_offset => to trigger at slack midnight, we compute
        # local_target_ns = midnight_ns_utc - slack_offset_ns
        slack_offset_ns = int(slack_offset * 1e9)
        target_local_ns = midnight_ns_utc - slack_offset_ns - int((SAFETY_MARGIN_MS / 1000.0) * 1e9)

        now_ns = accurate_time_ns(ntp_offset)
        secs_until = (target_local_ns - now_ns) / 1e9
        print(f"Local seconds until target send (adjusted for Slack server): {secs_until:.3f}s")

        # Pre-type message
        message_box = page.locator('[data-qa="message_input"]').first
        message_box.click()
        page.wait_for_timeout(200)
        page.keyboard.press("Control+a")
        page.wait_for_timeout(50)
        page.keyboard.type(MESSAGE, delay=20)

        # Coarse sleep
        if secs_until > 2.0:
            time.sleep(max(0, secs_until - 1.5))

        # Fine loop
        print("Entering fine wait loop...")
        while True:
            remaining = (target_local_ns - accurate_time_ns(ntp_offset)) / 1e9
            if remaining <= 0.02:
                break
            time.sleep(max(0.001, remaining * 0.5))

        # Busy spin final micro window
        client = browser.new_cdp_session(page)
        # Wait until local time reaches target_local_ns
        while accurate_time_ns(ntp_offset) < target_local_ns:
            pass

        # Dispatch Enter via CDP
        send_ts_ns = time.time_ns() + int(ntp_offset * 1e9)
        client.send("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        client.send("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})

        # Log result relative to midnight
        midnight_dt = datetime.fromtimestamp(midnight_ns_utc / 1e9, tz=WAT)
        delta_ms = (send_ts_ns - midnight_ns_utc) / 1e6
        print(f"Message sent at {datetime.fromtimestamp(send_ts_ns/1e9, tz=WAT).strftime('%H:%M:%S.%f')} WAT (midnight {delta_ms:+.1f} ms)")

        page.wait_for_timeout(2000)
        print("Done. Browser remains open for inspection.")
        try:
            while True:
                if not browser.is_connected():
                    break
                page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            print("Exiting...")


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        print('Error:', e)
        sys.exit(1)
