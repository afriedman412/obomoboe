"""Make the app icons from icons/source.png.

    pip install pillow && python scripts/make_icons.py

Writes icons/obomoboe.icns (Mac) and icons/obomoboe.ico (Windows), which
obomoboe.spec builds into the app, src/static/icon.png for its header and
docs/icon.png for the download page. Only needed when the picture changes;
both outputs are checked in.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "icons"

# Apple's icon grid: a 1024 canvas with the artwork in an 824 rounded
# square, leaving room for the shadow the Dock expects.
CANVAS, ART, RADIUS = 1024, 824, 185


def rounded(image: Image.Image, size: int, radius: int) -> Image.Image:
    art = image.convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=radius, fill=255)
    art.putalpha(mask)
    return art


def mac_icon(source: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    offset = (CANVAS - ART) // 2
    shadow = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (offset, offset + 12, offset + ART, offset + ART + 12),
        radius=RADIUS, fill=(0, 0, 0, 90))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    canvas.alpha_composite(rounded(source, ART, RADIUS), (offset, offset))
    return canvas


def write_icns(icon: Image.Image, target: Path) -> None:
    work = Path(tempfile.mkdtemp())
    iconset = work / "obomoboe.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(
            iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)],
                   check=True)
    shutil.rmtree(work)


def main() -> None:
    source = Image.open(ICONS / "source.png")
    mac = mac_icon(source)
    mac.save(ICONS / "obomoboe.png")
    if sys.platform == "darwin":
        write_icns(mac, ICONS / "obomoboe.icns")
    # Windows draws its own frame, so the picture fills the icon, with
    # gently rounded corners.
    rounded(source, 256, 40).save(
        ICONS / "obomoboe.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
               (256, 256)])
    # The mark beside the name in the app's own header, sized for a 2x screen.
    rounded(source, 96, 20).save(ROOT / "src" / "static" / "icon.png")
    # And beside the headline on the download page, which runs to 112px.
    rounded(source, 256, 54).save(ROOT / "docs" / "icon.png")
    print("wrote", ", ".join(p.name for p in sorted(ICONS.glob("obomoboe.*"))),
          "src/static/icon.png and docs/icon.png")


if __name__ == "__main__":
    main()
