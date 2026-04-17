from playwright.sync_api import sync_playwright
import ntplib
from datetime import datetime, timezone, timedelta
import pytz
import time

SLACK_WORKSPACE_URL = "https://hng-14.slack.com/archives/C0AFU2RH486"
MESSAGE = "Active"
WAT = pytz.timezone("Africa/Lagos")

def get_ntp_offset():
    """Sync with NTP server to get accurate time offset."""
    try:
        client = ntplib.NTPClient()
        response = client.request("pool.ntp.org", version=3)
        offset = response.offset
        print(f"NTP offset: {offset:.4f}s")
        return offset
    except Exception as e:
        print(f"NTP sync failed, using system clock: {e}")
        return 0.0

def accurate_now(offset):
    """Return NTP-corrected current time."""
    return datetime.now(timezone.utc) + timedelta(seconds=offset)

def seconds_until_midnight(offset):
    """Calculate how many seconds until next midnight WAT."""
    now = accurate_now(offset).astimezone(WAT)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (midnight - now).total_seconds()

def run():
    ntp_offset = get_ntp_offset()

    secs = seconds_until_midnight(ntp_offset)
    print(f"Time until midnight (WAT): {secs:.2f}s")

    with sync_playwright() as p:
        print("Launching browser...")
        browser = p.chromium.launch_persistent_context(
            user_data_dir="./slack_session",
            headless=False,
            args=["--disable-extensions", "--no-sandbox"]
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        print("Loading Slack...")
        page.goto(SLACK_WORKSPACE_URL)
        
        # Check if we need to log in
        if not page.locator('[data-qa="message_input"]').first.is_visible():
            print("⚠️ Message box not found. You might need to log in manually.")
            print("--- PLEASE LOGIN IN THE BROWSER WINDOW ---")
            # Wait until the message box appears (meaning login is successful) 
            # or the user closes the browser.
            try:
                page.wait_for_selector('[data-qa="message_input"]', timeout=0)
                print("✅ Login detected!")
            except Exception:
                print("Browser closed or error occurred.")
                return

        print("Pre-typing message into input box...")
        message_box = page.locator('[data-qa="message_input"]').first
        message_box.click()
        # page.wait_for_timeout(500)
        message_box.type(MESSAGE, delay=0)  # type instantly, no human delay
        print("Message pre-loaded. Waiting for midnight...")

        # Sleep until 200ms before midnight (give time to wake up precisely)
        pre_fire_sleep = seconds_until_midnight(ntp_offset) - 0.2
        if pre_fire_sleep > 0:
            time.sleep(pre_fire_sleep)

        # --- Tight loop: spin until exact midnight ---
        while True:
            now = accurate_now(ntp_offset).astimezone(WAT)
            if now.hour == 0 and now.minute == 0 and now.second == 0:
                page.keyboard.press("Enter")
                fired_at = accurate_now(ntp_offset).astimezone(WAT)
                print(f"🚀 Message sent at {fired_at.strftime('%H:%M:%S.%f')} WAT")
                break
            time.sleep(0.0005)  # poll every 0.5ms

        page.wait_for_timeout(2000)  # wait to confirm it sent
        
        print("\n--- Task Complete ---")
        print("The browser will remain open so you can check the session.")
        print("Press Ctrl+C in this terminal or close the browser window to exit.")
        
        try:
            # Keep the script alive until the browser is actually closed by the user
            while True:
                if not browser.is_connected():
                    break
                page.wait_for_timeout(1000)
        except (KeyboardInterrupt, Exception):
            print("\nClosing session...")
            # browser.close()

if __name__ == "__main__":
    run()