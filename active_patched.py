from playwright.sync_api import sync_playwright
import ntplib
from datetime import datetime, timezone, timedelta
import pytz
import time
import subprocess
import statistics

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


# Tunables
SAMPLE_COUNT = 5  # NTP samples per server
MAX_RTT_SAMPLES = 5  # measure RTT to slack a few times
SAFETY_MARGIN_MS = 5.0  # small safety margin to account for jitter


def get_ntp_offset():
    """Sync with NTP server to get accurate time offset. Tries multiple servers."""
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
                print(f"✅ NTP {server} sample {attempt+1}: offset={offset*1000:.1f}ms rtt={rtt*1000:.1f}ms")
            except Exception:
                pass
    if samples:
        # Weight offsets by RTT (lower RTT gets higher weight)
        offsets = [s[0] for s in samples]
        rtts = [s[1] for s in samples]
        median_offset = statistics.median(offsets)
        median_rtt = statistics.median(rtts)
        print(f"📊 NTP median offset={median_offset*1000:.2f}ms median_rtt={median_rtt*1000:.2f}ms")
        return median_offset

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

        # Type the first message (this one is already loaded when we fire)
        page.keyboard.type(MESSAGE, delay=20)
        print(f"Message pre-loaded: '{MESSAGE}'")

        # Measure RTT to Slack by issuing a small fetch in page context
        print("Measuring RTT to Slack via page.evaluate fetch...")
        rtt_samples = []
        for i in range(MAX_RTT_SAMPLES):
            try:
                t0 = time.time()
                # perform a lightweight request in page context to Slack's workspace URL
                page.evaluate("(url) => fetch(url, { method: 'HEAD', mode: 'no-cors' }).catch(() => null)", SLACK_WORKSPACE_URL)
                rtt = (time.time() - t0)
                rtt_samples.append(rtt)
                page.wait_for_timeout(100)
            except Exception:
                pass
        if rtt_samples:
            median_rtt = statistics.median(rtt_samples)
            print(f"📶 Measured median page RTT: {median_rtt*1000:.1f}ms")
        else:
            median_rtt = 0.2  # fallback 200ms
            print("⚠️ RTT measurement failed; using fallback 200ms")

        # ──────────────── PHASE 1: COARSE SLEEP ────────────────
        # Sleep until a few seconds before midnight (saves CPU)
        coarse_sleep = (midnight_ns - accurate_time_ns(offset_ns)) / 1e9 - max(3.0, median_rtt + 0.5)
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
        # Aim to arrive a few ms BEFORE midnight so we press Enter exactly at midnight
        target_ns = midnight_ns - int((SAFETY_MARGIN_MS / 1000.0) * 1e9)
        while accurate_time_ns(offset_ns) < target_ns:
            pass

        # ──────────────── 🔥 FIRE! 🔥 ────────────────
        # We will send exactly 2 messages. The first is fired using CDP for minimal browser overhead.
        fire_times = []

        # Prepare second message content but do not send yet
        page.evaluate("""() => {
            const editor = document.querySelector('[data-qa="message_input"] [contenteditable="true"]');
            if (editor) editor.focus();
        }""")
        # Use CDP to dispatch Enter at the precise moment
        client = browser.new_cdp_session(page)

        # Wait until the clock reaches the true midnight, then press Enter via CDP
        # Busy-wait up to midnight (already spun to just before target_ns)
        while accurate_time_ns(offset_ns) < midnight_ns:
            pass

        # Now at or after midnight_ns. Dispatch keydown + keyup for Enter immediately.
        send_ts_ns = time.time_ns() + offset_ns
        client.send("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        client.send("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        fire_times.append(send_ts_ns)

        # Small pause then send the second message (as backup) using DOM insert + CDP Enter
        page.evaluate("""() => {
            const editor = document.querySelector('[data-qa="message_input"] [contenteditable="true"]');
            if (editor) {
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, 'Active');
            }
        }""")
        time.sleep(0.010)  # 10ms gap
        send2_ts_ns = time.time_ns() + offset_ns
        client.send("Input.dispatchKeyEvent", {"type": "keyDown", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        client.send("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        fire_times.append(send2_ts_ns)

        # ──────────────── LOG RESULTS (after burst) ─────────────
        print("\n🎯 MIDNIGHT BURST COMPLETE!")
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