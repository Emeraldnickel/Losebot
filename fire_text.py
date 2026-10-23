#!/usr/bin/env python3
"""Classic 'burning text' animated GIF generator (small, 5-frame, transparent).

Matches the style of old Cooltext-type GIFs: flat pure-red non-antialiased
serif text, a pixelated fire texture drawn BEHIND the letters (also filling
the counters/gaps) that rises ~2 px per frame, hard 1-bit transparency,
indexed palette, 50 ms/frame, seamless loop.

Usage:  python fire_text.py "Burning" -o burning.gif [--font FONT.ttf] [--size 56]
Needs:  pip install pillow numpy
"""
import argparse
from io import BytesIO
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DEFAULT_FONTS = [
    "fonts/AGATE_R1.TTF"
]

# --- palette: 254 fire colours + pure red text (254) + transparent (255) -----
RAMP = [(0.00, (40, 0, 0)), (0.20, (95, 0, 0)), (0.40, (160, 25, 8)),
        (0.58, (210, 85, 25)), (0.75, (240, 150, 45)), (0.90, (255, 225, 90)),
        (1.00, (255, 250, 205))]
RED_IDX, TRANS_IDX = 254, 255


def fire_palette():
    xs = [p for p, _ in RAMP]
    cols = np.array([c for _, c in RAMP], float)
    t = np.linspace(0, 1, 254)
    pal = np.stack([np.interp(t, xs, cols[:, k]) for k in range(3)], 1).astype(np.uint8)
    return np.vstack([pal, [255, 0, 0], [0, 0, 0]])


def box_blur(a, rx, ry):
    """Cheap separable box blur (numpy only)."""
    for axis, r in ((1, rx), (0, ry)):
        if r:
            k = 2 * r + 1
            p = np.pad(a, [(r, r) if i == axis else (0, 0) for i in range(2)], mode="edge")
            c = np.cumsum(p, axis=axis, dtype=np.float32)
            c = np.concatenate([np.zeros_like(np.take(c, [0], axis)), c], axis)
            a = (np.take(c, range(k, c.shape[axis]), axis) - np.take(c, range(0, c.shape[axis] - k), axis)) / k
    return a


def periodic_noise(period, width, rng):
    """Noise whose vertical period is `period`, so scrolling it loops perfectly.
    Coarse octaves give chunky flame tongues instead of TV static."""
    out = np.zeros((period, width), np.float32)
    for sx, sy, w in ((1, 1, 0.55), (3, 2, 1.0), (6, 3, 0.8), (12, 5, 0.5)):
        bh, bw = max(period // sy, 2), max(width // sx, 2)
        base = (rng.random((bh, bw)) * 255).astype(np.uint8)
        tiled = Image.fromarray(np.tile(base, (3, 1)))            # tile so the resize wraps
        big = tiled.resize((width, period * 3), Image.BICUBIC)
        out += w * np.asarray(big, np.float32)[period:2 * period] / 255
    out -= out.min()
    return out / out.max()


def make_gif(text, out=None, font_path=None, size=47, frames=5, step=2, ms=50,
             rise=17, pad=2, seed=3, stroke=0):
    font = None
    for fp in ([font_path] if font_path else DEFAULT_FONTS):
        try:
            font = ImageFont.truetype(fp, size)
            break
        except OSError:
            continue
    if font is None:
        raise SystemExit("No usable TrueType font found; pass --font")

    l, t, r, b = font.getbbox(text, stroke_width=stroke)
    tw, th = r - l, b - t
    W, H = tw + 2 * pad + 2, th + rise + 2 * pad + 2
    ty = rise + pad
    # non-antialiased text mask (mode "1") -> crisp pixels like the original
    m1 = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(m1)
    d.fontmode = "1"
    d.text((pad + 1 - l, ty - t), text, font=font, fill=1, stroke_width=stroke, stroke_fill=1)
    text_mask = np.array(m1, bool)

    # ---- heat field ------------------------------------------------------
    m = text_mask.astype(np.float32)
    up = np.zeros_like(m)                       # flames rising off the letter tops
    for dy in range(rise + 4):
        s = np.roll(m, -dy, axis=0)
        if dy:
            s[-dy:] = 0
        up = np.maximum(up, s * (1 - dy / (rise + 4)) ** 0.9)
    up = box_blur(up, 1, 0)
    inner = box_blur(m, 3, 3)                   # fire seeping into counters / gaps
    ys = np.arange(H, dtype=np.float32)[:, None]
    ytop = ty
    depth = np.clip((ys - ytop) / max(th, 1), 0, 1)
    inner = inner * (0.80 - 0.45 * depth)       # bright near the top, dark near baseline
    heat = np.maximum(up, inner)

    # ---- animate: noise texture scrolls UP by `step` px per frame --------
    rng = np.random.default_rng(seed)
    period = frames * step
    base = periodic_noise(period, W, rng)
    tex = np.tile(base, (H // period + 2, 1))   # long periodic texture
    pal = fire_palette()
    out_frames = []
    for f in range(frames):
        n = tex[f * step: f * step + H]         # window slides down the texture => content moves up
        # content moves up when the window moves down; add vertical streak shaping
        v = heat * (0.45 + 0.95 * n) - 0.22 + 0.12 * (n - 0.5)
        alpha = v > 0.10
        idx = np.clip((v * 1.05 * 253).astype(int), 0, 253).astype(np.uint8)
        idx[~alpha] = TRANS_IDX
        idx[text_mask] = RED_IDX                # text on top, identical every frame
        im = Image.fromarray(idx, "P")
        im.putpalette(pal.astype(np.uint8).flatten().tolist())
        out_frames.append(im)

    gif = BytesIO()
    out_frames[0].save(gif, format="GIF", save_all=True, append_images=out_frames[1:], duration=ms,
                       loop=0, disposal=2, transparency=TRANS_IDX)
    gif.seek(0)
    if out is not None:
        with open(out, "wb") as output_file:
            output_file.write(gif.getbuffer())
        print(f"Saved {out} ({W}x{H}, {frames} frames, {ms} ms)")
    return gif


def make_watermarked_gif(text, scale=4, watermark="watermark",
                         watermark_opacity=120, out=None, **gif_options):
    if scale < 1:
        raise ValueError("scale must be at least 1")

    source = make_gif(text, **gif_options)
    source_gif = Image.open(source)
    width, height = source_gif.size
    output_size = (width * scale, height * scale)

    font_size = max(1, height * scale // 2)
    font = None
    for font_path in (gif_options.get("font_path"), *DEFAULT_FONTS):
        if not font_path:
            continue
        try:
            font = ImageFont.truetype(font_path, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size=font_size)

    stroke_width = max(1, font_size // 24)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bounds = measure.textbbox((0, 0), watermark, font=font, stroke_width=stroke_width)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    text_padding = stroke_width + 2
    text_layer = Image.new(
        "RGBA",
        (text_width + 2 * text_padding, text_height + 2 * text_padding),
    )
    text_position = (text_padding - bounds[0], text_padding - bounds[1])
    watermark_draw = ImageDraw.Draw(text_layer)
    watermark_draw.text(
        text_position,
        watermark,
        font=font,
        fill=(255, 255, 255, watermark_opacity),
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, watermark_opacity),
    )
    rotated = text_layer.rotate(25, resample=Image.Resampling.BICUBIC, expand=True)
    rotated = rotated.crop(rotated.getchannel("A").getbbox())
    margin = max(1, min(output_size) // 40)
    fit_scale = min(
        1,
        (output_size[0] - 2 * margin) / rotated.width,
        (output_size[1] - 2 * margin) / rotated.height,
    )
    if fit_scale < 1:
        fitted_size = (
            max(1, round(rotated.width * fit_scale)),
            max(1, round(rotated.height * fit_scale)),
        )
        rotated = rotated.resize(fitted_size, Image.Resampling.LANCZOS)

    watermark_layer = Image.new("RGBA", output_size)
    watermark_layer.alpha_composite(
        rotated,
        ((output_size[0] - rotated.width) // 2, (output_size[1] - rotated.height) // 2),
    )

    indexed_frames = []
    durations = []
    frame_count = getattr(source_gif, "n_frames", 1)
    for frame_number in range(frame_count):
        source_gif.seek(frame_number)
        durations.append(source_gif.info.get("duration", 50))
        frame = source_gif.convert("RGBA").resize(output_size, Image.Resampling.NEAREST)
        source_alpha = np.asarray(frame.getchannel("A"))
        frame = Image.alpha_composite(frame, watermark_layer)
        indexed = frame.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        palette = indexed.getpalette() or []
        alpha = np.asarray(frame.getchannel("A")).copy()
        watermark_alpha = np.asarray(watermark_layer.getchannel("A"))
        watermark_over_transparency = (source_alpha < 128) & (watermark_alpha > 0)
        alpha[watermark_over_transparency] = 255
        indexed_pixels = np.asarray(indexed).copy()
        indexed_pixels[alpha < 128] = TRANS_IDX
        indexed = Image.fromarray(indexed_pixels.astype(np.uint8), "P")
        indexed.putpalette(palette[:765] + [0, 0, 0])
        indexed_frames.append(indexed)

    result = BytesIO()
    indexed_frames[0].save(
        result,
        format="GIF",
        save_all=True,
        append_images=indexed_frames[1:],
        duration=durations,
        loop=source_gif.info.get("loop", 0),
        disposal=2,
        transparency=TRANS_IDX,
    )
    result.seek(0)

    if out is not None:
        with open(out, "wb") as output_file:
            output_file.write(result.getbuffer())
        print(f"Saved {out} ({output_size[0]}x{output_size[1]}, {len(indexed_frames)} frames)")

    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("text")
    p.add_argument("-o", "--out", default="fire.gif")
    p.add_argument("--font", help="path to a bold serif .ttf")
    p.add_argument("--size", type=int, default=47, help="font size in px (default 47)")
    p.add_argument("--frames", type=int, default=5)
    p.add_argument("--step", type=int, default=2, help="px the flames rise per frame")
    p.add_argument("--ms", type=int, default=50, help="frame duration in ms")
    p.add_argument("--rise", type=int, default=17, help="flame height above the text, px")
    p.add_argument("--seed", type=int, default=3)
    p.add_argument("--stroke", type=int, default=0, help="thicken glyphs by N px (for fonts lighter than the original)")
    a = p.parse_args()
    make_gif(a.text, a.out, a.font, a.size, a.frames, a.step, a.ms, a.rise, seed=a.seed, stroke=a.stroke)
