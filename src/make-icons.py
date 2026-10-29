from PIL import Image, ImageDraw
S = 1024
W  = (255, 255, 255)
AMBER = (255, 196, 64)
INK  = (13, 26, 45)          # 芯片内部底色（玻璃腔）

def lerp(a, b, t): return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

# ── 满铺直角底：纵向三段冷色渐变 ──
img = Image.new("RGB", (S, S))
d = ImageDraw.Draw(img)
c0, c1, c2 = (9, 15, 28), (18, 62, 98), (18, 140, 130)
for y in range(S):
    t = y / (S - 1)
    d.line([(0, y), (S, y)], fill=(lerp(c0, c1, t/0.6) if t < 0.6 else lerp(c1, c2, (t-0.6)/0.4)))

# ── 居中柔和径向光晕（不再偏左上）──
glow = Image.new("L", (S, S), 0)
ImageDraw.Draw(glow).ellipse([S*0.10, S*0.06, S*0.90, S*0.78], fill=52)
img = Image.composite(Image.new("RGB", (S, S), (120, 190, 255)), img, glow.point(lambda v: int(v*0.30)))
d = ImageDraw.Draw(img)

cx = S // 2
LW = 46                              # 线宽
X0, X1 = cx - 300, cx + 300          # 芯片左右
Y0, Y1 = 240, 800                    # 芯片上下
IN_TOP, IN_BOT = Y0 + LW//2, Y1 - LW//2

# ── 引脚（3 对，加粗加长，与边框同宽）──
PIN_W, PIN_L = 40, 74
for k in (-1, 0, 1):
    yy = 512 + k * 140
    d.rounded_rectangle([X0 - PIN_L, yy - PIN_W//2, X0 + LW//2, yy + PIN_W//2], radius=14, fill=W)
    d.rounded_rectangle([X1 - LW//2, yy - PIN_W//2, X1 + PIN_L, yy + PIN_W//2], radius=14, fill=W)

# ── 芯片框 ──
d.rounded_rectangle([X0, Y0, X1, Y1], radius=56, outline=W, width=LW)

# ── 温度计 ──
R   = 82                             # 球泡半径
YB  = IN_BOT - 50 - R                # 球泡圆心：底部留 50px 空隙，别贴边框
YT  = IN_TOP + 46                    # 管顶
TW  = 44                             # 管半宽

# 玻璃管外壁（白）+ 腔体（深色）
d.rounded_rectangle([cx-TW, YT, cx+TW, YB], radius=TW, fill=W)
d.rounded_rectangle([cx-TW+22, YT+22, cx+TW-22, YB], radius=TW-22, fill=INK)
# 球泡外壁（白）+ 腔体（深色）
d.ellipse([cx-R, YB-R, cx+R, YB+R], fill=W)
d.ellipse([cx-R+22, YB-R+22, cx+R-22, YB+R-22], fill=INK)

# 液柱：从球泡腔往上到约 62% 处（圆头）
LIQ_TOP = YT + int((YB - YT) * 0.34)
d.rounded_rectangle([cx-TW+22, LIQ_TOP, cx+TW-22, YB], radius=TW-22, fill=AMBER)
d.ellipse([cx-R+22, YB-R+22, cx+R-22, YB+R-22], fill=AMBER)

img = img.convert("RGB")
for path, size in [("ICON_256.PNG",256), ("ICON.PNG",64), ("ICON_64.PNG",64),
                   ("app/ui/images/icon_256.png",256), ("app/ui/images/icon_64.png",64)]:
    img.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
print("  已生成 5 个尺寸")
