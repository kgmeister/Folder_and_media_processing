"""Draws the app icon (an owl) into icon.png and icon.ico. Run: python assets/make_icon.py"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024
BG = (38, 50, 56)            # slate
BODY = (141, 110, 99)        # warm brown
BELLY = (215, 204, 200)
FACE = (239, 235, 233)
EYE = (255, 193, 7)          # amber
PUPIL = (33, 33, 33)
BEAK = (255, 143, 0)

def draw() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([32, 32, S - 32, S - 32], radius=230, fill=BG)
    # ear tufts
    d.polygon([(250, 330), (300, 140), (420, 280)], fill=BODY)
    d.polygon([(774, 330), (724, 140), (604, 280)], fill=BODY)
    # body and head
    d.ellipse([210, 230, 814, 930], fill=BODY)
    d.ellipse([330, 560, 694, 900], fill=BELLY)
    for x, y in [(430, 660), (512, 640), (594, 660), (470, 760), (554, 760)]:    # belly feathers
        d.arc([x - 34, y - 26, x + 34, y + 26], 20, 160, fill=BODY, width=14)
    # facial discs, eyes, beak
    for cx in (395, 629):
        d.ellipse([cx - 150, 300, cx + 150, 600], fill=FACE)
        d.ellipse([cx - 92, 358, cx + 92, 542], fill=EYE)
        d.ellipse([cx - 50, 400, cx + 50, 500], fill=PUPIL)
        d.ellipse([cx - 4, 412, cx + 26, 442], fill=(255, 255, 255))           # catch light
    d.polygon([(470, 530), (554, 530), (512, 625)], fill=BEAK)
    # feet
    for cx in (440, 584):
        for dx in (-26, 0, 26):
            d.rounded_rectangle([cx + dx - 10, 880, cx + dx + 10, 940], radius=10, fill=BEAK)
    return im

if __name__ == "__main__":
    out = Path(__file__).resolve().parent
    im = draw()
    im.resize((512, 512), Image.LANCZOS).save(out / "icon.png")
    im.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote icon.png and icon.ico")
