"""Crop supplied images inside the normal bounded agent execution."""
from pathlib import Path
import hashlib


def crop_image(source, box, png=None):
    """Crop pixels; separate default paths keep multiple queries in one action."""
    from PIL import Image
    if len(box) != 4 or any(type(v) is not int for v in box):
        raise ValueError('box must contain four integer pixel coordinates')
    with Image.open(source) as image:
        left, top, right, bottom = box
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError('Crop must be nonempty and inside the source image')
        cropped = image.crop(box).convert('RGB')
    if png is None:
        # Stable per source and region; repeat queries may reuse the same file,
        # while different crops in a single execution cannot overwrite each other.
        identity = repr((str(source), tuple(box))).encode('utf-8')
        png = 'output/detail_' + hashlib.sha256(identity).hexdigest()[:20] + '.png'
    output = Path(png)
    if output.suffix.lower() != '.png':
        raise ValueError('Crop output must be PNG')
    output.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output)
    return {'png': str(output), 'size_pixels': list(cropped.size)}
