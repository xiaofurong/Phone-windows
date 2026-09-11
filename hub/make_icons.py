#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PWA / apple-touch-icon。匹配控制中心深色 UI。
关键: 整图就是一张圆角矩形卡片(深色渐变), 内容居中画 ——
iOS / maskable 无论怎么裁圆角都安全, 不会露白底。"""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "icons")
os.makedirs(HERE, exist_ok=True)

BG_TOP = (14, 17, 22)      # #0e1116
BG_MID = (24, 30, 40)
ACC = (79, 157, 255)       # #4f9dff
ACC2 = (51, 192, 138)      # #33c08a 绿
FG = (232, 237, 242)
CARD = (17, 24, 36)
ONLINE = (51, 192, 138)

def base_card(size, radius_ratio=0.225):
    """返回一张圆角矩形深色卡片(透明背景外是 0), 用于叠底。"""
    S = size
    img = Image.new("RGBA", (S, S), (0,0,0,0))
    # 圆角矩形渐变
    grad = Image.new("RGB", (S, S))
    gpx = grad.load()
    for y in range(S):
        t = y / S
        if t < 0.5:
            r = int(BG_TOP[0] + (BG_MID[0]-BG_TOP[0])*t*2)
            g = int(BG_TOP[1] + (BG_MID[1]-BG_TOP[1])*t*2)
            b = int(BG_TOP[2] + (BG_MID[2]-BG_TOP[2])*t*2)
        else:
            r = int(BG_MID[0] + (BG_TOP[0]-BG_MID[0])*((t-0.5)*2))
            g = int(BG_MID[1] + (BG_TOP[1]-BG_MID[1])*((t-0.5)*2))
            b = int(BG_MID[2] + (BG_TOP[2]-BG_MID[2])*((t-0.5)*2))
        for x in range(S):
            gpx[x, y] = (r, g, b)
    # 圆角遮罩
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0,0,S,S], radius=int(S*radius_ratio), fill=255)
    # 把渐变裁成圆角
    img.paste(grad, (0,0), mask)
    return img

def draw_icon(size):
    S = size
    card = base_card(S)

    # 中央内容(屏+条+点)
    overlay = Image.new("RGBA", (S, S), (0,0,0,0))
    od = ImageDraw.Draw(overlay)

    # 中央屏框: 圆角矩形, Acc 描边 + 卡片底
    cw, ch = S*0.62, S*0.56
    x0 = (S-cw)/2; y0 = (S-ch)/2 - S*0.04
    bw = max(2, S*0.045)
    rad = S*0.085
    # 屏底
    od.rounded_rectangle([x0, y0, x0+cw, y0+ch], radius=rad, fill=CARD)
    # 屏描边(发光: 先画宽一点的半透明)
    od.rounded_rectangle([x0-bw*0.4, y0-bw*0.4, x0+cw+bw*0.4, y0+ch+bw*0.4],
                         radius=rad+bw*0.4, outline=ACC+(220,), width=int(bw))
    od.rounded_rectangle([x0, y0, x0+cw, y0+ch], radius=rad, outline=ACC, width=int(bw))

    # 屏内三条横条(代表音量/亮度 阶梯信号)
    n = 3
    lx = x0 + cw*0.22
    rx = x0 + cw*0.78
    top = y0 + ch*0.26
    bh = ch*0.10
    gap = ch*0.10
    fracs = [1.0, 0.62, 0.34]
    cols = [ACC, FG, ACC2]
    for i in range(n):
        f = fracs[i]
        bx0 = lx + (1-f) * (rx-lx)
        by = top + i*(bh+gap)
        od.rounded_rectangle([bx0, by, rx, by+bh], radius=max(2, bh*0.4), fill=cols[i]+(255,))

    # 底部小圆点: "OK 按钮"
    dr = S*0.04
    dx = x0 + cw*0.5; dy = y0 + ch*0.82
    od.ellipse([dx-dr, dy-dr, dx+dr, dy+dr], fill=ONLINE+(255,))

    # 左上"在线"指示点(稍出框感)
    or_ = S*0.055
    ox = x0 - bw*0.6
    oy = y0 + bw*0.6
    od.ellipse([ox-or_, oy-or_, ox+or_, oy+or_], fill=ONLINE+(255,))
    od.ellipse([ox-or_*0.7, oy-or_*0.7, ox+or_*0.7, oy+or_*0.7], fill=(140,240,200,255))

    out = Image.alpha_composite(card, overlay)
    return out  # RGBA

# 输出: apple-touch-icon 180 无 alpha; icon-192/512 保留 alpha
img180 = draw_icon(180).convert("RGB")
img180.save(os.path.join(HERE, "apple-touch-icon.png"))
img192 = draw_icon(192)
img192.save(os.path.join(HERE, "icon-192.png"))
img512 = draw_icon(512)
img512.save(os.path.join(HERE, "icon-512.png"))
print("icons written to", HERE)
for f in sorted(os.listdir(HERE)):
    p = os.path.join(HERE, f)
    im = Image.open(p)
    print("  %-26s %dx%d mode=%s" % (f, im.width, im.height, im.mode))