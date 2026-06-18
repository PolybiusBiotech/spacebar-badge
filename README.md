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
- Once paid, the badge switches to a **Processing…** screen — the QR closes
- When bar staff mark the order ready, the badge flashes green with **COLLECT**
- Press **B** on the QR screen to toggle between the QR code and an order summary (price + ref)
- Press **F** on the QR screen to cancel the order and go back to the menu

## Config (developers)

Edit the constants at the top of `app.py`:

```python
TILLWEB_BASE_URL = "https://bar.emf.camp"
KIOSK_TOKEN      = "<badge-app-public-token>"
LOCATION         = "Spacebar"          # must match emftillweb.toml token location
LOCATION_DISPLAY = "Space BAR"         # shown in menu header (logo TBD)
OMS_BASE_URL     = "http://<luke-device-ip>:8081"  # must be reachable from badge WiFi
```

After placing an order the badge polls `OMS_BASE_URL/api/orders?order=<ref>` every
5 seconds. When the OMS state changes to `processing` the QR screen closes. When it
changes to `collect` the badge flashes green and pulses the LEDs.

If the OMS is unreachable for 5 consecutive polls the badge shows **"Can't reach bar — see staff for order"** and stops polling. The order still exists at the till; staff can look it up manually.

Pressing **F** on the QR screen while the QR is still live cancels the order: the
badge POSTs `{"order_ref": ..., "barcode": ...}` to
`emftillweb/api/kiosk/orders/cancel.json`. The barcode HMAC check digit prevents
forgery — only a barcode issued by the server in a real order response is valid.

## emftillweb.toml entry

```toml
[kiosk.tokens.<public-badge-token>]
locations  = ["Spacebar"]
source     = "badge"
user       = "kiosk"
timeout    = 120        # must match EXPIRY_S in app.py
max_items  = 1          # one drink per badge order
rate_limit = 300        # 5 min between orders per IP
```

## Local dev (badge simulator)

```sh
# From the top-level repo root:
dev/sync-badge-sim.sh
cd /tmp/badge-2024-software
python3.10 -u sim/run.py spacebar_badge.SpaceBarApp
```

See [`dev/README.md`](../dev/README.md) for the full stack (mock till + OMS + sim).
