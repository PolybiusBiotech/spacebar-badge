# Space Bar badge app

Tildagon badge app for the Polybius Space Bar at EMF Camp.

Browse the menu, select an item to place an order — the QR code appears on your badge screen. Hold it up to the scanner at the payment terminal.

This badge app is for personal/staff use, not a general public ordering channel — it should be provisioned with an ordinary kiosk-style token, not a specially restricted one.

## Setup

1. Install from the badge app store, or copy to your badge manually
2. Make sure your badge is on the EMF camp WiFi
3. Open **Space Bar** from the Apps menu

## How it works

- Orders expire after **2 minutes** — the QR is only valid while the countdown is running
- Walk up to the payment terminal, hold your badge up to the scanner
- Soft drinks (no age check needed) auto-charge; alcoholic drinks need staff ID verification
- Once paid, the badge switches to a **Being prepared…** screen — the QR closes
- When bar staff mark the order ready, the badge flashes green with **COLLECT**
- Press **B** on the QR screen to toggle between the QR code and an order summary (price + ref)
- Press **F** on the QR screen to cancel the order and go back to the menu

## Config (developers)

Edit the constants at the top of `app.py`:

```python
TILLWEB_BASE_URL = "https://bar.emf.camp"
KIOSK_TOKEN      = "OlWh4o3Vny-1WtnLXo0B12VfZh4IgrD-bsYZquiffOw"
LOCATION         = "Spacebar"          # must match the location assigned to stocklines
LOCATION_DISPLAY = "Space BAR"         # shown in menu header (logo TBD)
OMS_BASE_URL     = "http://<luke-device-ip>:8081"  # must be reachable from badge WiFi
```

**Action item:** `KIOSK_TOKEN` above is currently set to a token that was
originally issued as a specially-restricted public badge token. Per the
descoping decision (the badge is now personal/staff use only, not a public
ordering channel), this should be swapped for an ordinary kiosk-style token
provisioned the same way as any other kiosk — not left as-is.

After placing an order the badge polls `OMS_BASE_URL/api/orders?order=<ref>` every
5 seconds. When the OMS state changes to `processing` the QR screen closes. When it
changes to `collect` the badge flashes green and pulses the LEDs.

If the OMS is unreachable for 5 consecutive polls the badge shows **"Can't reach bar — see staff for order"** and stops polling. The order still exists at the till; staff can look it up manually.

Pressing **F** on the QR screen while the QR is still live cancels the order: the
badge sends an HTTP `DELETE` to `{TILLWEB_BASE_URL}/api/kiosk/orders/{order_ref}/`,
with the barcode passed in the `Order-Barcode` header (no JSON body). The server
doesn't actually check that header — cancelling just requires the bearer token and
that the order isn't already paid or in use at a till — but the badge sends it
regardless in case that changes.

## emftillweb.toml entry

The backend uses a single shared bearer token for all kiosk-style clients — no
per-client location/limits config:

```toml
[kiosk]
token_file     = "/path/to/token"   # file containing the bearer token
till_user      = 1                  # quicktill User id, not a username
barcode_secret = "<random-secret>"  # shared with quicktill-kiosk-plugin; HMAC barcode check digits
location       = "Spacebar"         # the only location orders can be placed against
```

`KIOSK_TOKEN` in `app.py` must match the contents of `token_file` above. `LOCATION`
in `app.py` isn't actually sent to the server — placement is validated against the
single `location` configured server-side. See
[`docs/till-mods.md`](../docs/till-mods.md) for the full schema (`emftillweb` itself
is locked read-only, so its own README won't reflect this).

## Local dev (badge simulator)

```sh
# From the top-level repo root:
dev/sync-badge-sim.sh
cd /tmp/badge-2024-software
python3.10 -u sim/run.py spacebar_badge.SpaceBarApp
```

See [`dev/README.md`](../dev/README.md) for the full stack (mock till + OMS + sim).
