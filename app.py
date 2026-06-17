import asyncio
import json
import math
import time

import wifi
from app import App
from app_components import Menu, Notification, YesNoDialog, clear_background
from events.input import BUTTON_TYPES, Buttons

# ---------------------------------------------------------------------------
# Config — fill in before deploying
# ---------------------------------------------------------------------------
TILLWEB_BASE_URL = "https://bar.emf.camp"
KIOSK_TOKEN = "<badge-app-public-token>"   # matches emftillweb.toml badge token
LOCATION = "Spacebar"

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------
# Tildagon display: 240×240, centred at (0,0).
RADIUS = 120
QR_MAX = 200   # max pixels to use for QR within the circle

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
S_WIFI        = "wifi"
S_LOADING     = "loading"       # fetching stocklines
S_CATEGORIES  = "categories"
S_ITEMS       = "items"
S_BASKET      = "basket"
S_ORDERING    = "ordering"
S_QR          = "qr"
S_EXPIRED     = "expired"
S_ERROR       = "error"

EXPIRY_S = 120   # must match timeout in emftillweb.toml badge token


class SpaceBarApp(App):

    def __init__(self):
        self.button_states = Buttons(self)
        self.state = S_WIFI
        self.error_msg = ""
        self.notification = None
        self.menu = None

        # Stock data
        self.categories = []       # list of category name strings (sorted)
        self.items_by_cat = {}     # { category: [ {id, name, price}, ... ] }
        self.current_cat = None

        # Basket: { stockline_id: { name, price, qty } }
        self.basket = {}

        # Order result
        self.order_ref = ""
        self.qr_rows = []          # list of '010...' strings
        self.order_placed_at = 0   # monotonic time

        # Background task result slot
        self._bg_result = None
        self._bg_error = None

    # ------------------------------------------------------------------
    # App lifecycle
    # ------------------------------------------------------------------

    def background_update(self, delta):
        if self.state == S_WIFI:
            self._bg_connect_wifi()
        elif self.state == S_LOADING:
            self._bg_fetch_stocklines()
        elif self.state == S_ORDERING:
            self._bg_place_order()

    def update(self, delta):
        # Process background results
        if self._bg_result is not None:
            result, self._bg_result = self._bg_result, None
            self._handle_bg_result(result)
            return
        if self._bg_error is not None:
            msg, self._bg_error = self._bg_error, None
            self.state = S_ERROR
            self.error_msg = msg
            return

        # QR expiry countdown
        if self.state == S_QR:
            elapsed = time.ticks_ms() / 1000.0 - self.order_placed_at
            if elapsed >= EXPIRY_S:
                self._reset_for_new_order()

        # Tick menu and notification animations
        if self.menu:
            self.menu.update(delta)
        if self.notification:
            self.notification.update(delta)
            if self.notification._is_closed():
                self.notification = None

        # Button handling (non-menu states)
        if self.state in (S_WIFI, S_LOADING, S_ORDERING):
            return

        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._handle_cancel()

    def _handle_cancel(self):
        if self.state == S_ITEMS:
            self._show_categories()
        elif self.state == S_BASKET:
            self._show_categories()
        elif self.state in (S_QR, S_EXPIRED, S_ERROR):
            self._reset_for_new_order()
        else:
            self.minimise()

    # ------------------------------------------------------------------
    # Background tasks (blocking — called from background_update)
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
                self.qr_rows = data.get("qr_rows", [])
                self.order_placed_at = time.ticks_ms() / 1000.0
                self.state = S_QR

    # ------------------------------------------------------------------
    # Menu builders
    # ------------------------------------------------------------------

    BASKET_LABEL = "[ Basket ]"
    ORDER_LABEL  = "Place order"
    CLEAR_LABEL  = "Clear basket"

    def _set_menu(self, menu):
        if self.menu is not None and hasattr(self.menu, "_cleanup"):
            self.menu._cleanup()
        self.menu = menu

    def _show_categories(self):
        self.current_cat = None
        self.state = S_CATEGORIES
        labels = self.categories + ([self.BASKET_LABEL] if self.basket else [])
        self._set_menu(Menu(
            self, labels,
            select_handler=self._cat_selected,
            back_handler=self.minimise,
        ))

    def _cat_selected(self, item, idx):
        if item == self.BASKET_LABEL:
            self._show_basket()
            return
        self.current_cat = item
        self._show_items(item)

    def _show_items(self, category):
        self.state = S_ITEMS
        items = self.items_by_cat.get(category, [])
        labels = [f"{i['name']}  {i['price']}" for i in items]
        self._set_menu(Menu(
            self, labels,
            select_handler=lambda item, idx: self._item_selected(items[idx]),
            back_handler=self._show_categories,
        ))

    def _item_selected(self, item):
        sid = item["id"]
        if sid in self.basket:
            self.basket[sid]["qty"] += 1
        else:
            self.basket[sid] = {"name": item["name"], "price": item["price"], "qty": 1}
        self.notification = Notification(f"Added {item['name']}")

    def _show_basket(self):
        self.state = S_BASKET
        if not self.basket:
            self.notification = Notification("Basket is empty")
            self._show_categories()
            return
        lines = [
            f"{info['qty']}x {info['name']}"
            for info in self.basket.values()
        ]
        total = sum(
            float(info["price"]) * info["qty"]
            for info in self.basket.values()
            if info["price"] != "?"
        )
        lines.append(f"Total: {total:.2f}")
        lines.append(self.ORDER_LABEL)
        lines.append(self.CLEAR_LABEL)
        self._set_menu(Menu(
            self, lines,
            select_handler=self._basket_action,
            back_handler=self._show_categories,
        ))

    def _basket_action(self, item, idx):
        if item == self.ORDER_LABEL:
            self.state = S_ORDERING
            self._set_menu(None)
        elif item == self.CLEAR_LABEL:
            self.basket = {}
            self.notification = Notification("Basket cleared")
            self._show_categories()

    def _reset_for_new_order(self):
        self.basket = {}
        self.order_ref = ""
        self.qr_rows = []
        self._show_categories()

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def draw(self, ctx):
        ctx.save()
        clear_background(ctx)
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        if self.state == S_WIFI:
            self._draw_message(ctx, "Connecting\nto WiFi…", 0.3, 0.6, 0.3)

        elif self.state == S_LOADING:
            self._draw_message(ctx, "Loading\nmenu…", 0.3, 0.6, 0.3)

        elif self.state in (S_CATEGORIES, S_ITEMS, S_BASKET):
            if self.menu:
                self.menu.draw(ctx)
            if self.notification:
                self.notification.draw(ctx)

        elif self.state == S_ORDERING:
            self._draw_message(ctx, "Placing\norder…", 0.5, 0.4, 0.0)

        elif self.state == S_QR:
            self._draw_qr(ctx)

        elif self.state == S_EXPIRED:
            self._draw_message(ctx, "Order\nexpired", 0.6, 0.2, 0.0)

        elif self.state == S_ERROR:
            self._draw_message(ctx, self.error_msg, 0.6, 0.0, 0.0)

        ctx.restore()

    def _draw_message(self, ctx, text, r, g, b):
        ctx.rgb(r * 0.15, g * 0.15, b * 0.15)\
            .rectangle(-RADIUS, -RADIUS, RADIUS * 2, RADIUS * 2).fill()
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

        # White background square
        ctx.rgb(1, 1, 1)\
            .rectangle(offset - 4, offset - 4, n * cell + 8, n * cell + 8).fill()

        # QR modules
        ctx.rgb(0, 0, 0)
        for y, row in enumerate(rows):
            for x, bit in enumerate(row):
                if bit == "1":
                    ctx.rectangle(
                        offset + x * cell,
                        offset + y * cell,
                        cell, cell,
                    ).fill()

        # Countdown arc at bottom
        elapsed = time.ticks_ms() / 1000.0 - self.order_placed_at
        remaining = max(0.0, EXPIRY_S - elapsed)
        frac = remaining / EXPIRY_S
        ctx.rgb(0.2, 0.8, 0.2).font_size = 16
        ctx.move_to(0, RADIUS - 20).text(f"{int(remaining)}s")

        # Thin arc ring
        angle = frac * 2 * math.pi
        ctx.rgb(0.2, 0.8, 0.2)
        ctx.arc(0, 0, RADIUS - 8, -math.pi / 2, -math.pi / 2 + angle, False)
        ctx.line_width = 4
        ctx.stroke()


__app_export__ = SpaceBarApp
