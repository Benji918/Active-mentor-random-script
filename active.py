from playwright.sync_api import sync_playwright
import ntplib
from datetime import datetime, timezone, timedelta
import pytz
import time
import subprocess

SLACK_WORKSPACE_URL = "https://hng-14.slack.com/archives/C0AFU2RH486"
MESSAGE = "Active"
WAT = pytz.timezone("Africa/Lagos")
NTP_SERVERS = [
    "pool.ntp.org",
    "time.google.com",
    "time.cloudflare.com",
    "time.windows.com",
    "time.apple.com",
]


def get_ntp_offset():
    """Sync with NTP server to get accurate time offset. Tries multiple servers."""
    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        for attempt in range(3):
            try:
                response = client.request(server, version=3, timeout=2)
                offset = response.offset
                print(f"✅ NTP synced with {server} (attempt {attempt+1}): offset={offset*1000:.1f}ms")
                return offset
            except Exception:
                pass
    print("⚠️ ALL NTP servers failed! Trying chronyc/timedatectl for system time accuracy...")
    # Check if system time is NTP-synced at the OS level
    try:
        result = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized"],
                                capture_output=True, text=True, timeout=2)
        if "yes" in result.stdout.lower():
            print("✅ System clock is NTP-synchronized via systemd-timesyncd")
            return 0.0
    except Exception:
        pass
    print("❌ WARNING: No NTP sync available. System clock may be inaccurate!")
    return 0.0


def accurate_time_ns(offset_ns):
    """Return NTP-corrected time in nanoseconds since epoch."""
    return time.time_ns() + offset_ns


def accurate_now(offset_ns):
    """Return NTP-corrected current datetime."""
    ts = accurate_time_ns(offset_ns) / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def get_next_midnight_ns(offset_ns):
    """Get the exact nanosecond timestamp of the next midnight WAT."""
    now = accurate_now(offset_ns).astimezone(WAT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return int(midnight.timestamp() * 1e9)


def run():
    # ──────────────── NTP SYNC ────────────────
    ntp_offset = get_ntp_offset()
    offset_ns = int(ntp_offset * 1e9)  # Convert to nanoseconds for precision

    midnight_ns = get_next_midnight_ns(offset_ns)
    now_ns = accurate_time_ns(offset_ns)
    secs_until = (midnight_ns - now_ns) / 1e9
    print(f"Time until midnight (WAT): {secs_until:.2f}s")

    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch_persistent_context(
            user_data_dir="./slack_session",
            headless=False,
            args=[
                "--disable-extensions",
                "--no-sandbox",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ]
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        print("Loading Slack...")
        page.goto(SLACK_WORKSPACE_URL, wait_until="networkidle")

        # Check if we need to log in
        if not page.locator('[data-qa="message_input"]').first.is_visible():
            print("⚠️ Message box not found. You might need to log in manually.")
            print("--- PLEASE LOGIN IN THE BROWSER WINDOW ---")
            try:
                page.wait_for_selector('[data-qa="message_input"]', timeout=0)
                print("✅ Login detected!")
            except Exception:
                print("Browser closed or error occurred.")
                return

        # ──────────────── PRE-LOAD MESSAGE ────────────────
        print("Pre-typing message into input box...")
        message_box = page.locator('[data-qa="message_input"]').first
        message_box.click()
        page.wait_for_timeout(300)

        # Clear existing text
        page.keyboard.press("Control+a")
        page.wait_for_timeout(100)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)

        # Type the first message (this one is already loaded when we fire)
        page.keyboard.type(MESSAGE, delay=20)
        print(f"Message pre-loaded: '{MESSAGE}'")

        # ──────────────── PHASE 1: COARSE SLEEP ────────────────
        # Sleep until 3 seconds before midnight (saves CPU)
        coarse_sleep = (midnight_ns - accurate_time_ns(offset_ns)) / 1e9 - 3.0
        if coarse_sleep > 0:
            print(f"Coarse sleeping for {coarse_sleep:.1f}s...")
            time.sleep(coarse_sleep)

        # ──────────────── PHASE 2: FINE SLEEP ────────────────
        # Sleep in smaller chunks until 50ms before midnight
        print("Fine-tuning timing...")
        while True:
            remaining = (midnight_ns - accurate_time_ns(offset_ns)) / 1e9
            if remaining <= 0.05:  # 50ms before midnight
                break
            # Sleep for half the remaining time (converges quickly)
            time.sleep(remaining * 0.5)

        # ──────────────── PHASE 3: BUSY SPIN (last ~50ms) ────────────────
        # Ultra-tight spin for the final stretch - minimal work per iteration
        while accurate_time_ns(offset_ns) < midnight_ns:
            pass  # Pure spin, no datetime conversion, no timezone, just integer compare

        # ──────────────── 🔥 FIRE! 🔥 ────────────────
        # Message 1: Already typed, just press Enter
        fire_times = []

        # Fastest possible Enter: use CDP directly
        page.keyboard.press("Enter")
        fire_times.append(time.time_ns() + offset_ns)

        # Messages 2-4: Type + Enter as fast as possible
        for i in range(3):
            page.evaluate("""() => {
                const editor = document.querySelector('[data-qa="message_input"] [contenteditable="true"]');
                if (editor) {
                    editor.focus();
                    document.execCommand('insertText', false, 'Active');
                }
            }""")
            page.keyboard.press("Enter")
            fire_times.append(time.time_ns() + offset_ns)

        # ──────────────── LOG RESULTS (after burst) ────────────────
        print(f"\n🎯 MIDNIGHT BURST COMPLETE!")
        for i, ft in enumerate(fire_times, 1):
            ts = datetime.fromtimestamp(ft / 1e9, tz=WAT)
            delta_ms = (ft - midnight_ns) / 1e6
            print(f"🚀 Message {i} sent at {ts.strftime('%H:%M:%S.%f')} WAT (midnight +{delta_ms:.1f}ms)")

        page.wait_for_timeout(2000)  # wait to confirm it sent

        print("\n--- Task Complete ---")
        print("The browser will remain open so you can check the session.")
        print("Press Ctrl+C in this terminal or close the browser window to exit.")

        try:
            while True:
                if not browser.is_connected():
                    break
                page.wait_for_timeout(1000)
        except (KeyboardInterrupt, Exception):
            print("\nClosing session...")


if __name__ == "__main__":
    run()