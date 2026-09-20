import sys
from pathlib import Path

sys.path.append(str(Path.cwd()))

from scripts.linkedin_followup_runner import LinkedInSession


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_profile.py <profile_url>")
        return

    url = sys.argv[1]
    session = LinkedInSession()
    connected = session.connect(skip_rate_check=True)
    if not connected.get("ok"):
        print("Failed to connect to Chrome CDP.")
        return

    print(f"Navigating to {url} ...")
    session.cdp.navigate(url, wait_load=False, timeout=10)

    script = """
    (async () => {
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        await sleep(2500); // wait for page load

        // Target the h1 element inside the main profile section
        const nameEl = document.querySelector('h1.text-heading-xlarge, h1');
        if (nameEl) {
            return {ok: true, name: nameEl.innerText.trim()};
        }
        return {ok: false, reason: "Name element not found"};
    })();
    """

    result = session.cdp.evaluate(script, await_promise=True, timeout=15)
    print("Result:", result)
    session.disconnect()


if __name__ == "__main__":
    main()
