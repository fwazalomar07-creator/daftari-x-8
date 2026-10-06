"""يولّد فاتورة تجريبية (إنجليزية، لأن حزمة ara غير مثبتة هنا) ثم يشوّهها كصورة هاتف."""
import cv2, numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
ROWS = [("Oil Filter Toyota", "TY-100", 10, 6.50, 65.00),
        ("Air Filter Hyundai", "HY-200", 4, 12.00, 48.00),
        ("Engine Oil 5W30 4L", "OIL530", 6, 21.25, 127.50),
        ("Brake Pads Front", "BP-778", 2, 35.00, 70.00)]

def make(path_clean, path_photo):
    img = Image.new("RGB", (1000, 1300), "white"); d = ImageDraw.Draw(img)
    f, fb = ImageFont.truetype(FONT, 30), ImageFont.truetype(FONT, 34)
    d.text((60, 50), "Invoice No: INV-5521", font=fb, fill="black")
    d.text((60, 100), "Date: 2026-09-28", font=f, fill="black")
    d.text((60, 150), "Supplier: Al Omar Trading", font=f, fill="black")
    xs = {"name": 60, "code": 430, "qty": 600, "cost": 700, "total": 850}
    y = 260
    for k, t in zip(xs, ["Item", "Code", "Qty", "Price", "Total"]):
        d.text((xs[k], y), t, font=fb, fill="black")
    d.line((50, y + 50, 950, y + 50), fill="black", width=3)
    y += 80
    for n, c, q, u, t in ROWS:
        d.text((xs["name"], y), n, font=f, fill="black"); d.text((xs["code"], y), c, font=f, fill="black")
        d.text((xs["qty"], y), str(q), font=f, fill="black"); d.text((xs["cost"], y), f"{u:.2f}", font=f, fill="black")
        d.text((xs["total"], y), f"{t:.2f}", font=f, fill="black"); y += 65
    d.line((50, y, 950, y), fill="black", width=3)
    d.text((560, y + 30), "Grand Total: 310.50", font=fb, fill="black")
    img.save(path_clean)
    # --- محاكاة صورة هاتف: خلفية + منظور + ميلان + ظل + ضجيج + تمويه خفيف ---
    page = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    bg = np.full((1700, 1400, 3), (70, 90, 110), np.uint8)
    src = np.float32([[0, 0], [1000, 0], [1000, 1300], [0, 1300]])
    dst = np.float32([[170, 130], [1230, 190], [1180, 1560], [120, 1480]])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(page, M, (1400, 1700), borderValue=(0, 0, 0))
    mask = cv2.warpPerspective(np.full(page.shape[:2], 255, np.uint8), M, (1400, 1700))
    out = np.where(mask[..., None] > 0, warped, bg)
    grad = np.tile(np.linspace(0.62, 1.0, 1400)[None, :, None], (1700, 1, 1))  # ظل متدرج
    out = (out * grad).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0)
    out = np.clip(out + np.random.default_rng(1).normal(0, 6, out.shape), 0, 255).astype(np.uint8)
    cv2.imwrite(path_photo, out, [cv2.IMWRITE_JPEG_QUALITY, 80])

if __name__ == "__main__":
    make("/tmp/inv_clean.png", "/tmp/inv_photo.jpg"); print("ok")
