"""Генератор тестовой схемы «Сердце» для отладки ядра.

Чистый Python без зависимостей: строит палитру, заполняет сетку
(фон-полосы с волнистыми границами, двухцветное сердце с контуром,
конфетти, пустые уголки), считает количество крестов по цветам.
"""
import math
import random

W, H = 80, 100

# (DMC, название, rgb, символ)
_PALETTE_SPEC = [
    ("712",  "кремовый",       "#F5E9D0", "·"),
    ("754",  "персиковый",     "#F6D5B8", "★"),
    ("894",  "розовый нежный", "#F3C2C0", "✚"),
    ("963",  "розовый",        "#E08E9C", "♥"),
    ("335",  "розовый насыщенный", "#C05E77", "▣"),
    ("3721", "сливовый",       "#8E4159", "◆"),
    ("726",  "золотой",        "#EDB34C", "●"),
    ("995",  "бирюзовый",      "#7FBFB4", "▲"),
    ("553",  "сиреневый",      "#B9A6CF", "✿"),
]


def _bg_color(x: int, y: int) -> int:
    """Волнистые горизонтальные полосы фона."""
    t = y + 7 * math.sin(x * 0.08) + 5 * math.sin(y * 0.06 + 1.7)
    if t < 28:
        return 0
    if t < 54:
        return 1
    if t < 80:
        return 2
    return 3


def _heart_inside(hx: float, hy: float) -> bool:
    """Классическое неявное уравнение сердца."""
    return (hx * hx + hy * hy - 1) ** 3 - hx * hx * hy ** 3 <= 0


def build_pattern() -> dict:
    random.seed(42)
    grid = [[-1] * W for _ in range(H)]
    confetti = [6, 7, 8]

    for y in range(H):
        for x in range(W):
            hx = (x + 0.5 - W / 2) / 26
            hy = (49.5 - y) / 26
            inside = _heart_inside(hx, hy)
            expanded = _heart_inside(hx / 0.93, hy / 0.93)

            if inside and not expanded:
                grid[y][x] = 5                      # контур ◆
            elif inside:
                # двухцветная заливка с волнистой границей + конфетти
                if random.random() < 0.04:
                    grid[y][x] = random.choice(confetti)
                elif hy + 0.12 * math.sin(hx * 4) > 0:
                    grid[y][x] = 3
                else:
                    grid[y][x] = 4
            else:
                grid[y][x] = _bg_color(x, y)
                if random.random() < 0.02:
                    grid[y][x] = random.choice(confetti)

    # пустые уголки — чтобы видеть отрисовку пустых клеток
    for y in range(H):
        for x in range(W):
            if (x + y < 7 or (W - 1 - x) + y < 7
                    or x + (H - 1 - y) < 7 or (W - 1 - x) + (H - 1 - y) < 7):
                grid[y][x] = -1

    counts = [0] * len(_PALETTE_SPEC)
    for row in grid:
        for c in row:
            if c >= 0:
                counts[c] += 1

    palette = [
        {"id": i, "dmc": dmc, "name": name, "rgb": rgb, "symbol": sym, "count": counts[i]}
        for i, (dmc, name, rgb, sym) in enumerate(_PALETTE_SPEC)
    ]
    return {
        "name": "Демо: Сердце",
        "width": W,
        "height": H,
        "source": {"type": "demo"},
        "palette": palette,
        "grid": grid,
        "quarters": [],
        "backstitches": [],
    }


if __name__ == "__main__":
    p = build_pattern()
    print(p["name"], p["width"], "x", p["height"])
    print("крестов:", sum(c["count"] for c in p["palette"]))
