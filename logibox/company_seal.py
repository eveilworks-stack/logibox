"""Lossless white-matte removal for company seals; originals are kept separately."""
import base64
import io
from functools import lru_cache
from PIL import Image


def transparent_png(raw):
    with Image.open(io.BytesIO(raw)) as source:
        if source.format != 'PNG' or not (1 <= source.width <= 4096 and 1 <= source.height <= 4096):
            raise ValueError('社印は4096px以下のPNGを選択してください')
        image = source.convert('RGBA')
        # An existing alpha channel is authoritative. Do not alter transparent seals.
        if image.getextrema()[3][0] < 255:
            return raw
        pixels = []
        changed = False
        for r, g, b, a in image.getdata():
            # Remove the neutral white matte. Unmatting preserves the visible red
            # antialiasing exactly when composited back onto white.
            matte = min(r, g, b)
            alpha = 255 - matte
            pixels.append((round((r-matte)*255/alpha), round((g-matte)*255/alpha),
                           round((b-matte)*255/alpha), alpha) if alpha else (0, 0, 0, 0))
            changed |= matte > 0
        if not changed:
            return raw
        image.putdata(pixels)
        output = io.BytesIO()
        image.save(output, 'PNG')
        return output.getvalue()


@lru_cache(maxsize=8)
def encoded_png(value):
    return base64.b64encode(transparent_png(base64.b64decode(value, validate=True))).decode('ascii')


def save(c, value):
    from server import getset, setset
    # Keep every original, including future replacements, inside the backed-up DB.
    import hashlib
    raw = base64.b64decode(value, validate=True)
    converted = encoded_png(value)
    if converted != value:
        key = 'company_seal_original_' + hashlib.sha256(raw).hexdigest()
        if getset(c, key) is None:
            setset(c, key, value)
    setset(c, 'company_seal_png', converted)
    return converted
