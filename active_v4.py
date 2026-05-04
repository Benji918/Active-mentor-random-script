from playwright.sync_api import sync_playwright
import ntplib
from datetime import datetime, timezone, timedelta
import pytz
import time

SLACK_WORKSPACE_URL = "https://hng-14.slack.com/archives/C0AFU2RH486"
MESSAGE = "Active"
WAT = pytz.timezone("Africa/Lagos")
NTP_SERVER = "pool.ntp.org"

# --- NTP TIME ---
def get_ntp_time():
    client = ntplib.NTPClient()
    response = client.request(NTP_SERVER, version=3, timeout=2)
    return response.tx_time  # seconds since epoch (float)

def get_next_midnight_epoch():
    now = datetime.now(WAT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return midnight.timestamp()

def run():
    # Get NTP time and calculate until next midnight
    ntp_now = get_ntp_time()
    next_midnight = get_next_midnight_epoch()
    seconds_until_midnight = next_midnight - ntp_now
    print(f"Seconds until midnight: {seconds_until_midnight:.3f}")

    # Calculate target times (in seconds from now)
    targets = [0.020, 0.010, 0.005, 0.0]  # 20ms, 10ms, 5ms, 0ms before midnight
    target_times = [seconds_until_midnight - t for t in targets]

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
        if not page.locator('[data-qa="message_input"]').first.is_visible():
            print("⚠️ Message box not found. You might need to log in manually.")
            print("--- PLEASE LOGIN IN THE BROWSER WINDOW ---")
            try:
                page.wait_for_selector('[data-qa="message_input"]', timeout=0)
                print("✅ Login detected!")
            except Exception:
                print("Browser closed or error occurred.")
                return
        message_box = page.locator('[data-qa="message_input"]').first
        # Prepare absolute target times (epoch seconds)
        abs_targets = [next_midnight - t for t in targets]
        sent = [False] * len(abs_targets)
        print("Waiting for target times to send messages...")
        while not all(sent):
            now = get_ntp_time()
            for idx, target_time in enumerate(abs_targets):
                if not sent[idx] and now >= target_time:
                    # Send message
                    message_box.click()
                    page.wait_for_timeout(100)
                    page.keyboard.press("Control+a")
                    page.wait_for_timeout(50)
                    page.keyboard.type(MESSAGE, delay=10)
                    page.keyboard.press("Enter")
                    print(f"Sent 'Active' at {datetime.now(WAT).strftime('%H:%M:%S.%f')} (target {idx+1})")
                    sent[idx] = True
            # Sleep a short time to avoid busy waiting
            time.sleep(0.001)
        print("All messages sent. Script complete.")
        page.wait_for_timeout(2000)
        print("You may close the browser or script now.")

if __name__ == "__main__":
    run()
