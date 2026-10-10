"""アプリアイコンを生成（python scripts/make_icons.py）。信号の波形＝ノイズの中のシグナル。"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "docs" / "icons"


def draw(size: int, rounded: bool) -> Image.Image:
    s = 4  # 高解像度で描いて縮小（アンチエイリアス）
    S = size * s
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (15, 17, 21, 255)
    if rounded:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=bg)
    else:
        d.rectangle([0, 0, S, S], fill=bg)
    # ノイズ（小さな点）
    import random
    rnd = random.Random(7)
    for _ in range(70):
        x, y = rnd.uniform(0.14, 0.86) * S, rnd.uniform(0.3, 0.7) * S
        r = S * 0.008
        d.ellipse([x - r, y - r, x + r, y + r], fill=(70, 76, 88, 255))
    # シグナル（波形）
    pts = [(0.14, 0.56), (0.30, 0.56), (0.38, 0.40), (0.46, 0.72), (0.54, 0.26), (0.62, 0.62), (0.70, 0.50), (0.86, 0.50)]
    d.line([(x * S, y * S) for x, y in pts], fill=(79, 209, 155, 255), width=int(S * 0.055), joint="curve")
    r = S * 0.05
    cx, cy = 0.54 * S, 0.26 * S
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(126, 162, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, True).save(OUT / "icon-192.png")
    draw(512, False).save(OUT / "icon-512.png")
    draw(180, False).convert("RGB").save(OUT / "apple-touch-icon.png")  # iOSは自動で角丸にする
    print("icons written to", OUT)
