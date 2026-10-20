# -*- coding: utf-8 -*-
"""生成应用图标：圆角蓝底 + 下箭头 + 云"""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角矩形底
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=56, fill=(42, 108, 214, 255))

# 云朵（三个圆 + 底座）
w = (255, 255, 255, 240)
d.ellipse([54, 92, 118, 156], fill=w)
d.ellipse([92, 68, 176, 152], fill=w)
d.ellipse([146, 96, 204, 154], fill=w)
d.rounded_rectangle([62, 128, 198, 158], radius=15, fill=w)

# 下箭头
d.polygon([(128, 200), (100, 162), (156, 162)], fill=(255, 255, 255, 255))
d.rounded_rectangle([120, 150, 136, 170], radius=4, fill=(255, 255, 255, 255))

img.save(r"C:\Users\Echo\.workbuddy\quark-watch\app.ico",
         sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(r"C:\Users\Echo\.workbuddy\quark-watch\app.png")
print("icon ok")
