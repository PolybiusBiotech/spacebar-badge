# Space Bar badge app

Tildagon badge app for the Polybius Space Bar at EMF Camp.

Browse the menu, add items to your basket, place an order — the QR code appears on your badge screen. Hold it up to the scanner at the payment terminal.

## Setup

1. Install from the badge app store, or copy to your badge manually
2. Make sure your badge is on the EMF camp WiFi
3. Open **Space Bar** from the Apps menu

## How it works

- Orders expire after **2 minutes** — the QR is only valid while the countdown is running
- Walk up to the payment terminal, hold your badge up to the scanner
- Soft drinks (no age check needed) auto-charge; alcoholic drinks need staff ID verification

## Config (developers)

Edit the constants at the top of `app.py`:

```python
TILLWEB_BASE_URL = "https://bar.emf.camp"
KIOSK_TOKEN = "<badge-app-public-token>"
LOCATION = "Spacebar"
```

`KIOSK_TOKEN` must match a token entry in `emftillweb.toml` with `timeout = 120`.

## emftillweb.toml entry

```toml
[kiosk.tokens.<public-badge-token>]
locations = ["Spacebar"]
source = "badge-app"
timeout = 120
```
