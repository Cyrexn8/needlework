"""Импорт векторной PDF-схемы вышивки (экспорт генераторов схем, шрифт CrossStitch*).

Что извлекаем:
- страницы-листы схемы: символы (текстовые глифы CrossStitch7pt) + цветные заливки
  клеток (fs-прямоугольники) + цветные штрихи бэкстича + линейки (Arial 6pt);
- страницу-легенду: глиф ↔ номер DMC ↔ количество;
- страницу-легенду бэкстича: цвет линии ↔ номер DMC ↔ кол-во нитей.

Результат: pattern.json для ядра needlework + отчёт валидации в stdout.

Запуск: python app/pdf_import.py <file.pdf> <проект-папка> [имя]
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf


def cluster_1d(vals, tol=1.5):
    """Кластеризация 1D-координат с допуском (позиции в PDF слегка «дрожат»)."""
    vals = sorted(vals)
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def page_glyphs(page, size=None):
    """Глифы шрифта CrossStitch: (cx, cy, text).

    size=None — все размеры; число — с допуском ±1.0 (символы схемы 7–9pt,
    легенды ~10pt, маркеры ~3pt).
    """
    out = []
    d = page.get_text("dict")
    for blk in d["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                if "CrossStitch" in sp["font"] and (size is None or abs(sp["size"] - size) < 1.0):
                    x = (sp["bbox"][0] + sp["bbox"][2]) / 2
                    y = (sp["bbox"][1] + sp["bbox"][3]) / 2
                    out.append((x, y, sp["text"].strip()))
    return out


def cs_sizes(page):
    """Counter размеров глифов CrossStitch на странице."""
    c = Counter()
    d = page.get_text("dict")
    for blk in d["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                if "CrossStitch" in sp["font"]:
                    c[round(sp["size"], 1)] += 1
    return c


def page_cell_fills(page):
    """Заливки размером с клетку: (cx, cy, rgb)."""
    out = []
    for dr in page.get_drawings():
        r = dr["rect"]
        if dr["type"] in ("f", "fs") and dr.get("fill") and 3 < r.width < 20 and 3 < r.height < 20:
            out.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, tuple(dr["fill"])))
    return out


def _is_cell_sized(dr):
    r = dr["rect"]
    return 3 < r.width < 20 and 3 < r.height < 20


def page_colored_strokes(page):
    """Цветные штрихи (бэкстич): (rgb, [(x, y), ...]) — только насыщенные цвета.

    Клеточные заливки (fs с размером клетки) исключаются — их контуры не бэкстич.
    """
    def saturated(c):
        return c and (max(c) - min(c) > 0.25)
    out = []
    for dr in page.get_drawings():
        c = dr.get("color")
        if "f" in dr["type"] and _is_cell_sized(dr):
            continue  # заливка клетки
        if "s" in dr["type"] and saturated(c):
            pts = []
            for it in dr["items"]:
                if it[0] == "l":
                    pts.append((it[1].x, it[1].y))
                    pts.append((it[2].x, it[2].y))
                elif it[0] == "c":  # кривая — берём концы
                    pts.append((it[1].x, it[1].y))
                    pts.append((it[4].x, it[4].y))
                elif it[0] == "re":
                    r = it[1]
                    pts.extend([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)])
            # сжатие подряд идущих дубликатов
            compact = [pts[0]]
            for p in pts[1:]:
                if abs(p[0] - compact[-1][0]) > 0.01 or abs(p[1] - compact[-1][1]) > 0.01:
                    compact.append(p)
            if len(compact) >= 2:
                out.append((tuple(round(v, 3) for v in c), compact))
    return out


def nearest_index(coord, centers):
    return min(range(len(centers)), key=lambda i: abs(centers[i] - coord))


def _linfit(points):
    """МНК-прямая y = a*x + b по точкам (минимум 2)."""
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return a, b


def parse_legend(page, report):
    """Легенда: symbol_glyph → {'dmc': str, 'count': int, 'rgb': (r,g,b)|None}.

    Образцы цвета — картинки или векторные прямоугольники-заливки слева от глифа.
    """
    glyphs = [g for g in page_glyphs(page) if g[2]]            # без пустых «отступных»
    words = page.get_text("words")
    images = []
    for i in page.get_images(full=True):
        for r in page.get_image_rects(i[0]):
            images.append(r)
    vector_swatches = [dr["rect"] for dr in page.get_drawings()
                       if dr["type"] in ("f", "fs") and dr.get("fill")
                       and 3 < dr["rect"].width < 20 and 3 < dr["rect"].height < 20]
    entries = {}
    for gx, gy, sym in glyphs:
        # ближайшее слово справа в той же строке: сначала DMC, затем количество
        row = sorted([w for w in words
                      if abs((w[1] + w[3]) / 2 - gy) < 4 and w[0] > gx - 2],
                     key=lambda w: w[0])
        nums = [w[4] for w in row if re.fullmatch(r"\d+([+]\d+)*", w[4])]
        if not nums:
            continue
        dmc = nums[0]
        cnt = int(nums[1]) if len(nums) > 1 else None
        # ближайший образец слева: картинка или векторная заливка
        rgb = None
        img_cand = [r for r in images if abs((r.y0 + r.y1) / 2 - gy) < 6 and r.x1 <= gx + 2]
        vec_cand = [r for r in vector_swatches
                    if abs((r.y0 + r.y1) / 2 - gy) < 6 and r.x1 <= gx + 2]
        if img_cand and (not vec_cand or min(gx - r.x1 for r in img_cand)
                         <= min(gx - r.x1 for r in vec_cand)):
            r = min(img_cand, key=lambda r: gx - r.x1)
            pix = page.get_pixmap(clip=r)
            rgb = _pix_avg(pix)
        elif vec_cand:
            r = min(vec_cand, key=lambda r: gx - r.x1)
            for dr in page.get_drawings():
                if dr["rect"] == r and dr.get("fill"):
                    rgb = tuple(dr["fill"])
                    break
        if sym in entries and entries[sym]["dmc"] != dmc:
            report.append(f"! легенда: символ {sym!r} встречен с разными номерами: "
                          f"{entries[sym]['dmc']} и {dmc}")
        entries[sym] = {"dmc": dmc, "count": cnt, "rgb": rgb}
    return entries


def _pix_avg(pix):
    """Средний цвет пиксельного образца."""
    n = pix.width * pix.height
    if not n:
        return None
    s = [0, 0, 0]
    for i in range(0, len(pix.samples), pix.n):
        for ch in range(3):
            s[ch] += pix.samples[i + ch]
    return tuple(round(v / n, 3) for v in s)


def parse_backstitch_legend(page):
    """Легенда бэкстича: rgb линии → {'dmc': str, 'strands': int}."""
    words = page.get_text("words")
    mapping = {}
    for rgb, pts in page_colored_strokes(page):
        ys = [p[1] for p in pts]
        cy = sum(ys) / len(ys)
        row = sorted([w for w in words
                      if abs((w[1] + w[3]) / 2 - cy) < 6 and w[0] > max(p[0] for p in pts)],
                     key=lambda w: w[0])
        nums = [w[4] for w in row if re.fullmatch(r"\d+", w[4])]
        if len(nums) >= 2:
            mapping[rgb] = {"dmc": nums[0], "strands": int(nums[1])}
    return mapping


def parse_chart_page(page, report, chart_size):
    """Лист схемы → клетки в глобальных индексах + бэкстич + ¾-кресты.

    Координаты клеток: аффинное отображение «точка PDF → номер клетки»,
    построенное по линейкам (МНК) — устойчиво к пустым колонкам/рядам и
    «дрожанию» координат. Без линеек — позиционное приближение.
    """
    glyphs = page_glyphs(page, chart_size)
    strokes = page_colored_strokes(page)
    if len(glyphs) < 100:
        return None

    gx = cluster_1d([g[0] for g in glyphs])
    gy = cluster_1d([g[1] for g in glyphs])
    # шаг сетки: весь диапазон / число промежутков (устойчиво к «дрожанию» координат)
    pitch_x = (gx[-1] - gx[0]) / (len(gx) - 1)
    pitch_y = (gy[-1] - gy[0]) / (len(gy) - 1)

    # линейки: Arial ~6pt с номерами, кратными 10; ось — по положению относительно сетки
    rulers_h, rulers_v = [], []   # rulers_h: (x, номер колонки); rulers_v: (y, номер ряда)
    d = page.get_text("dict")
    for blk in d["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                if "Arial" in sp["font"] and abs(sp["size"] - 6.0) < 0.5:
                    t = sp["text"].strip()
                    if not (re.fullmatch(r"\d+", t) and int(t) % 10 == 0):
                        continue
                    cx = (sp["bbox"][0] + sp["bbox"][2]) / 2
                    cy = (sp["bbox"][1] + sp["bbox"][3]) / 2
                    above_or_below = cy < gy[0] - 0.4 * pitch_y or cy > gy[-1] + 0.4 * pitch_y
                    left_or_right = cx < gx[0] - 0.4 * pitch_x or cx > gx[-1] + 0.4 * pitch_x
                    if above_or_below:
                        rulers_h.append((cx, int(t)))
                    elif left_or_right:
                        rulers_v.append((cy, int(t)))

    def fit_axis(pts, c0, pitch):
        """Аффинное отображение «координата → индекс клетки»: (a, b)."""
        if len(pts) >= 2:
            fit = _linfit(pts)
            if fit:
                return fit
        return 1.0 / pitch, -c0 / pitch   # резерв: позиционное приближение

    fx = fit_axis(rulers_h, gx[0], pitch_x)
    fy = fit_axis(rulers_v, gy[0], pitch_y)
    if not rulers_h or not rulers_v:
        report.append("! линейки не найдены — позиционное приближение")

    # глифы из шапки листа (над верхней линейкой / левее левой) — не клетки схемы
    top_bound = min((y for _, y in rulers_h), default=-1e9) - 0.5 * pitch_y
    left_bound = min((x for _, x in rulers_v), default=-1e9) - 0.5 * pitch_x
    glyphs = [g for g in glyphs if g[1] >= top_bound and g[0] >= left_bound]

    def to_col(x):
        return round(fx[0] * x + fx[1])

    def to_row(y):
        return round(fy[0] * y + fy[1])

    cells = {}
    quarters = []
    color_by_symbol = defaultdict(list)
    for gx0, gy0, sym in glyphs:
        cells[(to_col(gx0), to_row(gy0))] = {"symbol": sym}
    for dr in page.get_drawings():
        r = dr["rect"]
        if not (dr["type"] in ("f", "fs") and dr.get("fill") and _is_cell_sized(dr)):
            continue
        rgb = tuple(dr["fill"])
        col = to_col((r.x0 + r.x1) / 2)
        row = to_row((r.y0 + r.y1) / 2)
        cell = cells.get((col, row))
        if cell is not None:
            cell["color"] = rgb
            color_by_symbol[cell["symbol"]].append(rgb)
        else:
            # без глифа: чёрные треугольники — метки перекрытий листов, серые полосы —
            # зона перекрытия соседнего листа: пропускаем; цветные треугольники — ¾
            if max(rgb) < 0.15:
                continue
            tri = [(it[1], it[2]) for it in dr["items"] if it[0] == "l"]
            if len(dr["items"]) == 3 and len(tri) == 3 and 0 <= col and 0 <= row:
                vs = []
                for a, b in tri:
                    for p in (a, b):
                        if not any(abs(p.x - v[0]) < 0.05 and abs(p.y - v[1]) < 0.05
                                   for v in vs):
                            vs.append((p.x, p.y))
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                quarters.append({
                    "col": col, "row": row, "color": rgb,
                    "pts": [[round((v[0] - cx) / pitch_x, 2), round((v[1] - cy) / pitch_y, 2)]
                            for v in vs],
                })

    # бэкстич: точки штрихов в координатах клеток (границы клеток = полуклетки)
    backstitch = []
    for rgb, pts in strokes:
        seg = []
        for x, y in pts:
            u = fx[0] * x + fx[1] - 0.5
            v = fy[0] * y + fy[1] - 0.5
            seg.append((round(u * 4) / 4, round(v * 4) / 4))
        backstitch.append({"color": rgb, "points": seg})

    return {
        "off_x": 0, "off_y": 0,   # координаты уже глобальные (аффинно по линейкам)
        "ncols": len(gx), "nrows": len(gy),
        "cells": cells,
        "quarters": quarters,
        "color_by_symbol": {k: v for k, v in color_by_symbol.items()},
        "backstitch": backstitch,
    }


def guess_rgb(symbol, color_by_symbol, legend_rgb):
    if color_by_symbol.get(symbol):
        cols = color_by_symbol[symbol]
        n = len(cols)
        return tuple(round(sum(c[i] for c in cols) / n, 3) for i in range(3))
    return legend_rgb


def import_pdf(pdf_path, out_dir, name=None):
    report = []
    doc = pymupdf.open(pdf_path)
    pages = list(doc)

    # --- размер глифов-символов: самый массовый размер на «плотных» страницах ---
    size_totals = Counter()
    for page in pages:
        for s, n in cs_sizes(page).items():
            if n >= 200:
                size_totals[s] += n
    if not size_totals:
        raise SystemExit("Не найдены страницы-листы схемы (нет глифов CrossStitch)")
    chart_size = size_totals.most_common(1)[0][0]

    # --- классификация страниц ---
    legends, bs_legends, charts = [], [], []
    for idx, page in enumerate(pages):
        sizes = cs_sizes(page)
        n_chart = sizes.get(chart_size, 0)
        n_any = sum(sizes.values())
        if n_chart >= 200:
            charts.append(idx)
        elif 8 <= n_any <= 400:
            legends.append(idx)
        elif n_any == 0 and len(page.get_drawings()) > 20 and page_colored_strokes(page):
            bs_legends.append(idx)
    if not legends:
        raise SystemExit("Не найдена страница-легенда")
    report.append(f"листы схемы: {[i + 1 for i in charts]} (шрифт {chart_size}pt)")
    report.append(f"страницы легенды: {[i + 1 for i in legends]}, бэкстич-легенда: {[i + 1 for i in bs_legends]}")

    # --- размер из заголовка легенды или обложки ---
    width = height = None
    size_patterns = [r"стежках:\s*(\d+)\s*[xх×]\s*(\d+)",
                     r"[Рр]азмер:?\s*(\d+)\s*[xх×]\s*(\d+)\s*крест"]
    for probe in [pages[i] for i in legends] + [pages[0]]:
        text = probe.get_text()
        for pat in size_patterns:
            m = re.search(pat, text)
            if m:
                width, height = int(m.group(1)), int(m.group(2))
                break
        if width:
            break
    report.append(f"размер схемы: {width} x {height}")

    # --- листы: среди найденных выбрать по 4 уникальных «Страница N» (приоритет — цветная версия с бэкстичем) ---
    def page_label(idx):
        words = pages[idx].get_text("words")
        for i, w in enumerate(words):
            if w[4] == "Страница" and i + 1 < len(words):
                return int(words[i + 1][4])
        return None

    groups = defaultdict(list)
    for idx in charts:
        groups[page_label(idx)].append(idx)
    picked = []
    for label, idxs in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0])):
        # цветная версия = много клеточных заливок; приоритет — с цветным бэкстичем
        color_with_bs = [i for i in idxs if len(page_cell_fills(pages[i])) > 100
                         and page_colored_strokes(pages[i])]
        color = [i for i in idxs if len(page_cell_fills(pages[i])) > 100]
        picked.append((label, color_with_bs[0] if color_with_bs else
                       (color[0] if color else idxs[0])))
    report.append(f"выбраны листы: {[(lb, i + 1) for lb, i in picked]}")

    # --- парсим легенды ---
    legend = parse_legend(pages[legends[0]], report)
    report.append(f"легенда: {len(legend)} символов")
    bs_legend = {}
    for i in bs_legends:
        bs_legend.update(parse_backstitch_legend(pages[i]))
    report.append(f"легенда бэкстича: {len(bs_legend)} цветов")

    # --- парсим листы и склеиваем ---
    grid = {}
    all_backstitch = []
    all_quarters = []
    fills_by_symbol = defaultdict(list)
    parsed_pages = []
    for label, idx in picked:
        data = parse_chart_page(pages[idx], report, chart_size)
        if data is None:
            continue
        parsed_pages.append(data)
        # выравнивание по содержимому: вокруг линеечной оценки ищем смещение
        # с минимумом конфликтов на перекрытии с уже собранной сеткой
        best_off, best_score = (data["off_x"], data["off_y"]), None
        for ddx in range(-10, 11):
            for ddy in range(-10, 11):
                cx, cy = data["off_x"] + ddx, data["off_y"] + ddy
                matched = conflicts = 0
                for (col, row), cell in data["cells"].items():
                    g = grid.get((col + cx, row + cy))
                    if g is not None:
                        matched += 1
                        if g["symbol"] != cell["symbol"]:   # цвет может отличаться (серые зоны)
                            conflicts += 1
                if matched < 20:
                    continue
                score = (conflicts, -matched, abs(ddx) + abs(ddy))
                if best_score is None or score < best_score:
                    best_score, best_off = score, (cx, cy)
        if best_score is not None:
            data["off_x"], data["off_y"] = best_off
            report.append(f"лист {label}: смещение ({best_off[0]},{best_off[1]}), "
                          f"наложение {best_score[1]} клеток, конфликтов {best_score[0]}")
        else:
            report.append(f"лист {label}: смещение по линейкам ({data['off_x']},{data['off_y']})")
        for (col, row), cell in data["cells"].items():
            grid[(col + data["off_x"], row + data["off_y"])] = cell
        for b in data["backstitch"]:
            all_backstitch.append(b)
        for q in data["quarters"]:
            all_quarters.append({
                "x": q["col"] + data["off_x"], "y": q["row"] + data["off_y"],
                "rgb": q["color"], "pts": q["pts"],
            })
        for sym, cols in data["color_by_symbol"].items():
            fills_by_symbol[sym].extend(cols)

    # --- сборка палитры ---
    sym_stats = Counter(cell["symbol"] for cell in grid.values())
    palette = []
    sym_to_id = {}
    for sym, count in sorted(sym_stats.items(), key=lambda kv: -kv[1]):
        leg = legend.get(sym, {})
        cols = fills_by_symbol.get(sym)
        if cols:
            n = len(cols)
            rgb = tuple(round(sum(c[i] for c in cols) / n, 3) for i in range(3))
        else:
            rgb = leg.get("rgb")
        palette.append({
            "id": len(palette),
            "dmc": leg.get("dmc", "?"),
            "name": "",
            "rgb": _rgb_hex(rgb) if rgb else "#CCCCCC",
            "symbol": sym,
            "count": count,
        })
        sym_to_id[sym] = palette[-1]["id"]

    # --- сетка в pattern.json: нормализуем отрицательные индексы, размер = факт ∪ легенда ---
    if grid:
        minc = min(c for c, _ in grid)
        minr = min(r for _, r in grid)
        maxc = max(c for c, _ in grid) - minc + 1
        maxr = max(r for _, r in grid) - minr + 1
        if minc or minr:
            report.append(f"! сетка началась с ({minc},{minr}) — сдвигаю к нулю")
            grid = {(c - minc, r - minr): cell for (c, r), cell in grid.items()}
    else:
        minc = minr = maxc = maxr = 0
    w = max(width or 0, maxc)
    h = max(height or 0, maxr)
    if (width, height) and (w, h) != (width, height):
        report.append(f"! фактический размер {w}x{h} отличается от заявленного {width}x{height}")
    g = [[-1] * w for _ in range(h)]
    for (c, r), cell in grid.items():
        g[r][c] = sym_to_id[cell["symbol"]]

    # --- бэкстич: цвет → dmc из легенды ---
    backstitches = []
    for b in all_backstitch:
        pts = [(u, v) for u, v in b["points"]]
        info = bs_legend.get(tuple(b["color"]), {})
        backstitches.append({
            "color": info.get("dmc", _rgb_hex(b["color"])),
            "points": pts,
        })
    # дедуп (перекрытия листов дублируют линии)
    seen, uniq = set(), []
    for b in backstitches:
        key = (b["color"], tuple(b["points"]))
        if key not in seen:
            seen.add(key)
            uniq.append(b)

    pattern = {
        "name": name or Path(pdf_path).stem,
        "width": w,
        "height": h,
        "source": {"type": "pdf", "file": Path(pdf_path).name},
        "palette": palette,
        "grid": g,
        "quarters": [
            {"x": q["x"] - minc, "y": q["y"] - minr,
             "color": next((p["dmc"] for p in palette
                            if p["rgb"] == _rgb_hex(q["rgb"])), _rgb_hex(q["rgb"])),
             "pts": q["pts"]}
            for q in all_quarters
        ],
        "backstitches": uniq,
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pattern.json").write_text(
        json.dumps(pattern, ensure_ascii=False), encoding="utf-8")
    print("\n".join(report))
    print(f"сохранено: {out / 'pattern.json'}")
    print(f"итог: {w}x{h}, цветов {len(palette)}, клеток {sum(p['count'] for p in palette)}, "
          f"бэкстича {len(uniq)}")
    return pattern


def _rgb_hex(rgb):
    if not rgb:
        return "#CCCCCC"
    return "#%02X%02X%02X" % tuple(min(255, max(0, round(v * 255))) for v in rgb[:3])


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    import_pdf(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
