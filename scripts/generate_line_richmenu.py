"""
generate_line_richmenu.py
─────────────────────────────────────────────────────────────────────
สร้างรูป LINE Rich Menu (2 ปุ่ม: เว็บ + คำนวณเบี้ย)

LINE spec:
  - Compact (1 row, 2-3 buttons): 2500 x 843 px
  - Large (2 rows):                2500 x 1686 px
  - Format: PNG/JPEG, ≤ 1MB

USAGE:
    python scripts/generate_line_richmenu.py
    → สร้างไฟล์ richmenu_compact.png + richmenu_large.png ใน Downloads/
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ASSETS = Path(__file__).resolve().parent.parent / "services" / "assets"
FONT_BOLD = ASSETS / "Sarabun-Bold.ttf"
FONT_REG  = ASSETS / "Sarabun-Regular.ttf"
OUT_DIR   = Path.home() / "Downloads"

# Brand palette — โทนเขียวประกันคุ้มภัย
GREEN_DARK  = (10, 150, 90)
GREEN_MID   = (15, 175, 105)
GREEN_LIGHT = (40, 200, 130)
WHITE       = (255, 255, 255)
SHADOW      = (0, 80, 50, 60)


def _round_rect(draw, xy, radius, fill, outline=None, width=0):
    """draw rounded rectangle"""
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _icon_browser(draw, cx, cy, size):
    """🌐 browser icon — กล่อง + แถบบน + จุด 3 สี"""
    w = size; h = int(size * 0.78)
    x0, y0 = cx - w//2, cy - h//2
    x1, y1 = cx + w//2, cy + h//2

    # frame
    draw.rounded_rectangle((x0, y0, x1, y1), radius=int(size*0.06),
                           outline=WHITE, width=max(4, size//30))
    # top bar
    bar_h = int(h * 0.18)
    draw.rectangle((x0, y0, x1, y0 + bar_h), fill=WHITE)
    # dots
    dot_r = max(3, bar_h // 5)
    dy = y0 + bar_h // 2
    for i, dx in enumerate([x0 + bar_h*0.7, x0 + bar_h*1.5, x0 + bar_h*2.3]):
        draw.ellipse((dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r), fill=GREEN_DARK)


def _icon_calculator(draw, cx, cy, size):
    """🧮 calculator icon — กล่อง + จอ + ปุ่ม"""
    w = int(size * 0.78); h = size
    x0, y0 = cx - w//2, cy - h//2
    x1, y1 = cx + w//2, cy + h//2

    # frame
    draw.rounded_rectangle((x0, y0, x1, y1), radius=int(size*0.06),
                           outline=WHITE, width=max(4, size//30))

    # display screen
    screen_pad = int(w * 0.10)
    screen_h   = int(h * 0.22)
    draw.rounded_rectangle((x0 + screen_pad, y0 + screen_pad,
                            x1 - screen_pad, y0 + screen_pad + screen_h),
                           radius=int(screen_h*0.15), fill=WHITE)

    # buttons grid 3x3
    grid_top    = y0 + screen_pad * 2 + screen_h
    grid_bottom = y1 - screen_pad
    grid_left   = x0 + screen_pad
    grid_right  = x1 - screen_pad
    btn_w = (grid_right - grid_left) // 3
    btn_h = (grid_bottom - grid_top) // 3
    pad   = max(2, btn_w // 8)
    for row in range(3):
        for col in range(3):
            bx0 = grid_left + col * btn_w + pad
            by0 = grid_top + row * btn_h + pad
            bx1 = bx0 + btn_w - pad * 2
            by1 = by0 + btn_h - pad * 2
            draw.rounded_rectangle((bx0, by0, bx1, by1),
                                   radius=int(min(btn_w, btn_h) * 0.18),
                                   fill=WHITE)


def _draw_button(canvas, x0, y0, x1, y1, title, subtitle, icon_fn, accent="dark"):
    """วาดปุ่ม 1 ปุ่ม — gradient + icon + title + subtitle"""
    draw = ImageDraw.Draw(canvas)
    w, h = x1 - x0, y1 - y0

    # background — gradient เขียวด้วยการวาดแถบ horizontal
    top = GREEN_LIGHT if accent == "light" else GREEN_MID
    bot = GREEN_MID   if accent == "light" else GREEN_DARK
    for i in range(h):
        t = i / h
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        draw.line((x0, y0 + i, x1, y0 + i), fill=(r, g, b))

    # icon (top 55%)
    icon_size = int(h * 0.32)
    icon_fn(draw, (x0 + x1) // 2, y0 + int(h * 0.32), icon_size)

    # title
    try:
        font_title = ImageFont.truetype(str(FONT_BOLD), int(h * 0.13))
        font_sub   = ImageFont.truetype(str(FONT_REG),  int(h * 0.08))
    except Exception:
        font_title = ImageFont.load_default()
        font_sub   = ImageFont.load_default()

    # measure + center title
    tb = draw.textbbox((0, 0), title, font=font_title)
    tw = tb[2] - tb[0]
    draw.text(((x0 + x1) // 2 - tw // 2, y0 + int(h * 0.62)),
              title, fill=WHITE, font=font_title)

    # subtitle
    sb = draw.textbbox((0, 0), subtitle, font=font_sub)
    sw = sb[2] - sb[0]
    draw.text(((x0 + x1) // 2 - sw // 2, y0 + int(h * 0.80)),
              subtitle, fill=(220, 255, 235), font=font_sub)


def build_compact():
    """1 row, 2 buttons (2500 x 843)"""
    W, H = 2500, 843
    img  = Image.new("RGB", (W, H), WHITE)
    gap  = 0
    half = W // 2
    _draw_button(img, 0,    0, half, H,
                 "เว็บ", "เปิดเว็บไซต์", _icon_browser, accent="light")
    _draw_button(img, half, 0, W,    H,
                 "คำนวณเบี้ยประกัน", "ตรวจสอบราคา", _icon_calculator, accent="dark")
    out = OUT_DIR / "richmenu_compact.png"
    img.save(out, "PNG", optimize=True)
    print(f"✓ {out}  ({out.stat().st_size/1024:.0f} KB)")


def build_large():
    """2 rows × 2 buttons (2500 x 1686)"""
    W, H = 2500, 1686
    img  = Image.new("RGB", (W, H), WHITE)
    half_w = W // 2
    half_h = H // 2
    # row 1
    _draw_button(img, 0,      0,      half_w, half_h,
                 "เว็บ", "เปิดเว็บไซต์", _icon_browser, accent="light")
    _draw_button(img, half_w, 0,      W,      half_h,
                 "คำนวณเบี้ยประกัน", "ตรวจสอบราคา", _icon_calculator, accent="dark")
    # row 2 — placeholder ปุ่มเพิ่มได้
    _draw_button(img, 0,      half_h, half_w, H,
                 "ใบแจ้งหนี้", "สร้าง invoice", _icon_calculator, accent="dark")
    _draw_button(img, half_w, half_h, W,      H,
                 "ติดต่อ", "Line / โทร", _icon_browser, accent="light")
    out = OUT_DIR / "richmenu_large.png"
    img.save(out, "PNG", optimize=True)
    print(f"✓ {out}  ({out.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    build_compact()
    build_large()
    print(f"\nไฟล์อยู่ที่ {OUT_DIR}")
    print("ใช้กับ LINE Official Account Manager → Rich Menu → upload ภาพนี้")
