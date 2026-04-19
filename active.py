from playwright.sync_api import sync_playwright
import ntplib
from datetime import datetime, timezone, timedelta
import pytz
import time
import subprocess

SLACK_WORKSPACE_URL = "https://hng-14.slack.com/archives/C0AFU2RH486"
MESSAGE = "Active"
WAT = pytz.timezone("Africa/Lagos")

# 5 fire points spread evenly from 10ms before midnight to exact midnight
FIRE_OFFSETS_MS = [10, 7.5, 5, 2.5, 0]

NTP_SERVERS = [
    "pool.ntp.org",
    "time.google.com",
    "time.cloudflare.com",
    "time.windows.com",
    "time.apple.com",
]


def get_ntp_offset():
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
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True, text=True, timeout=2
        )
        if "yes" in result.stdout.lower():
            print("✅ System clock is NTP-synchronized via systemd-timesyncd")
            return 0.0
    except Exception:
        pass
    print("❌ WARNING: No NTP sync available. System clock may be inaccurate!")
    return 0.0


def accurate_time_ns(offset_ns):
    return time.time_ns() + offset_ns


def accurate_now(offset_ns):
    ts = accurate_time_ns(offset_ns) / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def get_next_midnight_ns(offset_ns):
    now = accurate_now(offset_ns).astimezone(WAT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return int(midnight.timestamp() * 1e9)


def warm_connection(page):
    """Send a dummy fetch to Slack's servers to keep TCP connection hot."""
    try:
        page.evaluate("""async () => {
            await fetch('https://slack.com/api/api.test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            });
        }""")
        print("🔥 Connection warmed")
    except Exception:
        pass


def inject_message(page, text):
    """Re-inject message via JS — faster than keyboard.type between sends."""
    page.evaluate(f"""() => {{
        const editor = document.querySelector(
            '[data-qa="message_input"] [contenteditable="true"]'
        );
        if (editor) {{
            editor.focus();
            document.execCommand('selectAll', false, null);
            document.execCommand('insertText', false, '{text}');
        }}
    }}""")


def run():
    ntp_offset = get_ntp_offset()
    offset_ns = int(ntp_offset * 1e9)

    midnight_ns = get_next_midnight_ns(offset_ns)

    now_ns = accurate_time_ns(offset_ns)
    secs_until = (midnight_ns - now_ns) / 1e9
    print(f"⏱ Time until midnight (WAT): {secs_until:.2f}s")
    print(f"🎯 Will send 5 messages from 10ms before midnight to exact midnight")

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
                "--disable-hang-monitor",
            ]
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        print("Loading Slack...")
        page.goto(SLACK_WORKSPACE_URL, wait_until="networkidle")

        # Login check
        if not page.locator('[data-qa="message_input"]').first.is_visible():
            print("⚠️ Please log in to Slack in the browser window...")
            try:
                page.wait_for_selector('[data-qa="message_input"]', timeout=0)
                print("✅ Login detected!")
            except Exception:
                print("Browser closed or error.")
                return

        # Pre-type message
        print("Pre-typing message...")
        message_box = page.locator('[data-qa="message_input"]').first
        message_box.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(MESSAGE, delay=20)
        print(f"✅ Message pre-loaded: '{MESSAGE}'")

        # ── Coarse sleep: sleep until 10s before first fire ──
        first_fire_ns = midnight_ns - int(max(FIRE_OFFSETS_MS) * 1_000_000)
        coarse_sleep = (first_fire_ns - accurate_time_ns(offset_ns)) / 1e9 - 10.0
        if coarse_sleep > 0:
            print(f"💤 Coarse sleeping for {coarse_sleep:.1f}s...")
            time.sleep(coarse_sleep)

        # ── Warm the TCP connection ──
        print("🔥 Warming connection...")
        warm_connection(page)

        # # ── Fine-tune: adaptive sleep down to 50ms before first fire ──
        # print("🎯 Fine-tuning...")
        # while True:
        #     remaining = (first_fire_ns - accurate_time_ns(offset_ns)) / 1e9
        #     if remaining <= 0.05:
        #         break
        #     time.sleep(remaining * 0.5)

        # ── Pre-calculate all 5 fire timestamps ──
        fire_times_ns = [
            midnight_ns - int(ms * 1_000_000)
            for ms in FIRE_OFFSETS_MS
        ]

        # ── FIRE LOOP ──
        for i, fire_ns_target in enumerate(fire_times_ns):
            # Spin lock until this specific fire time
            while accurate_time_ns(offset_ns) < fire_ns_target:
                pass

            page.keyboard.press("Enter")
            fired_at = accurate_now(offset_ns).astimezone(WAT)
            offset_from_midnight = (fire_ns_target - midnight_ns) / 1_000_000
            print(f"🚀 Message {i+1}/5 | {fired_at.strftime('%H:%M:%S.%f')} WAT | offset: {offset_from_midnight:.1f}ms")

            # Re-inject text for next send (skip after last)
            if i < len(fire_times_ns) - 1:
                inject_message(page, MESSAGE)

        page.wait_for_timeout(3000)

        print("\n✅ Done. Browser stays open — check Slack to confirm.")
        print("Press Ctrl+C to exit.")

        try:
            while True:
                if not browser.is_connected():
                    break
                page.wait_for_timeout(1000)
        except (KeyboardInterrupt, Exception):
            print("\nClosing...")


if __name__ == "__main__":
    run()