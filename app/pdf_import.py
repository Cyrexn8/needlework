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


def page_glyphs(page, size=7.0):
    """Глифы шрифта CrossStitch (size~7 — клетки схемы, ~10 — легенды): (cx, cy, text)."""
    out = []
    d = page.get_text("dict")
    for blk in d["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                if "CrossStitch" in sp["font"] and abs(sp["size"] - size) < 1.0:
                    x = (sp["bbox"][0] + sp["bbox"][2]) / 2
                    y = (sp["bbox"][1] + sp["bbox"][3]) / 2
                    out.append((x, y, sp["text"].strip()))
    return out


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


def parse_legend(page, report):
    """Легенда: symbol_glyph → {'dmc': str, 'count': int, 'rgb': (r,g,b)|None}."""
    glyphs = [g for g in page_glyphs(page, 10.0) if g[2]]      # без пустых «отступных»
    words = page.get_text("words")
    images = []
    for i in page.get_images(full=True):
        for r in page.get_image_rects(i[0]):
            images.append(r)
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
        # ближайшая картинка-образец слева
        rgb = None
        cand = [r for r in images if abs((r.y0 + r.y1) / 2 - gy) < 6 and r.x1 <= gx + 2]
        if cand:
            r = min(cand, key=lambda r: gx - r.x1)
            pix = page.get_pixmap(clip=r)
            rgb = _pix_avg(pix)
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


def parse_chart_page(page, report):
    """Лист схемы → {col, row, color, symbol} в локальных индексах + бэкстич + ¾."""
    glyphs = page_glyphs(page, 7.0)
    strokes = page_colored_strokes(page)
    if len(glyphs) < 100:
        return None

    gx = cluster_1d([g[0] for g in glyphs])
    gy = cluster_1d([g[1] for g in glyphs])
    # шаг сетки: весь диапазон / число промежутков (устойчиво к «дрожанию» координат)
    pitch_x = (gx[-1] - gx[0]) / (len(gx) - 1)
    pitch_y = (gy[-1] - gy[0]) / (len(gy) - 1)

    def to_col(x):
        return round((x - gx[0]) / pitch_x)

    def to_row(y):
        return round((y - gy[0]) / pitch_y)

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
            # заливка без символа-глифа: треугольник → ¾ креста, иначе игнор
            tri = [(it[1], it[2]) for it in dr["items"] if it[0] == "l"]
            if len(dr["items"]) == 3 and len(tri) == 3:
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
            else:
                report.append(f"! заливка без символа при ({r.x0:.1f},{r.y0:.1f}) — пропущена")

    # линейки: Arial ~6pt с номерами, кратными 10
    rulers = []
    d = page.get_text("dict")
    for blk in d["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                if "Arial" in sp["font"] and abs(sp["size"] - 6.0) < 0.5:
                    t = sp["text"].strip()
                    if re.fullmatch(r"\d+", t) and int(t) % 10 == 0:
                        cx = (sp["bbox"][0] + sp["bbox"][2]) / 2
                        cy = (sp["bbox"][1] + sp["bbox"][3]) / 2
                        rulers.append({"t": t, "x": cx, "y": cy,
                                       "horiz": cy < gy[0] or cy > gy[-1]})

    def axis_offset(c0, pitch, rs, use_x):
        """Сопоставить линейки (номера линий сетки) позиционным индексам клеток."""
        offs = []
        for r in rs:
            v = r["x"] if use_x else r["y"]
            if not (c0 - pitch <= v <= c0 + 100 * pitch):
                continue
            k = round((v - c0) / pitch)
            offs.append(int(r["t"]) - k)
        if not offs:
            return None
        most, _ = Counter(offs).most_common(1)[0]
        return most

    off_x = axis_offset(gx[0], pitch_x, [r for r in rulers if r["horiz"]], True)
    off_y = axis_offset(gy[0], pitch_y, [r for r in rulers if not r["horiz"]], False)
    if off_x is None or off_y is None:
        report.append("! линейки не найдены — смещение листа неизвестно")
        return None

    # бэкстич: точки штрихов в координатах клеток (центр клетки = целое)
    backstitch = []
    for rgb, pts in strokes:
        seg = []
        for x, y in pts:
            u = (x - gx[0]) / pitch_x + off_x
            v = (y - gy[0]) / pitch_y + off_y
            seg.append((round(u * 4) / 4, round(v * 4) / 4))
        backstitch.append({"color": rgb, "points": seg})

    return {
        "off_x": off_x, "off_y": off_y,
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

    # --- классификация страниц ---
    legends, bs_legends, charts = [], [], []
    for idx, page in enumerate(pages):
        n_chart = len(page_glyphs(page, 7.0))
        n_legend = len(page_glyphs(page, 10.0))
        if 20 <= n_legend <= 200:
            legends.append(idx)
        if n_legend < 10 and n_chart < 10 and page_colored_strokes(page):
            bs_legends.append(idx)
        if n_chart >= 500:
            charts.append(idx)
    if not charts:
        raise SystemExit("Не найдены страницы-листы схемы (нет глифов CrossStitch)")
    if not legends:
        raise SystemExit("Не найдена страница-легенда")
    report.append(f"листы схемы: {[i + 1 for i in charts]}")
    report.append(f"страницы легенды: {[i + 1 for i in legends]}, бэкстич-легенда: {[i + 1 for i in bs_legends]}")

    # --- размер из заголовка легенды ---
    m = re.search(r"стежках:\s*(\d+)\s*[x×]\s*(\d+)", pages[legends[0]].get_text())
    width, height = (int(m.group(1)), int(m.group(2))) if m else (None, None)
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
        data = parse_chart_page(pages[idx], report)
        if data is None:
            continue
        parsed_pages.append(data)
        if label == 1:
            data["off_x"], data["off_y"] = 0, 0   # «Страница 1» — левый верх схемы
        # выравнивание по содержимому: вокруг линеечной оценки ищем смещение
        # с минимумом конфликтов на перекрытии с уже собранной сеткой
        best_off, best_score = (data["off_x"], data["off_y"]), None
        for ddx in range(-5, 6):
            for ddy in range(-5, 6):
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

    # --- сетка в pattern.json ---
    maxc = max(c for c, _ in grid) if grid else 0
    maxr = max(r for _, r in grid) if grid else 0
    if width is None:
        width, height = maxc + 1, maxr + 1
    w, h = max(width, maxc + 1), max(height, maxr + 1)
    g = [[-1] * w for _ in range(h)]
    missing = 0
    for (c, r), cell in grid.items():
        if 0 <= c < w and 0 <= r < h:
            g[r][c] = sym_to_id[cell["symbol"]]
        else:
            missing += 1
    if missing:
        report.append(f"! {missing} клеток вне размера {w}x{h}")

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
            {"x": q["x"], "y": q["y"],
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
