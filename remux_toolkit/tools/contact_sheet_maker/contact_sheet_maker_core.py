# remux_toolkit/tools/contact_sheet_maker/contact_sheet_maker_core.py

import os
import math
from PIL import Image, ImageDraw, ImageFont
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

class Worker(QObject):
    """A worker to generate the contact sheet in a background thread."""
    # current, total
    progress = pyqtSignal(int, int)
    # path_or_error_string, success_bool
    finished = pyqtSignal(str, bool)

    @pyqtSlot(dict)
    def make_sheet(self, params: dict):
        try:
            png_dir = params['png_dir']
            out = params['out']
            cols = params.get('cols', 8)
            limit = params.get('limit')
            thumb_w = params.get('thumb_w', 400)
            thumb_h = params.get('thumb_h', 150)
            label_h = params.get('label_h', 22)
            pad = params.get('pad', 8)

            files = [os.path.join(png_dir, f) for f in sorted(os.listdir(png_dir)) if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp"))]
            if not files:
                self.finished.emit("No supported images found in the directory.", False)
                return

            if limit and limit > 0:
                files = files[:limit]

            n = len(files)
            rows = math.ceil(n / cols)
            cell_w, cell_h = thumb_w, thumb_h + label_h
            W = cols * cell_w + (cols + 1) * pad
            H = rows * cell_h + (rows + 1) * pad
            sheet = Image.new("RGBA", (W, H), (45, 45, 45, 255)) # Dark background
            draw = ImageDraw.Draw(sheet)
            try:
                font = ImageFont.truetype("tahoma.ttf", 14)
            except IOError:
                font = ImageFont.load_default()


            def fit(im):
                w, h = im.size
                if w == 0 or h == 0: return im
                s = min(thumb_w / w, thumb_h / h, 1.0)
                return im.resize((int(w * s), int(h * s)), Image.Resampling.LANCZOS)

            for i, path in enumerate(files):
                r, c = divmod(i, cols)
                x0 = pad + c * (cell_w + pad)
                y0 = pad + r * (cell_h + pad)

                im = Image.open(path).convert("RGBA")
                im = fit(im)
                ox = x0 + (thumb_w - im.width) // 2
                oy = y0 + (thumb_h - im.height) // 2
                sheet.paste(im, (ox, oy), im)

                draw.rectangle([x0, y0 + thumb_h, x0 + cell_w, y0 + cell_h], fill=(255, 255, 255, 255))
                name = os.path.basename(path)
                if len(name) > 50: name = name[:47] + "..."

                bbox = draw.textbbox((0, 0), name, font=font)
                tw = bbox[2] - bbox[0]

                tx = x0 + max(2, int((cell_w - tw) / 2))
                ty = y0 + thumb_h + (label_h - (bbox[3] - bbox[1])) / 2
                draw.text((tx, ty), name, fill=(0, 0, 0, 255), font=font)
                self.progress.emit(i + 1, n)

            sheet.save(out)
            self.finished.emit(out, True)

        except Exception as e:
            self.finished.emit(f"An error occurred: {e}", False)
