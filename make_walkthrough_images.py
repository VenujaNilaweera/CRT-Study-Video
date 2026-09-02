"""Build the walkthrough photographs from the real study clip.

Every image is a genuine frame of CRTtest01.mp4. The only thing added is the
release flash, and that is applied with the exact same recipe studio.py uses
when it encodes a clip (3 frames tinted 50% pure green), so the picture the
walkthrough shows is the picture the participant will actually see.
"""
import av, numpy as np
from PIL import Image

SRC = '/home/user/CRT-Study-Video/CRTtest01.mp4'
OUT = '/home/user/CRT-Study-Video/assets'
import os; os.makedirs(OUT, exist_ok=True)

c = av.open(SRC)
frames = [f.to_ndarray(format='rgb24') for f in c.decode(c.streams.video[0])]
H, W = frames[0].shape[:2]

FLASH_ALPHA = 0.5          # studio.py FLASH_ALPHA
FLASH_RGB = (0, 255, 0)    # studio.py FLASH_COLOR, BGR green == RGB green


def crop(idx, cx, cy, size=196, out=384, flash=False):
    a = frames[idx].astype(np.float32)
    if flash:
        ov = np.empty_like(a); ov[:] = FLASH_RGB
        a = ov * FLASH_ALPHA + a * (1 - FLASH_ALPHA)
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    x0 = max(0, min(W - size, cx - size // 2))
    y0 = max(0, min(H - size, cy - size // 2))
    return im.crop((x0, y0, x0 + size, y0 + size)).resize((out, out), Image.LANCZOS)


def save(im, name, q=86):
    p = f'{OUT}/{name}'
    im.save(p, quality=q, optimize=True, progressive=True)
    print(f'{name:28} {os.path.getsize(p)/1024:6.1f} KB  {im.size}')


# --- the three states of one capillary-refill test -------------------------
# f150 pressure held on the nail bed · f168 the release (flash frame)
# · f212 colour has flowed back in
save(crop(150, 104, 150), 'crt-press.jpg')
save(crop(168, 100, 158, flash=True), 'crt-release.jpg')
save(crop(212, 91, 152), 'crt-refill.jpg')

# --- filmstrip thumbnails for "your clips come to you" ---------------------
for n, (idx, cx, cy) in enumerate(
        [(150, 104, 150), (168, 100, 158), (212, 91, 152), (240, 150, 150)], 1):
    save(crop(idx, cx, cy, out=192), f'crt-thumb{n}.jpg', q=80)
