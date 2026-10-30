"""MATCH: which page of the deck is on screen in each slide of a timeline (`slide-sync/match@1`).

For every slide (segment) of a `detect` timeline, a few frames inside it are taken through ffmpeg-skill `look`, and each
is compared with every page image of an `import`ed deck. The comparison is image similarity only, deterministic and
standard library (docs/decisions.md ADR-002):

1. Every picture becomes a grid of mean luma (pages and frames alike).
2. A frame is looked at as a whole (a screen capture), as the page's aspect ratio fitted inside it (letterbox /
   pillarbox), and, when the slide sits inside a uniform surround (bars, a dark room), as the quadrilateral found
   there, warped back to a rectangle (a camera's keystone, a small tilt).
3. Each view and each page is z-scored (brightness and contrast drop out), the deck's mean page is subtracted (what
   all pages share: template, logo, background), and they are compared by normalised correlation, -1..1.
4. A segment's score for a page is the median over its frames; the best page is reported only when it clears
   `min_score` and beats the runner-up by `min_margin`. Otherwise the segment gets `page: null` and the reason.

No OCR here: slide text from pixels is detection-skill's job.
"""
from __future__ import annotations

import json
import math
import operator
import os
import struct
import tempfile
import zlib
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .core import TIMELINE_SCHEMA, SlideSyncError
from .deck import DECK_SCHEMA, MANIFEST
from .engine import FfmpegSkill, Runner, run_tool, subprocess_runner

MATCH_SCHEMA = "slide-sync/match@1"
METHOD = "luma-grid-zncc"
DEFAULT_MIN_SCORE = 0.7    # ADR-002: every wrong page measured below 0.6, every right one above 0.85
DEFAULT_MIN_MARGIN = 0.1   # ADR-002: the right page led the runner-up by at least 0.3
DEFAULT_SAMPLES = 3
MAX_SAMPLES = 9
FRAME_WIDTH = 640          # frames are taken this wide (ffmpeg-skill look --width): 2.5 px per grid cell
GRID_W, GRID_H = 256, 144  # first reduction of every picture: mean luma per cell (where a slide is located)
FEAT_W, FEAT_H = 128, 72   # what is correlated: mean luma per cell of a view
MIN_TEMPLATE_PAGES = 3     # the deck's mean page is subtracted from 3 pages up
UNIFORM_TOL = 12.0         # luma (0-255): a line whose mean deviation is at most this is uniform (a surround)
MIN_KEEP = 0.25            # a located slide is at least this fraction of the frame in each direction
FLAT_STD = 2.0             # a picture whose luma standard deviation is below this is flat (blank, solid colour)
FLAT_RGB_SCALE = 64.0      # flat vs flat: the score falls from 1 to 0 as the mean colours differ by this much
INSET = 0.03               # a located slide is compared without this fraction at each edge (edge error, bars)
EDGE_GUARD = 0.1           # seconds kept clear of a segment's ends when sampling (the cut itself, a fade)

Box = Tuple[float, float, float, float]  # x0, y0, x1, y1 in grid cells
Quad = Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float], Tuple[float, float]]


# ---------------------------------------------------------------- PNG (standard library)

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path: str) -> Tuple[int, int, int, List[bytes]]:
    """(width, height, channels, rows) of a non-interlaced 8- or 16-bit PNG, rows unfiltered and reduced to 8 bits
    per sample, palette expanded to RGB. Enough for what ffmpeg and this skill's import write."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise SlideSyncError("INPUT_NOT_FOUND", f"cannot read {path}: {e}")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SlideSyncError("INVALID_IMAGE", f"not a PNG: {path}")
    pos, idat, palette, header = 8, [], b"", None
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if header is None or not idat:
        raise SlideSyncError("INVALID_IMAGE", f"damaged PNG: {path}")
    width, height, depth, colour, _, _, interlace = header
    samples = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if samples is None or depth not in (8, 16) or (colour == 3 and depth != 8) or interlace:
        raise SlideSyncError("INVALID_IMAGE", f"unsupported PNG (colour type {colour}, {depth}-bit, interlace {interlace}): {path}")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        raise SlideSyncError("INVALID_IMAGE", f"damaged PNG: {path}")
    bpp = samples * depth // 8
    stride = width * bpp
    if len(raw) < height * (stride + 1):
        raise SlideSyncError("INVALID_IMAGE", f"truncated PNG: {path}")
    rows: List[bytes] = []
    prev = bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 0xFF
        elif f == 4:
            for i in range(stride):
                line[i] = (line[i] + _paeth(line[i - bpp] if i >= bpp else 0, prev[i], prev[i - bpp] if i >= bpp else 0)) & 0xFF
        elif f != 0:
            raise SlideSyncError("INVALID_IMAGE", f"damaged PNG (filter {f}): {path}")
        prev = line
        out = bytes(line[0::2]) if depth == 16 else bytes(line)
        if colour == 3:
            out = b"".join(palette[3 * i:3 * i + 3] for i in out)
        rows.append(out)
    channels = 3 if colour == 3 else samples
    return width, height, channels, rows


def _png_size(path: str) -> Tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    return struct.unpack(">II", head[16:24])


# ---------------------------------------------------------------- features

def luma_grid(path: str, gw: int, gh: int) -> Tuple[List[float], List[float]]:
    """(luma, rgb): the mean luma of each cell of a gw x gh grid laid over the whole picture (any aspect ratio is
    stretched), luma being (R + G + B) / 3, and the picture's mean R, G, B. The equal-weight luma keeps the
    reduction to one C-level sum per cell and pixel row; correlation does not need a perceptual weighting."""
    width, height, channels, rows = read_png(path)
    if width < gw or height < gh:
        raise SlideSyncError("INVALID_IMAGE", f"{path} is {width}x{height}, smaller than the {gw}x{gh} grid")
    xs = [width * i // gw * channels for i in range(gw + 1)]
    ys = [height * j // gh for j in range(gh + 1)]
    colour = channels >= 3
    per_pixel = 3 if colour else 1  # what one pixel adds to a plain sum of its colour samples
    sums = [0] * (gw * gh)
    for j in range(gh):
        base = j * gw
        for y in range(ys[j], ys[j + 1]):
            row = rows[y]
            for i in range(gw):
                lo, hi = xs[i], xs[i + 1]
                if channels in (1, 3):
                    v = sum(row[lo:hi])
                elif channels == 4:
                    v = sum(row[lo:hi]) - sum(row[lo + 3:hi:4])
                else:  # grey + alpha
                    v = sum(row[lo:hi:2])
                sums[base + i] += v
    luma = []
    for j in range(gh):
        n_rows = ys[j + 1] - ys[j]
        for i in range(gw):
            luma.append(sums[j * gw + i] / ((xs[i + 1] - xs[i]) // channels * n_rows * per_pixel))
    step = max(1, height // 64)  # mean colour from every step-th row: only blank / solid pictures use it
    rgb, n = [0, 0, 0], 0
    for y in range(0, height, step):
        row = rows[y]
        for c in range(3):
            rgb[c] += sum(row[(c if colour else 0)::channels])
        n += width
    return luma, [v / n for v in rgb]


def _spread(seg: Sequence[float]) -> float:
    """Mean absolute deviation from the median: a line of camera noise stays low, a line that crosses into the slide
    (even for a tenth of its length) does not."""
    med = sorted(seg)[len(seg) // 2]
    return sum(abs(v - med) for v in seg) / len(seg)


def _surround(luma: Sequence[float], gw: int, gh: int, tol: float) -> Tuple[Dict[str, float], Tuple[int, int, int, int]]:
    """The surround's colour on each edge whose outermost line is uniform, and the whole-cell box left after removing,
    from those edges, the uniform lines of that colour (letterbox / pillarbox bars, a dark wall around a projection).
    Removal stops at the first line that differs: the slide's own edge (its white margin is a different colour)."""
    def line(horizontal: bool, k: int, lo: int, hi: int) -> List[float]:
        return list(luma[k * gw + lo:k * gw + hi]) if horizontal else [luma[y * gw + k] for y in range(lo, hi)]
    x0, y0, x1, y1 = 0, 0, gw, gh
    refs: Dict[str, float] = {}
    for edge, horizontal, pos in (("top", True, 0), ("bottom", True, gh - 1), ("left", False, 0), ("right", False, gw - 1)):
        first = line(horizontal, pos, 0, gw if horizontal else gh)
        if _spread(first) <= tol:
            refs[edge] = sum(first) / len(first)
    for _ in range(2):  # twice: removing one axis's bars shortens the other axis's lines, which may then be uniform
        for edge, ref in refs.items():
            horizontal = edge in ("top", "bottom")
            while (y1 - y0 if horizontal else x1 - x0) > 1:
                pos = {"top": y0, "bottom": y1 - 1, "left": x0, "right": x1 - 1}[edge]
                seg = line(horizontal, pos, *((x0, x1) if horizontal else (y0, y1)))
                if _spread(seg) > tol or abs(sum(seg) / len(seg) - ref) > tol:
                    break
                if edge == "top":
                    y0 += 1
                elif edge == "bottom":
                    y1 -= 1
                elif edge == "left":
                    x0 += 1
                else:
                    x1 -= 1
    return refs, (x0, y0, x1, y1)


def _edge_points(luma: Sequence[float], gw: int, gh: int, edge: str, ref: float, box: Tuple[int, int, int, int],
                 tol: float) -> List[Tuple[float, float]]:
    """Sub-cell points on one edge of the slide: along each grid line across that edge (the middle 70 % of the slide),
    where the luma crosses halfway from the surround's level to the slide's just inside the edge (for a step edge
    blurred by scaling, the halfway point is the edge). Points are
    (along, across): (y, x) for left / right edges, (x, y) for top / bottom."""
    x0, y0, x1, y1 = box
    horizontal_edge = edge in ("top", "bottom")
    lo, hi = (x0, x1) if horizontal_edge else (y0, y1)
    margin = int((hi - lo) * 0.15)
    n_across = gh if horizontal_edge else gw
    points = []
    for k in range(lo + margin, hi - margin):
        near = [m for m in (k - 1, k, k + 1) if 0 <= m < (gw if horizontal_edge else gh)]
        # from this edge to the middle only, as the mean of three neighbouring lines (to calm camera noise)
        cells = range(n_across // 2) if edge in ("top", "left") else range(n_across - 1, n_across - 1 - n_across // 2, -1)
        if horizontal_edge:
            profile = [sum(luma[y * gw + m] for m in near) / len(near) for y in cells]
        else:
            profile = [sum(luma[m * gw + x] for m in near) / len(near) for x in cells]
        dev = [abs(v - ref) for v in profile]
        first = next((i for i in range(len(dev) - 1) if dev[i] > 2 * tol and dev[i + 1] > 2 * tol), None)
        if first is None or first == 0:
            continue
        half = max(dev[first + 1:first + 4]) / 2  # halfway to the slide's level just past a blurred edge
        if half <= tol:
            continue
        i = next((j for j in range(1, first + 2) if dev[j] >= half), None)
        if i is None:
            continue
        cross = (i - 0.5) + (half - dev[i - 1]) / max(dev[i] - dev[i - 1], 1e-9)  # cell i is centred on i + 0.5
        if edge in ("bottom", "right"):
            cross = n_across - cross
        points.append((k + 0.5, cross))
    return points


def _fit_line(points: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """across = a * along + b by least squares, refitted once without points more than 1.5 cells off."""
    for _ in range(2):
        if len(points) < 4:
            return None
        n = len(points)
        mx = sum(p[0] for p in points) / n
        my = sum(p[1] for p in points) / n
        sxx = sum((p[0] - mx) ** 2 for p in points)
        if sxx == 0:
            return None
        a = sum((p[0] - mx) * (p[1] - my) for p in points) / sxx
        b = my - a * mx
        points = [p for p in points if abs(p[1] - (a * p[0] + b)) <= 1.5]
    return a, b


def slide_quad(luma: Sequence[float], gw: int, gh: int, tol: float = UNIFORM_TOL) -> Optional[Quad]:
    """The four corners (top-left, top-right, bottom-right, bottom-left, in grid cells) of the slide inside a uniform
    surround: each edge with a surround is a straight line fitted to sub-cell edge points, so a tilted or keystoned
    projection (a camera off the projector's axis) is found as a quadrilateral, not just its bounding box. An edge
    without a surround is the frame's own edge. None when there is no surround or too little is left inside it."""
    refs, box = _surround(luma, gw, gh, tol)
    x0, y0, x1, y1 = box
    if box == (0, 0, gw, gh) or (x1 - x0) < MIN_KEEP * gw or (y1 - y0) < MIN_KEEP * gh:
        return None
    lines = {"left": (0.0, 0.0), "right": (0.0, float(gw)), "top": (0.0, 0.0), "bottom": (0.0, float(gh))}
    for edge, ref in refs.items():
        fitted = _fit_line(_edge_points(luma, gw, gh, edge, ref, box, tol))
        if fitted is None:
            return None
        lines[edge] = fitted

    def corner(vertical: str, horizontal: str) -> Tuple[float, float]:
        av, bv = lines[vertical]      # x = av * y + bv
        ah, bh = lines[horizontal]    # y = ah * x + bh
        x = (av * bh + bv) / (1 - av * ah)
        return x, ah * x + bh
    quad = (corner("left", "top"), corner("right", "top"), corner("right", "bottom"), corner("left", "bottom"))
    xs, ys = [c[0] for c in quad], [c[1] for c in quad]
    if min(xs) < -1 or max(xs) > gw + 1 or min(ys) < -1 or max(ys) > gh + 1:
        return None
    if max(xs) - min(xs) < MIN_KEEP * gw or max(ys) - min(ys) < MIN_KEEP * gh:
        return None
    return quad


def quad_sampler(quad: Quad) -> Callable[[float, float], Tuple[float, float]]:
    """(u, v) in the unit square -> (x, y) in the quad: the projective map of the square onto the four corners
    (Heckbert, "Fundamentals of Texture Mapping and Image Warping", 1989, section 2.2.3)."""
    (xa, ya), (xb, yb), (xc, yc), (xd, yd) = quad
    sx, sy = xa - xb + xc - xd, ya - yb + yc - yd
    g = h = 0.0
    if abs(sx) > 1e-9 or abs(sy) > 1e-9:
        dx1, dx2, dy1, dy2 = xb - xc, xd - xc, yb - yc, yd - yc
        den = dx1 * dy2 - dx2 * dy1
        if abs(den) > 1e-12:
            g = (sx * dy2 - dx2 * sy) / den
            h = (dx1 * sy - sx * dy1) / den
    a, b, c = xb - xa + g * xb, xd - xa + h * xd, xa
    d, e, f = yb - ya + g * yb, yd - ya + h * yd, ya

    def to_xy(u: float, v: float) -> Tuple[float, float]:
        w = g * u + h * v + 1
        return (a * u + b * v + c) / w, (d * u + e * v + f) / w
    return to_xy


def quad_view(luma: Sequence[float], gw: int, gh: int, quad: Quad, frac: float = INSET, ow: int = 0, oh: int = 0,
              sub: int = 2) -> List[float]:
    """The slide inside `quad` resampled to ow x oh, as if seen straight on, without `frac` at each edge (the same
    region as a page's inset view): each output cell is the mean of sub x sub bilinear samples."""
    ow, oh = ow or FEAT_W, oh or FEAT_H
    to_xy = quad_sampler(quad)

    def at(x: float, y: float) -> float:
        x, y = min(max(x - 0.5, 0.0), gw - 1.0), min(max(y - 0.5, 0.0), gh - 1.0)  # cell i is centred on i + 0.5
        i, j = min(int(x), gw - 2), min(int(y), gh - 2)
        fx, fy = x - i, y - j
        top = luma[j * gw + i] * (1 - fx) + luma[j * gw + i + 1] * fx
        bottom = luma[(j + 1) * gw + i] * (1 - fx) + luma[(j + 1) * gw + i + 1] * fx
        return top * (1 - fy) + bottom * fy
    span = 1 - 2 * frac
    out = []
    for r in range(oh):
        for q in range(ow):
            acc = 0.0
            for sv in range(sub):
                for su in range(sub):
                    u = frac + span * (q + (su + 0.5) / sub) / ow
                    v = frac + span * (r + (sv + 0.5) / sub) / oh
                    acc += at(*to_xy(u, v))
            out.append(acc / (sub * sub))
    return out


def fit_box(frame_aspect: float, page_aspect: float, gw: int, gh: int) -> Optional[Box]:
    """Where a page of `page_aspect` sits when it is scaled to fit a frame of `frame_aspect` and centred (letterbox
    or pillarbox), in grid cells; None when the two aspects are within 1 %."""
    if abs(page_aspect / frame_aspect - 1) <= 0.01:
        return None
    if page_aspect < frame_aspect:
        w = gw * page_aspect / frame_aspect
        return (gw - w) / 2, 0.0, (gw + w) / 2, float(gh)
    h = gh * frame_aspect / page_aspect
    return 0.0, (gh - h) / 2, float(gw), (gh + h) / 2


def _weights(n_in0: float, n_in1: float, n_out: int) -> List[List[Tuple[int, float]]]:
    """Area-average weights from the input span [n_in0, n_in1) (cells, fractions allowed) to n_out output cells."""
    span = (n_in1 - n_in0) / n_out
    out = []
    for k in range(n_out):
        a, b = n_in0 + k * span, n_in0 + (k + 1) * span
        w = []
        i = int(math.floor(a))
        while i < b and i < n_in1:
            overlap = min(b, i + 1) - max(a, i)
            if overlap > 1e-9:
                w.append((i, overlap / span))
            i += 1
        out.append(w)
    return out


def resample(values: Sequence[float], gw: int, box: Box, ow: int = 0, oh: int = 0) -> List[float]:
    """The box of a gw-wide grid, area-averaged to ow x oh (default: the feature size)."""
    ow, oh = ow or FEAT_W, oh or FEAT_H
    x0, y0, x1, y1 = box
    wx, wy = _weights(x0, x1, ow), _weights(y0, y1, oh)
    ys = sorted({y for w in wy for y, _ in w})
    rows = {y: [sum(values[y * gw + i] * f for i, f in wx[k]) for k in range(ow)] for y in ys}
    return [sum(rows[y][k] * f for y, f in wy[j]) for j in range(oh) for k in range(ow)]


def _zscore(vec: Sequence[float]) -> Optional[List[float]]:
    """Zero mean, unit standard deviation, so a frame's brightness and contrast (exposure, a washed-out projector)
    drop out before anything is compared; None for a flat vector (nothing to correlate)."""
    n = len(vec)
    mean = sum(vec) / n
    centred = [v - mean for v in vec]
    std = math.sqrt(sum(c * c for c in centred) / n)
    if std < FLAT_STD:
        return None
    return [c / std for c in centred]


def _unit(vec: Sequence[float]) -> Optional[List[float]]:
    """Zero mean, unit length: the dot product of two such vectors is their correlation."""
    n = len(vec)
    mean = sum(vec) / n
    centred = [v - mean for v in vec]
    norm = math.sqrt(sum(c * c for c in centred))
    if norm < 1e-6 * math.sqrt(n):
        return None
    return [c / norm for c in centred]


def _inset(box: Box, frac: float = INSET) -> Box:
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) * frac, (y1 - y0) * frac
    return x0 + dx, y0 + dy, x1 - dx, y1 - dy


def signature(path: str) -> Dict[str, Any]:
    """A picture reduced to what it is compared by: its luma grid, the feature of the whole picture (not normalised
    yet, see `prepare`), its aspect ratio, and its mean colour and flatness (blank / solid pictures)."""
    width, height = _png_size(path)
    gw, gh = GRID_W, GRID_H
    luma, rgb = luma_grid(path, gw, gh)
    full = resample(luma, gw, (0.0, 0.0, float(gw), float(gh)))
    n = len(full)
    mean = sum(full) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in full) / n)
    inset = _inset((0.0, 0.0, float(gw), float(gh)))
    return {"luma": luma, "grid": (gw, gh), "aspect": width / height, "flat": std < FLAT_STD, "rgb": rgb,
            "views": {"full": full, "inset": resample(luma, gw, inset)}}


def add_frame_views(sig: Dict[str, Any], page_aspects: Sequence[float]) -> Dict[str, Any]:
    """A frame is looked at in more than one way, because the slide need not fill it: the whole frame (a screen
    capture), the page's aspect ratio fitted and centred (letterbox / pillarbox), and the quadrilateral found inside a
    uniform surround (bars of any colour, a dark room around a keystoned projection), warped back to a rectangle. The
    last two leave INSET out at each edge and are compared with the pages' inset view."""
    gw, gh = sig["grid"]
    views = {"full": sig["views"]["full"]}
    for aspect in sorted(set(round(a, 3) for a in page_aspects)):
        box = fit_box(sig["aspect"], aspect, gw, gh)
        if box:
            views[f"fit {aspect:g}"] = resample(sig["luma"], gw, _inset(box))
    quad = slide_quad(sig["luma"], gw, gh)
    if quad:
        views["located"] = quad_view(sig["luma"], gw, gh, quad)
    return dict(sig, views=views)


def deck_template(pages: Sequence[Dict[str, Any]]) -> Optional[Dict[str, List[float]]]:
    """The mean (z-scored) feature over the deck's pages with content, or None for a deck with fewer than
    MIN_TEMPLATE_PAGES. Subtracting it removes what every page shares (title bar, logo, footer, the background) so
    the comparison rests on what tells one page from another."""
    content = []
    for p in pages:
        if p["flat"]:
            continue
        z = {k: _zscore(p["views"][k]) for k in ("full", "inset")}
        if all(v is not None for v in z.values()):
            content.append(z)
    if len(content) < MIN_TEMPLATE_PAGES:
        return None
    return {k: [sum(col) / len(content) for col in zip(*(v[k] for v in content))] for k in ("full", "inset")}


def prepare(sig: Dict[str, Any], template: Optional[Dict[str, List[float]]]) -> Dict[str, Any]:
    """The signature's views made ready to correlate: z-scored, the deck template subtracted (when there is one),
    then zero mean and unit length. Views that come out flat are dropped."""
    views = {}
    if not sig["flat"]:
        for name, vec in sig["views"].items():
            z = _zscore(vec)
            if z is None:
                continue
            base = template["full" if name == "full" else "inset"] if template else None
            v = _unit([a - b for a, b in zip(z, base)] if base else z)
            if v is not None:
                views[name] = v
    return {"views": views, "flat": sig["flat"], "rgb": sig["rgb"]}


def similarity(frame: Dict[str, Any], page: Dict[str, Any]) -> Tuple[float, Optional[str]]:
    """(score 0..1, view) of a prepared frame against a prepared page: the best correlation over the frame's views
    (the whole frame against the whole page, a located slide against the page's inset view), floored at 0. A blank /
    solid picture scores against another by how close their colours are, and 0 against anything with content."""
    if frame["flat"] or page["flat"]:
        if not (frame["flat"] and page["flat"]):
            return 0.0, None
        diff = max(abs(x - y) for x, y in zip(frame["rgb"], page["rgb"]))
        return max(0.0, 1.0 - diff / FLAT_RGB_SCALE), "flat"
    best: Tuple[float, Optional[str]] = (0.0, None)
    for name, vec in frame["views"].items():
        target = page["views"].get("full" if name == "full" else "inset")
        if target is None:
            continue
        score = sum(map(operator.mul, vec, target))
        if score > best[0]:
            best = (score, name)
    return best


# ---------------------------------------------------------------- deck, timeline, sampling

def load_deck(deck_dir: str) -> Dict[str, Any]:
    path = os.path.join(deck_dir, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            deck = json.load(fh)
    except (OSError, ValueError) as e:
        raise SlideSyncError("INVALID_DECK", f"cannot read {path} (run `slide-sync import` first): {e}")
    if not isinstance(deck, dict) or deck.get("schema") != DECK_SCHEMA or not deck.get("pages"):
        raise SlideSyncError("INVALID_DECK", f"{path} is not a {DECK_SCHEMA} manifest with pages")
    return deck


def sample_times(start: float, end: float, n: int, duration: Optional[float] = None) -> List[float]:
    """n times evenly inside [start, end] (at (k + 1) / (n + 1) of it), kept EDGE_GUARD clear of both ends so a frame
    is never the cut itself, and never past the end of the video."""
    hi = end if duration is None else min(end, duration)
    lo, hi2 = start + EDGE_GUARD, hi - EDGE_GUARD
    if hi2 <= lo:
        lo = hi2 = (start + hi) / 2
    return [round(lo + (hi2 - lo) * (k + 1) / (n + 1), 3) for k in range(n)]


def _median(values: List[float]) -> float:
    s = sorted(values)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def decide(scores: Dict[int, float], *, min_score: float, min_margin: float) -> Dict[str, Any]:
    """Best page, or none with the reason. `scores` maps page index -> segment score."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_page, best = ranked[0]
    runner_page, runner = ranked[1] if len(ranked) > 1 else (None, -1.0)
    margin = best - runner
    if best < min_score:
        page, reason = None, "below_min_score"
    elif margin < min_margin:
        page, reason = None, "ambiguous"
    else:
        page, reason = best_page, None
    return {"page": page, "confidence": round(best, 3), "margin": round(margin, 3), "reason": reason,
            "candidates": [{"page": p, "score": round(s, 3)} for p, s in ranked[:3]]}


# ---------------------------------------------------------------- the op

def match(skill: FfmpegSkill, video: str, deck_dir: str, timeline: Dict[str, Any], *, min_score: float = DEFAULT_MIN_SCORE,
          min_margin: float = DEFAULT_MIN_MARGIN, samples: int = DEFAULT_SAMPLES, timeout: float = 3600.0,
          runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """The deck page shown in each slide of `timeline`: page index, confidence (the score) and margin over the
    runner-up, or `page: null` with the reason when the evidence is not good enough."""
    if not os.path.isfile(video):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {video}")
    if not (-1.0 <= min_score <= 1.0):
        raise SlideSyncError("INVALID_ARGUMENT", "--min-score must be in [-1, 1]")
    if not (0.0 <= min_margin <= 2.0):
        raise SlideSyncError("INVALID_ARGUMENT", "--min-margin must be in [0, 2]")
    if not (1 <= samples <= MAX_SAMPLES):
        raise SlideSyncError("INVALID_ARGUMENT", f"--samples must be 1 to {MAX_SAMPLES}")
    if timeline.get("schema") != TIMELINE_SCHEMA or not timeline.get("slides"):
        raise SlideSyncError("INVALID_TIMELINE", f"not a {TIMELINE_SCHEMA} document with slides")
    deck = load_deck(deck_dir)
    warnings: List[str] = []
    src = (timeline.get("source") or {}).get("path")
    if src and os.path.abspath(src) != os.path.abspath(video):
        warnings.append(f"the timeline was measured on {src}, not on this video")
    raw_pages = {int(p["index"]): signature(os.path.join(deck_dir, p["image"])) for p in deck["pages"]}
    template = deck_template(list(raw_pages.values()))
    pages = {i: prepare(sig, template) for i, sig in raw_pages.items()}
    aspects = [sig["aspect"] for sig in raw_pages.values()]
    duration = (timeline.get("source") or {}).get("duration")
    slides = timeline["slides"]
    times = [sample_times(float(s["start"]), float(s["end"]), samples, duration) for s in slides]
    flat_times = [t for ts in times for t in ts]
    with tempfile.TemporaryDirectory(prefix="slide_sync_match_") as tmp:
        args = [video] + [x for t in flat_times for x in ("--at", f"{t:.3f}")] + [
            "--width", str(FRAME_WIDTH), "--no-timecode", "-o", os.path.join(tmp, "frame")]
        doc = run_tool(skill, "look", args, timeout=timeout, runner=runner)
        outputs = doc.get("outputs") or ([doc["output"]] if doc.get("output") else [])
        if len(outputs) != len(flat_times):
            raise SlideSyncError("ENGINE_FAILED", f"ffmpeg-skill look wrote {len(outputs)} frame(s) for {len(flat_times)} requested")
        frame_sigs = [prepare(add_frame_views(signature(o if os.path.isabs(o) else os.path.join(os.getcwd(), o)), aspects),
                              template) for o in outputs]
    segments = []
    k = 0
    for s, ts in zip(slides, times):
        sigs = frame_sigs[k:k + len(ts)]
        k += len(ts)
        per_page = {i: [similarity(f, sig) for f in sigs] for i, sig in pages.items()}
        scores = {i: _median([sc for sc, _ in got]) for i, got in per_page.items()}
        d = decide(scores, min_score=min_score, min_margin=min_margin)
        best_views = [v for _, v in per_page[d["candidates"][0]["page"]] if v]
        view = max(sorted(set(best_views)), key=best_views.count) if best_views else None
        # each frame on its own, with the same rule: frames that disagree mean the segment holds more than one page
        # (a change detect did not report, an animated build) or something in front of the slide
        sample_pages = [decide({i: got[n][0] for i, got in per_page.items()}, min_score=min_score,
                               min_margin=min_margin)["page"] for n in range(len(ts))]
        mixed = len({p for p in sample_pages if p is not None}) > 1
        if mixed:
            warnings.append(f"segment {s['index']} ({s['start']:.3f}-{s['end']:.3f}s): its frames show different pages "
                            f"{sample_pages}; detect may have missed a change there")
        segments.append({"index": s["index"], "start": s["start"], "end": s["end"], "page": d["page"],
                         "confidence": d["confidence"], "margin": d["margin"], "method": METHOD, "view": view,
                         "reason": d["reason"], "samples": [{"time": t, "page": p} for t, p in zip(ts, sample_pages)],
                         "mixed": mixed, "candidates": d["candidates"]})
    shown = sorted({s["page"] for s in segments if s["page"] is not None})
    unmatched = [s["index"] for s in segments if s["page"] is None]
    if unmatched:
        warnings.append(f"{len(unmatched)} segment(s) not matched to a page: " + ", ".join(map(str, unmatched[:10]))
                        + (" ..." if len(unmatched) > 10 else ""))
    return {
        "schema": MATCH_SCHEMA,
        "source": {"path": os.path.abspath(video), "duration": duration},
        "deck": {"path": os.path.abspath(deck_dir), "page_count": len(pages), "source": (deck.get("source") or {}).get("path")},
        "matcher": {"skill": "slide-sync", "skill_version": __version__, "engine": "ffmpeg-skill/look",
                    "engine_version": skill.version, "method": METHOD, "min_score": min_score, "min_margin": min_margin,
                    "samples": samples, "frame_width": FRAME_WIDTH, "grid": [GRID_W, GRID_H], "feature": [FEAT_W, FEAT_H],
                    "uniform_tol": UNIFORM_TOL},
        "segment_count": len(segments),
        "matched_count": len(segments) - len(unmatched),
        "segments": segments,
        "pages_shown": shown,
        "pages_not_shown": [i for i in sorted(pages) if i not in shown],
        "warnings": warnings,
        "provenance": "OBSERVED",
    }
