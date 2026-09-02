"""Build the walkthrough illustrations for the intro.

The three pictures are the study team's own illustrations of the capillary-
refill test — pressurise, release (green flash), colour returns — supplied by
the supervisor. Each is placed on a square tile in its own background colour
(a dark teal, rgb 4,18,26) so the three read as one consistent set with no
visible border, and so they sit seamlessly on the dark walkthrough card.

Source crops live in scratchpad/walkthrough_src/; re-run after replacing them.
"""
import os
from PIL import Image

SRC = os.path.join(os.path.dirname(__file__), 'walkthrough_src')
OUT = os.path.join(os.path.dirname(__file__), 'assets')
os.makedirs(OUT, exist_ok=True)

BG = (4, 18, 26)            # the illustrations' own background — seamless tile

# Which source crop illustrates each step of the test.
STEPS = {
    'crt-press.jpg':   'press.png',    # pressure applied (downward arrows)
    'crt-release.jpg': 'release.png',  # released — the green flash
    'crt-refill.jpg':  'refill.png',   # colour returns
}


def square(src_path, size, out_path, q=92):
    im = Image.open(src_path).convert('RGB')
    # Scale to fill the tile's height, keeping the illustration's proportions;
    # the narrower width leaves a little matching-colour margin either side.
    scale = size / im.height
    im = im.resize((max(1, round(im.width * scale)), size), Image.LANCZOS)
    canvas = Image.new('RGB', (size, size), BG)
    canvas.paste(im, ((size - im.width) // 2, 0))
    canvas.save(out_path, quality=q, optimize=True, progressive=True)
    print(f'{os.path.basename(out_path):20} {os.path.getsize(out_path)/1024:6.1f} KB')


for out_name, src_name in STEPS.items():
    square(os.path.join(SRC, src_name), 300, os.path.join(OUT, out_name))

# Filmstrip thumbnails for step 3 ("your clips come to you"): the same three
# illustrations, smaller, repeated to suggest a queue of clips.
strip = ['press.png', 'release.png', 'refill.png', 'press.png']
for i, src_name in enumerate(strip, 1):
    square(os.path.join(SRC, src_name), 192, os.path.join(OUT, f'crt-thumb{i}.jpg'), q=88)
