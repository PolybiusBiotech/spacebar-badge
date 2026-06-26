import json
import math
import time

import wifi
from qr import make as _qr_make
from app import App
from app_components import clear_background
from events.input import BUTTON_TYPES, Buttons

# ---------------------------------------------------------------------------
# Config — fill in before deploying
# ---------------------------------------------------------------------------
TILLWEB_BASE_URL = "https://bar.emf.camp"
KIOSK_TOKEN = "OlWh4o3Vny-1WtnLXo0B12VfZh4IgrD-bsYZquiffOw"
LOCATION = "Spacebar"
LOCATION_DISPLAY = "Space BAR"            # shown in menu header (logo TBD)
OMS_BASE_URL = "http://127.0.0.1:8081"    # OMS device on local WiFi

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------
RADIUS  = 120
QR_MAX  = 200
BG      = (0.0, 0.059, 0.0)    # #000F00 dark green-black — kiosk background

# Color palette — mirrors the kiosk Polybius theme
C_TITLE  = (0.486, 0.757, 0.259)  # #7CC142 lime green    — headings, order ref
C_BODY   = (0.91,  1.0,   0.91)   # #e8ffe8 light green   — body copy
C_SELECT = (0.486, 0.757, 0.259)  # #7CC142 lime green    — selected menu item
C_ACCENT  = (0.745, 0.0,   0.996)  # #BE00FE magenta       — prices, CTAs, ">"
C_GRAFFITI = (0.988, 0.639, 0.067) # #FCA311 orange        — "Space" spray-paint
C_INFO   = (0.486, 0.757, 0.259)  # lime green            — status text
C_OK     = (0.486, 0.757, 0.259)  # lime green            — countdown, success
C_WARN   = (0.976, 0.886, 0.0)    # #F9E200 amber         — in-progress states
C_ERR    = (0.969, 0.498, 0.008)  # #F77F02 orange-red    — errors, expired
C_MUTED  = (0.29,  0.478, 0.29)   # #4a7a4a muted green   — secondary / inactive

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

EXPIRY_S          = 120
STATUS_POLL_S     = 5    # seconds between OMS status polls
OMS_POLL_FAIL_MAX = 5    # consecutive OMS poll failures before surfacing an error


# ---------------------------------------------------------------------------
# NavMenu — themed scrolling list; no framework event handlers
# ---------------------------------------------------------------------------
class NavMenu:
    """Vertically-scrolling selection list styled to match the app theme.

    Handles its own button input via handle_buttons(); no async event
    handlers registered, so no cleanup step needed.
    """

    LINE_H   = 40    # px between rows
    VISIBLE  = 3     # max rows shown at once
    FONT     = "Camp Font 2"
    FONT_SZ  = 28
    IND_X    = -RADIUS + 22   # x position of the ">" selection indicator
    Y_OFFSET = 16    # shift menu centre down so header has room

    def __init__(self, items, *, on_select, on_back=None):
        self.items    = items
        self.on_select = on_select
        self.on_back  = on_back
        self._idx    = 0
        self._scroll = 0

    def handle_buttons(self, button_states):
        """Process nav buttons. Returns True if a button was consumed."""
        if button_states.get(BUTTON_TYPES["UP"]):
            button_states.clear()
            if self._idx > 0:
                self._idx -= 1
                self._clamp_scroll()
            return True
        if button_states.get(BUTTON_TYPES["DOWN"]):
            button_states.clear()
            if self._idx < len(self.items) - 1:
                self._idx += 1
                self._clamp_scroll()
            return True
        if button_states.get(BUTTON_TYPES["CONFIRM"]):
            button_states.clear()
            if self.items:
                self.on_select(self.items[self._idx], self._idx)
            return True
        if button_states.get(BUTTON_TYPES["CANCEL"]):
            button_states.clear()
            if self.on_back:
                self.on_back()
            return True
        return False

    def _clamp_scroll(self):
        if self._idx < self._scroll:
            self._scroll = self._idx
        elif self._idx >= self._scroll + self.VISIBLE:
            self._scroll = self._idx - self.VISIBLE + 1

    def draw(self, ctx):
        n = min(self.VISIBLE, len(self.items))
        start_y = -(n * self.LINE_H) // 2 + self.LINE_H // 2 + self.Y_OFFSET

        # Scroll-up indicator
        if self._scroll > 0:
            ctx.font = "Arimo Regular"
            ctx.font_size = 16
            ctx.rgb(*C_MUTED)
            ctx.move_to(0, start_y - self.LINE_H).text(". . .")

        for i in range(n):
            real_idx = self._scroll + i
            if real_idx >= len(self.items):
                break
            selected = real_idx == self._idx
            y = start_y + i * self.LINE_H

            if selected:
                ctx.rgb(*C_ACCENT)  # magenta ">" indicator
                ctx.font = self.FONT
                ctx.font_size = self.FONT_SZ
                ctx.move_to(self.IND_X, y).text(">")
                ctx.rgb(*C_SELECT)  # lime green for selected text
            else:
                ctx.rgb(*C_MUTED)

            ctx.font = self.FONT
            ctx.font_size = self.FONT_SZ
            ctx.move_to(0, y).text(self.items[real_idx])

        # Scroll-down indicator
        if self._scroll + n < len(self.items):
            ctx.font = "Arimo Regular"
            ctx.font_size = 16
            ctx.rgb(*C_MUTED)
            ctx.move_to(0, start_y + n * self.LINE_H).text(". . .")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class SpaceBarApp(App):

    def __init__(self):
        self.button_states = Buttons(self)
        self.state    = S_WIFI
        self.error_msg = ""
        self.menu     = None

        self.categories   = []
        self.items_by_cat = {}
        self.current_cat  = None

        self.basket = {}

        self.order_ref       = ""
        self.barcode         = ""
        self.qr_rows         = []
        self.order_placed_at = 0
        self._show_bill      = False
        self._qr_expired     = False
        self._last_status_check = 0.0

        self._bg_result    = None
        self._bg_error     = None
        self._poll_fails   = 0

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
        # Consume background results first
        if self._bg_result is not None:
            result, self._bg_result = self._bg_result, None
            self._handle_bg_result(result)
            return
        if self._bg_error is not None:
            msg, self._bg_error = self._bg_error, None
            self.state = S_ERROR
            self.error_msg = msg
            return

        # QR expiry + B-button toggle
        if self.state == S_QR:
            elapsed = time.ticks_ms() / 1000.0 - self.order_placed_at
            if elapsed >= EXPIRY_S and not self._qr_expired:
                self._qr_expired = True
                self._show_bill = True
            if not self._qr_expired and self.button_states.get(BUTTON_TYPES["RIGHT"]):
                self.button_states.clear()
                self._show_bill = not self._show_bill

        # LED flash for collect state
        if self.state == S_COLLECT:
            flash = (int(time.ticks_ms() / 500) % 2) == 0
            try:
                import tildagonos
                color = (0, 200, 50) if flash else (0, 0, 0)
                for i in range(12):
                    tildagonos.leds[i] = color
                tildagonos.leds.write()
            except Exception:
                pass

        # Menu navigation (NavMenu handles its own buttons)
        if self.menu is not None:
            self.menu.handle_buttons(self.button_states)
            return

        # Block cancel/back in these states
        if self.state in (S_WIFI, S_LOADING, S_ORDERING, S_CANCELLING, S_PROCESSING):
            return

        # F / Cancel when no menu active
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
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
                self._bg_result = ("connected", None)
        except Exception as e:
            self._bg_error = str(e)[:60]

    def _bg_fetch_stocklines(self):
        try:
            import urequests
            url = f"{TILLWEB_BASE_URL}/api/stocklines.json?location={LOCATION}"
            resp = urequests.get(url, headers={"Authorization": f"Bearer {KIOSK_TOKEN}"})
            try:
                data = json.loads(resp.content)
            finally:
                resp.close()
            self._bg_result = ("stocklines", data)
        except Exception as e:
            self._bg_error = f"Menu load failed\n{str(e)[:40]}"

    def _bg_place_order(self):
        try:
            import urequests
            items = [
                {"stockline_id": sid, "qty": info["qty"]}
                for sid, info in self.basket.items()
            ]
            body = json.dumps({"location": LOCATION, "items": items})
            resp = urequests.post(
                f"{TILLWEB_BASE_URL}/api/kiosk/orders",
                data=body,
                headers={
                    "Authorization": f"Bearer {KIOSK_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            try:
                data = json.loads(resp.content)
            finally:
                resp.close()
            self._bg_result = ("order", data)
        except Exception as e:
            self._bg_error = f"Order failed\n{str(e)[:40]}"

    def _bg_cancel_order(self):
        try:
            import urequests
            resp = urequests.request(
                "DELETE",
                f"{TILLWEB_BASE_URL}/api/kiosk/orders/{self.order_ref}",
                headers={
                    "Authorization": f"Bearer {KIOSK_TOKEN}",
                    "Order-Barcode": self.barcode,
                },
            )
            resp.close()
        except Exception:
            pass  # best-effort — order will expire naturally if this fails
        self._bg_result = ("cancelled", None)

    def _bg_maybe_poll_status(self):
        now = time.ticks_ms() / 1000.0
        if now - self._last_status_check < STATUS_POLL_S:
            return
        self._last_status_check = now
        self._bg_poll_order_status()

    def _bg_poll_order_status(self):
        try:
            import urequests
            resp = urequests.get(f"{OMS_BASE_URL}/api/orders?order={self.order_ref}")
            try:
                data = json.loads(resp.content)
            finally:
                resp.close()
            state = data.get("order", {}).get("state")
            if state:
                self._poll_fails = 0
                self._bg_result = ("order_status", state)
            else:
                self._poll_fails += 1
        except Exception:
            self._poll_fails += 1
            if self._poll_fails >= OMS_POLL_FAIL_MAX:
                self._bg_error = "Can't reach bar\nSee staff for order"

    # ------------------------------------------------------------------
    # Background result handler
    # ------------------------------------------------------------------

    def _handle_bg_result(self, result):
        kind, data = result

        if kind == "connected":
            self.state = S_LOADING

        elif kind == "stocklines":
            lines = data.get("stocklines", [])
            by_cat = {}
            for line in lines:
                cat = line.get("department", "Other")
                if cat not in by_cat:
                    by_cat[cat] = []
                by_cat[cat].append({
                    "id":    line["id"],
                    "name":  line["name"],
                    "price": line.get("price", "?"),
                })
            self.items_by_cat = by_cat
            self.categories   = sorted(by_cat.keys())
            self._show_categories()

        elif kind == "order":
            if "error" in data:
                self.state = S_ERROR
                self.error_msg = data.get("message", data["error"])
            else:
                self.order_ref       = data.get("order_ref", "")
                self.barcode         = data.get("barcode", "")
                self.qr_rows         = _qr_make(self.barcode) if self.barcode else []
                self.order_placed_at = time.ticks_ms() / 1000.0
                self._show_bill      = False
                self._qr_expired     = False
                self._last_status_check = 0.0
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

    def _show_categories(self):
        self.current_cat = None
        self.state = S_CATEGORIES
        if not self.categories:
            self.state = S_ERROR
            self.error_msg = "No items available\nat this location"
            return
        self.menu = NavMenu(
            self.categories,
            on_select=self._cat_selected,
        )

    def _cat_selected(self, item, idx):
        self.current_cat = item
        self._show_items(item)

    def _show_items(self, category):
        self.state = S_ITEMS
        items  = self.items_by_cat.get(category, [])
        labels = [f"{i['name']}  \xa3{i['price']}" for i in items]
        self.menu = NavMenu(
            labels,
            on_select=lambda label, idx: self._item_selected(items[idx]),
            on_back=self._show_categories,
        )

    def _item_selected(self, item):
        self.basket = {item["id"]: {"name": item["name"], "price": item["price"], "qty": 1}}
        self.menu   = None
        self.state  = S_ORDERING

    def _reset_for_new_order(self):
        self._clear_leds()
        self.basket    = {}
        self.order_ref = ""
        self.barcode   = ""
        self.qr_rows   = []
        self._show_bill = False
        self._qr_expired = False
        self._last_status_check = 0.0
        self._poll_fails = 0
        if self.categories:
            self._show_categories()
        else:
            # Menu never loaded (e.g. WiFi error before fetch) — restart from scratch
            self.menu  = None
            self.state = S_WIFI

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
        ctx.text_align   = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE

        if self.state == S_WIFI:
            self._draw_status(ctx, "Joining\nrebel net…", C_OK)
        elif self.state == S_LOADING:
            self._draw_status(ctx, "Accessing\nBAR registry…", C_OK)
        elif self.state == S_ORDERING:
            self._draw_status(ctx, "Transmitting\norder…", C_WARN)
        elif self.state == S_CANCELLING:
            self._draw_status(ctx, "Voiding\ntransaction…", C_WARN)
        elif self.state == S_ERROR:
            self._draw_status(ctx, self.error_msg, C_ERR)

        elif self.state == S_CATEGORIES:
            self._draw_menu_header(ctx, LOCATION_DISPLAY)
            if self.menu:
                self.menu.draw(ctx)

        elif self.state == S_ITEMS:
            self._draw_menu_header(ctx, self.current_cat or "")
            if self.menu:
                self.menu.draw(ctx)

        elif self.state == S_QR:
            if self._show_bill:
                self._draw_bill(ctx)
            else:
                self._draw_qr(ctx)

        elif self.state == S_PROCESSING:
            self._draw_processing(ctx)

        elif self.state == S_COLLECT:
            self._draw_collect(ctx)

        ctx.restore()

    def _draw_status(self, ctx, text, color):
        """Centred status message with brand mark at top."""
        # "Space" graffiti: orange, angled, Arimo — offset-left over "BAR"
        ctx.save()
        ctx.translate(-25, -RADIUS + 42)
        ctx.rotate(-0.38)
        ctx.font = "Arimo Regular"
        ctx.font_size = 26
        ctx.rgb(*C_GRAFFITI)
        ctx.move_to(0, 0).text("Space")
        ctx.restore()
        ctx.font = "Camp Font 2"
        ctx.font_size = 26
        ctx.rgb(*C_TITLE)
        ctx.move_to(0, -RADIUS + 54).text("BAR")

        ctx.font = "Camp Font 2"
        ctx.font_size = 26
        ctx.rgb(*color)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            ctx.move_to(0, (i - len(lines) / 2 + 0.5) * 34).text(line)

    def _draw_menu_header(self, ctx, label):
        """Location or category label at the top of menu screens."""
        if label == LOCATION_DISPLAY:
            # "Space" graffiti: orange, angled, Arimo — offset-left over "BAR"
            ctx.save()
            ctx.translate(-30, -RADIUS + 44)
            ctx.rotate(-0.38)
            ctx.font = "Arimo Regular"
            ctx.font_size = 32
            ctx.rgb(*C_GRAFFITI)
            ctx.move_to(0, 0).text("Space")
            ctx.restore()
            ctx.font = "Camp Font 2"
            ctx.font_size = 34
            ctx.rgb(*C_TITLE)
            ctx.move_to(0, -RADIUS + 62).text("BAR")
        else:
            ctx.font = "Camp Font 2"
            ctx.font_size = 22
            ctx.rgb(*C_TITLE)
            ctx.move_to(0, -RADIUS + 36).text(label)

    def _draw_qr(self, ctx):
        rows = self.qr_rows
        if not rows:
            self._draw_status(ctx, "No QR data", C_ERR)
            return

        n      = len(rows)
        cell   = QR_MAX // n
        offset = -(n * cell) // 2

        ctx.rgb(1, 1, 1).rectangle(offset - 4, offset - 4, n * cell + 8, n * cell + 8).fill()
        ctx.rgb(0, 0, 0)
        for y, row in enumerate(rows):
            for x, bit in enumerate(row):
                if bit == "1":
                    ctx.rectangle(offset + x * cell, offset + y * cell, cell, cell).fill()

        self._draw_countdown(ctx)

    def _draw_bill(self, ctx):
        ctx.font = "Camp Font 1"
        ctx.font_size = 14
        ctx.rgb(*C_MUTED)
        ctx.move_to(0, -62).text("ORDER")

        ctx.font_size = 28
        ctx.rgb(*C_TITLE)  # lime green
        ctx.move_to(0, -32).text(self.order_ref)

        try:
            total = sum(
                float(info["price"]) * info["qty"]
                for info in self.basket.values()
                if info["price"] not in ("?", None)
            )
            price_str = f"\xa3{total:.2f}"
        except (ValueError, TypeError):
            price_str = "\xa3?"
        ctx.font = "Arimo Regular"
        ctx.font_size = 44
        ctx.rgb(*C_ACCENT)  # magenta price — kiosk CTA colour
        ctx.move_to(0, 14).text(price_str)

        ctx.font = "Camp Font 2"
        ctx.font_size = 16
        ctx.rgb(*C_MUTED)
        for i, info in enumerate(self.basket.values()):
            ctx.move_to(0, 46 + i * 20).text(info["name"])

        ctx.font = "Arimo Regular"
        self._draw_countdown(ctx)

    def _draw_countdown(self, ctx):
        elapsed   = time.ticks_ms() / 1000.0 - self.order_placed_at
        remaining = max(0.0, EXPIRY_S - elapsed)
        frac      = remaining / EXPIRY_S

        ctx.font = "Arimo Regular"
        if self._qr_expired:
            ctx.font_size = 16
            ctx.rgb(*C_ERR)
            ctx.move_to(0, RADIUS - 20).text("Expired")
        else:
            ctx.font_size = 16
            ctx.rgb(*C_OK)  # lime green timer text
            ctx.move_to(0, RADIUS - 20).text(f"{int(remaining)}s")
            angle = frac * 2 * math.pi
            ctx.rgb(*C_TITLE)  # lime green arc sweeping the edge
            ctx.arc(0, 0, RADIUS - 8, -math.pi / 2, -math.pi / 2 + angle, False)
            ctx.line_width = 5
            ctx.stroke()

    def _draw_processing(self, ctx):
        ctx.font = "Camp Font 1"
        ctx.font_size = 14
        ctx.rgb(*C_MUTED)
        ctx.move_to(0, -54).text("ORDER")

        ctx.font_size = 28
        ctx.rgb(*C_TITLE)  # lime green
        ctx.move_to(0, -26).text(self.order_ref)

        ctx.font = "Camp Font 2"
        ctx.font_size = 22
        ctx.rgb(*C_ACCENT)  # magenta — mirrors kiosk CTA highlight
        ctx.move_to(0, 10).text("Being prepared…")

        ctx.font_size = 16
        ctx.rgb(*C_MUTED)
        for i, info in enumerate(self.basket.values()):
            ctx.move_to(0, 40 + i * 20).text(info["name"])

    def _draw_collect(self, ctx):
        flash = (int(time.ticks_ms() / 500) % 2) == 0

        # Pulsing magenta ring around the bezel — more legible than a fill on round screen
        if flash:
            ctx.rgb(*C_ACCENT)
            ctx.arc(0, 0, RADIUS - 5, 0, 2 * math.pi, False)
            ctx.line_width = 10
            ctx.stroke()

        ctx.font = "Camp Font 1"
        ctx.font_size = 52
        ctx.rgb(*C_ACCENT)  # magenta — kiosk checkout button colour
        ctx.move_to(0, -10).text("COLLECT")

        ctx.font = "Camp Font 2"
        ctx.font_size = 24
        ctx.rgb(*C_TITLE)  # lime green order ref
        ctx.move_to(0, 40).text(self.order_ref)


__app_export__ = SpaceBarApp
