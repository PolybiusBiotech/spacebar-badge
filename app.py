import json
import math
import time

import wifi
from app import App
from app_components import Menu, Notification, clear_background
from events.input import BUTTON_TYPES, Buttons

# ---------------------------------------------------------------------------
# Config — fill in before deploying
# ---------------------------------------------------------------------------
TILLWEB_BASE_URL = "https://bar.emf.camp"
KIOSK_TOKEN = "<badge-app-public-token>"   # matches emftillweb.toml badge token
LOCATION = "Spacebar"
OMS_BASE_URL = "http://127.0.0.1:8081"    # OMS device on local WiFi

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------
RADIUS = 120
QR_MAX = 200
BG = (0.0, 0.05, 0.2)

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
S_WIFI        = "wifi"
S_LOADING     = "loading"
S_CATEGORIES  = "categories"
S_ITEMS       = "items"
S_ORDERING    = "ordering"
S_QR          = "qr"
S_CANCELLING  = "cancelling"   # user left QR — voiding order
S_PROCESSING  = "processing"   # paid — being made
S_COLLECT     = "collect"      # ready — come get it
S_ERROR       = "error"

EXPIRY_S = 120
STATUS_POLL_S = 5   # seconds between OMS status polls


class SpaceBarApp(App):

    def __init__(self):
        self.button_states = Buttons(self)
        self.state = S_WIFI
        self.error_msg = ""
        self.notification = None
        self.menu = None
        self._stale_menu = None

        self.categories = []
        self.items_by_cat = {}
        self.current_cat = None

        self.basket = {}

        self.order_ref = ""
        self.barcode = ""
        self.qr_rows = []
        self.order_placed_at = 0
        self._show_bill = False
        self._qr_expired = False
        self._last_status_check = 0.0

        self._bg_result = None
        self._bg_error = None

    # ------------------------------------------------------------------
    # App lifecycle
    # ------------------------------------------------------------------

    def background_update(self, delta):
        if self._bg_result is not None or self._bg_error is not None:
            return
        if self.state == S_WIFI:
            self._bg_connect_wifi()
        elif self.state == S_LOADING:
            self._bg_fetch_stocklines()
        elif self.state == S_ORDERING:
            self._bg_place_order()
        elif self.state == S_CANCELLING:
            self._bg_cancel_order()
        elif self.state in (S_QR, S_PROCESSING):
            self._bg_maybe_poll_status()

    def update(self, delta):
        if self._stale_menu is not None:
            self._stale_menu._cleanup()
            self._stale_menu = None

        if self._bg_result is not None:
            result, self._bg_result = self._bg_result, None
            self._handle_bg_result(result)
            return
        if self._bg_error is not None:
            msg, self._bg_error = self._bg_error, None
            self.state = S_ERROR
            self.error_msg = msg
            return

        if self.state == S_QR:
            elapsed = time.ticks_ms() / 1000.0 - self.order_placed_at
            if elapsed >= EXPIRY_S and not self._qr_expired:
                self._qr_expired = True
                self._show_bill = True
            if not self._qr_expired and self.button_states.get(BUTTON_TYPES["RIGHT"]):
                self.button_states.clear()
                self._show_bill = not self._show_bill

        if self.state == S_COLLECT:
            t = time.ticks_ms() / 1000.0
            flash = (int(t * 2) % 2) == 0
            try:
                import tildagonos
                color = (0, 200, 50) if flash else (0, 0, 0)
                for i in range(12):
                    tildagonos.leds[i] = color
                tildagonos.leds.write()
            except Exception:
                pass

        if self.menu:
            self.menu.update(delta)
        if self.notification:
            self.notification.update(delta)
            if self.notification._is_closed():
                self.notification = None

        if self.state in (S_WIFI, S_LOADING, S_ORDERING, S_CANCELLING, S_PROCESSING):
            return

        if self.menu is None and self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            if self.state == S_QR and not self._qr_expired and self.barcode:
                self.state = S_CANCELLING
            else:
                self._reset_for_new_order()

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def _bg_connect_wifi(self):
        try:
            if not wifi.status():
                wifi.connect()
            if wifi.status():
                self.state = S_LOADING
        except Exception as e:
            self._bg_error = f"WiFi error: {e}"

    def _bg_fetch_stocklines(self):
        try:
            import urequests
            url = f"{TILLWEB_BASE_URL}/api/stocklines.json?location={LOCATION}"
            resp = urequests.get(url, headers={"Authorization": f"Bearer {KIOSK_TOKEN}"})
            data = json.loads(resp.content)
            self._bg_result = ("stocklines", data)
        except Exception as e:
            self._bg_error = f"Failed to load menu: {e}"

    def _bg_place_order(self):
        try:
            import urequests
            items = [
                {"stockline_id": sid, "qty": info["qty"]}
                for sid, info in self.basket.items()
            ]
            body = json.dumps({
                "location": LOCATION,
                "items": items,
            })
            resp = urequests.post(
                f"{TILLWEB_BASE_URL}/api/kiosk/orders.json",
                data=body,
                headers={
                    "Authorization": f"Bearer {KIOSK_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            data = json.loads(resp.content)
            self._bg_result = ("order", data)
        except Exception as e:
            self._bg_error = f"Order failed: {e}"

    def _bg_maybe_poll_status(self):
        now = time.ticks_ms() / 1000.0
        if now - self._last_status_check < STATUS_POLL_S:
            return
        self._last_status_check = now
        self._bg_poll_order_status()

    def _bg_cancel_order(self):
        try:
            import urequests
            body = json.dumps({"order_ref": self.order_ref, "barcode": self.barcode})
            urequests.post(
                f"{TILLWEB_BASE_URL}/api/kiosk/orders/cancel.json",
                data=body,
                headers={
                    "Authorization": f"Bearer {KIOSK_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
        except Exception:
            pass  # best-effort — order will expire naturally if this fails
        self._bg_result = ("cancelled", None)

    def _bg_poll_order_status(self):
        try:
            import urequests
            resp = urequests.get(f"{OMS_BASE_URL}/api/orders?order={self.order_ref}")
            data = json.loads(resp.content)
            state = data.get("order", {}).get("state")
            if state:
                self._bg_result = ("order_status", state)
        except Exception:
            pass  # silent — polling failures don't crash the app

    # ------------------------------------------------------------------
    # Background result handler
    # ------------------------------------------------------------------

    def _handle_bg_result(self, result):
        kind, data = result

        if kind == "stocklines":
            lines = data.get("stocklines", [])
            by_cat = {}
            for line in lines:
                cat = line.get("department", "Other")
                if cat not in by_cat:
                    by_cat[cat] = []
                by_cat[cat].append({
                    "id": line["id"],
                    "name": line["name"],
                    "price": line.get("price", "?"),
                })
            self.items_by_cat = by_cat
            self.categories = sorted(by_cat.keys())
            self._show_categories()

        elif kind == "order":
            if "error" in data:
                self.state = S_ERROR
                self.error_msg = data.get("message", data["error"])
            else:
                self.order_ref = data.get("order_ref", "")
                self.barcode = data.get("barcode", "")
                self.qr_rows = data.get("qr_rows", [])
                self.order_placed_at = time.ticks_ms() / 1000.0
                self._show_bill = False
                self._qr_expired = False
                self._last_status_check = 0.0  # poll immediately on next cycle
                self.state = S_QR

        elif kind == "cancelled":
            self._reset_for_new_order()

        elif kind == "order_status":
            oms_state = data
            if oms_state == "processing" and self.state in (S_QR, S_PROCESSING):
                self.state = S_PROCESSING
            elif oms_state == "collect" and self.state != S_COLLECT:
                self.state = S_COLLECT
                self._set_leds_collect()

    # ------------------------------------------------------------------
    # Menu builders
    # ------------------------------------------------------------------

    def _set_menu(self, menu):
        if self.menu is not None and hasattr(self.menu, "_cleanup"):
            self._stale_menu = self.menu
        self.menu = menu

    def _show_categories(self):
        self.current_cat = None
        self.state = S_CATEGORIES
        if not self.categories:
            self.state = S_ERROR
            self.error_msg = "No items available\nat this location"
            return
        self._set_menu(Menu(
            self, self.categories,
            select_handler=self._cat_selected,
        ))

    def _cat_selected(self, item, idx):
        self.current_cat = item
        self._show_items(item)

    def _show_items(self, category):
        self.state = S_ITEMS
        items = self.items_by_cat.get(category, [])
        labels = [f"{i['name']}  £{i['price']}" for i in items]
        self._set_menu(Menu(
            self, labels,
            select_handler=lambda item, idx: self._item_selected(items[idx]),
            back_handler=self._show_categories,
        ))

    def _item_selected(self, item):
        sid = item["id"]
        self.basket = {sid: {"name": item["name"], "price": item["price"], "qty": 1}}
        self.state = S_ORDERING
        self._set_menu(None)

    def _reset_for_new_order(self):
        self._clear_leds()
        self.basket = {}
        self.order_ref = ""
        self.barcode = ""
        self.qr_rows = []
        self._show_bill = False
        self._qr_expired = False
        self._last_status_check = 0.0
        self._show_categories()

    # ------------------------------------------------------------------
    # LED helpers
    # ------------------------------------------------------------------

    def _set_leds_collect(self):
        try:
            import tildagonos
            for i in range(12):
                tildagonos.leds[i] = (0, 200, 50)
            tildagonos.leds.write()
        except Exception:
            pass

    def _clear_leds(self):
        try:
            import tildagonos
            for i in range(12):
                tildagonos.leds[i] = (0, 0, 0)
            tildagonos.leds.write()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def draw(self, ctx):
        ctx.save()
        clear_background(ctx)
        ctx.rgb(*BG).rectangle(-RADIUS, -RADIUS, RADIUS * 2, RADIUS * 2).fill()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        if self.state == S_WIFI:
            self._draw_message(ctx, "Connecting\nto WiFi…", 0.3, 0.6, 0.3)
        elif self.state == S_LOADING:
            self._draw_message(ctx, "Loading\nmenu…", 0.3, 0.6, 0.3)
        elif self.state in (S_CATEGORIES, S_ITEMS):
            if self.menu:
                self.menu.draw(ctx)
            if self.notification:
                self.notification.draw(ctx)
        elif self.state == S_ORDERING:
            self._draw_message(ctx, "Placing\norder…", 0.5, 0.4, 0.0)
        elif self.state == S_CANCELLING:
            self._draw_message(ctx, "Cancelling\norder…", 0.5, 0.4, 0.0)
        elif self.state == S_QR:
            if self._show_bill:
                self._draw_bill(ctx)
            else:
                self._draw_qr(ctx)
        elif self.state == S_PROCESSING:
            self._draw_processing(ctx)
        elif self.state == S_COLLECT:
            self._draw_collect(ctx)
        elif self.state == S_ERROR:
            self._draw_message(ctx, self.error_msg, 0.6, 0.0, 0.0)

        ctx.restore()

    def _draw_message(self, ctx, text, r, g, b):
        ctx.rgb(r, g, b)
        ctx.font_size = 22
        for i, line in enumerate(text.split("\n")):
            ctx.move_to(0, (i - len(text.split("\n")) / 2 + 0.5) * 28).text(line)

    def _draw_qr(self, ctx):
        rows = self.qr_rows
        if not rows:
            self._draw_message(ctx, "No QR data", 0.6, 0.0, 0.0)
            return

        n = len(rows)
        cell = QR_MAX // n
        offset = -(n * cell) // 2

        ctx.rgb(1, 1, 1)\
            .rectangle(offset - 4, offset - 4, n * cell + 8, n * cell + 8).fill()

        ctx.rgb(0, 0, 0)
        for y, row in enumerate(rows):
            for x, bit in enumerate(row):
                if bit == "1":
                    ctx.rectangle(
                        offset + x * cell,
                        offset + y * cell,
                        cell, cell,
                    ).fill()

        self._draw_countdown(ctx)

    def _draw_bill(self, ctx):
        ctx.font = "Camp Font 1"
        ctx.rgb(0.9, 0.9, 1.0)
        ctx.font_size = 16
        ctx.move_to(0, -55).text("ORDER")

        ctx.font_size = 20
        ctx.move_to(0, -32).text(self.order_ref)

        total = sum(
            float(info["price"]) * info["qty"]
            for info in self.basket.values()
            if info["price"] != "?"
        )
        ctx.rgb(0.4, 1.0, 0.6)
        ctx.font_size = 40
        ctx.move_to(0, 10).text(f"\xa3{total:.2f}")

        ctx.rgb(0.7, 0.7, 0.9)
        ctx.font_size = 14
        for i, info in enumerate(self.basket.values()):
            ctx.move_to(0, 36 + i * 18).text(info["name"])

        ctx.font = "Arimo Regular"
        self._draw_countdown(ctx)

    def _draw_countdown(self, ctx):
        elapsed = time.ticks_ms() / 1000.0 - self.order_placed_at
        remaining = max(0.0, EXPIRY_S - elapsed)
        frac = remaining / EXPIRY_S

        if self._qr_expired:
            ctx.rgb(0.7, 0.1, 0.1)
            ctx.font_size = 13
            ctx.move_to(0, RADIUS - 18).text("Expired")
        else:
            ctx.rgb(0.2, 0.8, 0.2)
            ctx.font_size = 14
            ctx.move_to(0, RADIUS - 18).text(f"{int(remaining)}s")
            angle = frac * 2 * math.pi
            ctx.arc(0, 0, RADIUS - 8, -math.pi / 2, -math.pi / 2 + angle, False)
            ctx.line_width = 4
            ctx.stroke()

    def _draw_processing(self, ctx):
        ctx.font = "Camp Font 1"
        ctx.rgb(0.9, 0.9, 1.0)
        ctx.font_size = 14
        ctx.move_to(0, -50).text("ORDER")
        ctx.font_size = 24
        ctx.move_to(0, -28).text(self.order_ref)
        ctx.rgb(0.4, 0.7, 1.0)
        ctx.font_size = 18
        ctx.move_to(0, 10).text("Processing...")
        ctx.rgb(0.6, 0.6, 0.8)
        ctx.font_size = 13
        for i, info in enumerate(self.basket.values()):
            ctx.move_to(0, 38 + i * 18).text(info["name"])

    def _draw_collect(self, ctx):
        t = time.ticks_ms() / 1000.0
        flash = (int(t * 2) % 2) == 0
        if flash:
            ctx.rgb(0.0, 0.6, 0.2).rectangle(-RADIUS, -RADIUS, RADIUS * 2, RADIUS * 2).fill()
        ctx.font = "Camp Font 1"
        ctx.font_size = 52
        if flash:
            ctx.rgb(1.0, 1.0, 1.0)
        else:
            ctx.rgb(0.0, 0.9, 0.3)
        ctx.move_to(0, -10).text("COLLECT")
        ctx.rgb(0.9, 0.9, 1.0)
        ctx.font_size = 20
        ctx.move_to(0, 35).text(self.order_ref)


__app_export__ = SpaceBarApp
