"""
从有道云笔记官方导出的文件导入到 Notion
支持格式：.md, .pdf, .html, .txt
"""

import json
import os
import re
import sys
import time
import fitz  # pymupdf
from pathlib import Path
from notion_client import Client
from bs4 import BeautifulSoup

import config
from converter import markdown_to_notion_blocks, html_to_markdown

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(msg):
    print(msg, flush=True)


def _extract_quote_ranges(page) -> list[tuple]:
    """检测引用块的竖线标记，返回 [(y0, y1), ...] 表示引用区域的 y 范围。
    有道云导出的引用是左侧一条灰色竖线。
    """
    drawings = page.get_drawings()
    ranges = []
    for d in drawings:
        rect = d["rect"]
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        fill = d.get("fill")
        # 细竖线（宽 < 5, 高 > 15），非黑非白填充
        if w < 5 and h > 15 and fill and fill != (1.0, 1.0, 1.0) and fill != (0.0, 0.0, 0.0):
            # 灰色或彩色都算
            ranges.append((rect[1], rect[3]))
    # 合并重叠区域
    if ranges:
        ranges.sort()
        merged = [ranges[0]]
        for y0, y1 in ranges[1:]:
            if y0 <= merged[-1][1] + 5:
                merged[-1] = (merged[-1][0], max(merged[-1][1], y1))
            else:
                merged.append((y0, y1))
        return merged
    return []


def _extract_checkboxes(page) -> list[tuple]:
    """从 PDF 页面的矢量绘图中检测 checkbox 及其勾选状态。
    返回 [(y, x, checked: bool), ...] 列表。
    有道云导出的 checkbox 是两个矢量图形：圆角方框 + 勾号（如果已勾选）。
    """
    drawings = page.get_drawings()
    boxes = []   # checkbox 方框
    checks = []  # 勾号

    for d in drawings:
        rect = d["rect"]
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        color = d.get("color")
        fill = d.get("fill")
        item_types = [it[0] for it in d.get("items", [])]

        # checkbox 方框：~10x10 像素，有边框色无填充，含曲线（圆角）
        if 9 < w < 14 and 9 < h < 14 and color and not fill and "c" in item_types:
            boxes.append((rect[0], rect[1], rect[2], rect[3]))
        # 勾号：~5x5 像素，有边框色无填充
        elif 3 < w < 8 and 3 < h < 8 and color and not fill:
            checks.append((rect[0], rect[1], rect[2], rect[3]))

    result = []
    for bx, by, bx2, by2 in boxes:
        has_check = any(
            abs(cy - by) < 5 and abs(cx - bx) < 5
            for cx, cy, _, _ in checks
        )
        result.append((by, bx, has_check))
    return result


def _extract_bullets(page) -> list[tuple]:
    """检测无序列表标记（3 级），返回 [(y, x, level), ...] 列表。
    有道云导出的无序列表：
      一级 • 实心圆：x≈34, fill=黑, 全曲线(c), 16 items
      二级 ○ 空心圆：x≈55, fill=None, color=黑, 全曲线(c), 16 items
      三级 ■ 实心方块：x≈76, fill=黑, items=['re'], 1 item
    """
    drawings = page.get_drawings()
    bullets = []
    for d in drawings:
        rect = d["rect"]
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if not (3 < w < 8 and 3 < h < 8):
            continue
        fill = d.get("fill")
        color = d.get("color")
        item_types = [it[0] for it in d.get("items", [])]
        # 一级：实心圆（黑色填充，无边框色，贝塞尔曲线）
        if fill == (0.0, 0.0, 0.0) and not color and "c" in item_types:
            bullets.append((rect[1], rect[0], 1))
        # 二级：空心圆（无填充，黑色边框，贝塞尔曲线）
        elif not fill and color == (0.0, 0.0, 0.0) and "c" in item_types:
            bullets.append((rect[1], rect[0], 2))
        # 三级：实心方块（黑色填充，re 矩形）
        elif fill == (0.0, 0.0, 0.0) and "re" in item_types and len(item_types) == 1:
            bullets.append((rect[1], rect[0], 3))
    return bullets


def pdf_to_markdown(pdf_path: str) -> str:
    """从 PDF 提取文字内容，根据字体大小识别标题层级，检测 checkbox，转为 Markdown"""
    try:
        doc = fitz.open(pdf_path)
        page_width = doc[0].rect.width if len(doc) > 0 else 595

        # 第一遍：收集所有字体大小，确定标题阈值
        all_sizes = []
        for page in doc:
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if text:
                            all_sizes.append(span["size"])

        if not all_sizes:
            doc.close()
            return ""

        # 找出正文字号（出现最多的）和各级标题字号
        from collections import Counter
        size_counts = Counter(round(s, 1) for s in all_sizes)
        body_size = size_counts.most_common(1)[0][0]

        # 收集所有大于正文的字号，从大到小排序作为标题层级
        heading_sizes = sorted(set(s for s in size_counts if s > body_size), reverse=True)
        # 最多支持 3 级标题
        heading_sizes = heading_sizes[:3]
        # 建立字号 → 标题级别的映射
        size_to_level = {}
        for i, s in enumerate(heading_sizes):
            size_to_level[s] = i + 1  # h1, h2, h3

        # 提取每页的 checkbox 信息
        page_checkboxes = {}
        for page_num, page in enumerate(doc):
            cbs = _extract_checkboxes(page)
            if cbs:
                page_checkboxes[page_num] = cbs

        # 提取每页的无序列表圆点
        page_bullets = {}
        for page_num, page in enumerate(doc):
            bts = _extract_bullets(page)
            if bts:
                page_bullets[page_num] = bts

        # 提取每页的引用区域
        page_quotes = {}  # {page_num: [(y0, y1), ...]}
        for page_num, page in enumerate(doc):
            qrs = _extract_quote_ranges(page)
            if qrs:
                page_quotes[page_num] = qrs

        # 提取每页的表格信息
        page_tables = {}  # {page_num: [(y, markdown_table_str), ...]}
        page_table_rects = {}  # {page_num: [(x0, y0, x1, y1), ...]} 用于跳过表格区域内的文本
        for page_num, page in enumerate(doc):
            try:
                tables = page.find_tables()
                if tables.tables:
                    tbl_list = []
                    rect_list = []
                    for tab in tables.tables:
                        rows = tab.extract()
                        if not rows:
                            continue
                        # 过滤掉被表格误吞的非表格行（最后一行如果其余列全是None且内容很长）
                        clean_rows = []
                        clean_row_indices = []
                        for ri, row in enumerate(rows):
                            if all(cell is None for cell in row[1:]) and row[0] and len(row[0]) > 100:
                                continue
                            clean_rows.append(row)
                            clean_row_indices.append(ri)
                        if not clean_rows:
                            continue

                        # 用实际有效行的 cell 坐标计算精确的表格 y 范围
                        # tab.cells 是 [(x0,y0,x1,y1), ...] 每个 cell 的 rect
                        cells = tab.cells
                        cols = tab.col_count
                        if cells and clean_row_indices:
                            first_row_idx = clean_row_indices[0]
                            last_row_idx = clean_row_indices[-1]
                            # 每行有 cols 个 cell
                            first_cell = cells[first_row_idx * cols]
                            last_cell = cells[last_row_idx * cols + cols - 1]
                            actual_rect = (tab.bbox[0], first_cell[1], tab.bbox[2], last_cell[3])
                        else:
                            actual_rect = tab.bbox
                        rect_list.append(actual_rect)

                        # 转为 markdown 表格
                        ncols = len(clean_rows[0])
                        md_lines = []
                        header = clean_rows[0]
                        md_lines.append("| " + " | ".join((c or "").replace("\n", " ") for c in header) + " |")
                        md_lines.append("| " + " | ".join("---" for _ in range(ncols)) + " |")
                        for row in clean_rows[1:]:
                            md_lines.append("| " + " | ".join((c or "").replace("\n", " ") for c in row) + " |")
                        tbl_list.append((actual_rect[1], "\n".join(md_lines)))
                    if tbl_list:
                        page_tables[page_num] = tbl_list
                    if rect_list:
                        page_table_rects[page_num] = rect_list
            except Exception:
                pass

        # 检测代码块区域（背景矩形 + 等宽字体）
        page_code_rects = {}  # {page_num: [(y0, y1), ...]}
        for page_num, page in enumerate(doc):
            drawings = page.get_drawings()
            code_rects = []
            for d in drawings:
                fill = d.get("fill")
                if not fill or fill == (1.0, 1.0, 1.0):
                    continue
                # 灰色背景（RGB 各通道 > 0.9）
                if all(c > 0.9 for c in fill):
                    rect = d["rect"]
                    w = rect[2] - rect[0]
                    h = rect[3] - rect[1]
                    if w > 100 and h > 15:
                        code_rects.append((rect[1], rect[3]))
            if code_rects:
                # 合并重叠的矩形
                code_rects.sort()
                merged = [code_rects[0]]
                for y0, y1 in code_rects[1:]:
                    if y0 <= merged[-1][1] + 2:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], y1))
                    else:
                        merged.append((y0, y1))
                page_code_rects[page_num] = merged

        # 提取图片并保存到临时目录（压缩到 5MB 以内）
        import tempfile
        from io import BytesIO
        from PIL import Image as PILImage
        img_dir = os.path.join(tempfile.gettempdir(), "youdao2notion_imgs")
        os.makedirs(img_dir, exist_ok=True)
        pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
        MAX_IMG_SIZE = 5 * 1024 * 1024  # 5MB

        def _save_compressed(pix, save_path):
            """将 PyMuPDF Pixmap 压缩保存为 JPEG，确保 < 5MB"""
            png_bytes = pix.tobytes("png")
            img = PILImage.open(BytesIO(png_bytes))
            if img.mode == "RGBA":
                img = img.convert("RGB")
            # 先尝试质量 85
            for quality in [85, 70, 50, 30, 15]:
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= MAX_IMG_SIZE:
                    with open(save_path, "wb") as f:
                        f.write(buf.getvalue())
                    return
            # 还超过就缩小尺寸
            while True:
                w, h = img.size
                img = img.resize((w // 2, h // 2), PILImage.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=50, optimize=True)
                if buf.tell() <= MAX_IMG_SIZE or img.size[0] < 100:
                    with open(save_path, "wb") as f:
                        f.write(buf.getvalue())
                    return

        # 第二遍：提取所有行并按页码+y坐标排序（修复PDF乱序问题）
        all_lines_raw = []
        img_counter = 0
        # 预先收集每页已处理的图片 xref，避免同页多个 image block 重复提取同一张图
        used_xrefs = set()
        for page_num, page in enumerate(doc):
            page_imgs = page.get_images(full=True)
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                # 图片 block
                if block.get("type") == 1:
                    bbox = block["bbox"]
                    img_y = bbox[1]
                    img_w = bbox[2] - bbox[0]
                    img_h = bbox[3] - bbox[1]
                    # 跳过太小的图片（可能是图标/装饰）
                    if img_w < 30 or img_h < 30:
                        continue
                    # 提取图片并保存
                    try:
                        for img_info in page_imgs:
                            xref = img_info[0]
                            if xref in used_xrefs:
                                continue
                            used_xrefs.add(xref)
                            pix = fitz.Pixmap(doc, xref)
                            if pix.alpha:
                                pix = fitz.Pixmap(fitz.csRGB, pix)
                            img_counter += 1
                            img_filename = f"{pdf_basename}_img{img_counter}.jpg"
                            img_path = os.path.join(img_dir, img_filename)
                            _save_compressed(pix, img_path)
                            all_lines_raw.append((page_num, img_y, f"![image](local://{img_path})", 0, False, 0))
                            break  # 一个 block 对应一张图
                    except Exception:
                        pass
                    continue

                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    line_text = ""
                    line_size = 0
                    line_bold = False
                    line_y = line["bbox"][1]
                    line_x = line["bbox"][0]
                    line_x1 = line["bbox"][2]

                    # 跳过表格区域内的文本（已由 find_tables 提取）
                    in_table = False
                    if page_num in page_table_rects:
                        for tx0, ty0, tx1, ty1 in page_table_rects[page_num]:
                            if ty0 - 2 <= line_y <= ty1 + 2:
                                in_table = True
                                break
                    if in_table:
                        continue

                    # 检测是否在代码块区域内
                    in_code = False
                    if page_num in page_code_rects:
                        for cy0, cy1 in page_code_rects[page_num]:
                            if cy0 - 2 <= line_y <= cy1 + 2:
                                in_code = True
                                break

                    for span in line["spans"]:
                        text = span["text"]
                        if text.strip():
                            line_size = max(line_size, span["size"])
                            if span["flags"] & 16:
                                line_bold = True
                        line_text += text
                    line_text = line_text.strip()
                    if line_text:
                        if in_code:
                            all_lines_raw.append((page_num, line_y, line_text, line_size, line_bold, line_x, True, line_x1))
                        else:
                            all_lines_raw.append((page_num, line_y, line_text, line_size, line_bold, line_x, False, line_x1))

        # 按页码和 y 坐标排序，确保视觉顺序正确
        all_lines_raw.sort(key=lambda x: (x[0], x[1]))

        # 预处理：把孤立的 "数字." 编号行合并到对应的内容行
        # 有道云PDF里，编号在左侧(x≈31)，内容在右侧(x≈47)，y坐标接近
        import re as _re
        numbered_indices = set()
        for idx, row in enumerate(all_lines_raw):
            pg, ly, lt = row[0], row[1], row[2]
            if _re.match(r'^\d+\.$', lt.strip()):
                best_j = None
                best_dy = 999
                for j, row2 in enumerate(all_lines_raw):
                    pg2, ly2, lt2 = row2[0], row2[1], row2[2]
                    if pg2 == pg and j != idx and not _re.match(r'^\d+\.$', lt2.strip()):
                        dy = abs(ly2 - ly)
                        if dy < 5 and dy < best_dy:
                            best_dy = dy
                            best_j = j
                if best_j is not None:
                    r = list(all_lines_raw[best_j])
                    r[2] = f"{lt.strip()} {r[2]}"
                    all_lines_raw[best_j] = tuple(r)
                    numbered_indices.add(idx)

        if numbered_indices:
            all_lines_raw = [r for i, r in enumerate(all_lines_raw) if i not in numbered_indices]

        # 插入表格占位行到 all_lines_raw
        for pg, tbl_list in page_tables.items():
            for tbl_y, tbl_md in tbl_list:
                all_lines_raw.append((pg, tbl_y, f"__TABLE__\n{tbl_md}\n__TABLE_END__", 0, False, 0, False))
        all_lines_raw.sort(key=lambda x: (x[0], x[1]))

        # 计算正常行距（用于判断段落间空行）
        line_deltas = []
        for idx in range(1, len(all_lines_raw)):
            if all_lines_raw[idx][0] == all_lines_raw[idx-1][0]:  # 同页
                d = all_lines_raw[idx][1] - all_lines_raw[idx-1][1]
                if 5 < d < 50:
                    line_deltas.append(d)
        if line_deltas:
            from collections import Counter as _Counter
            delta_counts = _Counter(round(d, 0) for d in line_deltas)
            normal_line_height = delta_counts.most_common(1)[0][0]
            # 超过正常行距 * 1.05 时视为段落分隔
            paragraph_gap = normal_line_height * 1.05
        else:
            paragraph_gap = 25

        lines = []
        prev_line = ""
        prev_y = -999
        prev_page = -1
        prev_x1 = 0  # 上一行的右边缘 x 坐标
        in_code_block = False
        code_block_buf = []  # 缓存代码块行，用于去除行号

        def _flush_code_block():
            """将缓存的代码块行写入 lines，自动去除独立行号"""
            if not code_block_buf:
                return
            # 检测独立行号行：只包含一个数字的行，如 "1"、"2"、"13"
            # 有道云PDF代码块的行号会被提取为独立行，穿插在内容行之间
            import re as _re2
            num_indices = []
            nums = []
            for i, cl in enumerate(code_block_buf):
                if _re2.match(r'^\d+$', cl.strip()):
                    num_indices.append(i)
                    nums.append(int(cl.strip()))
            # 如果这些数字构成从1开始的连续序列，说明是行号，去掉
            if nums and nums == list(range(1, max(nums) + 1)) and len(nums) >= 2:
                num_set = set(num_indices)
                for i, cl in enumerate(code_block_buf):
                    if i not in num_set:
                        lines.append(cl)
            else:
                lines.extend(code_block_buf)
            code_block_buf.clear()
        # 判断一行是否写满了页面宽度（自动换行），右边距 < 80 视为写满
        def _is_line_full(x1):
            return (page_width - x1) < 80

        for row in all_lines_raw:
            page_num, line_y, line_text = row[0], row[1], row[2]
            line_size = row[3] if len(row) > 3 else 0
            line_bold = row[4] if len(row) > 4 else False
            line_x = row[5] if len(row) > 5 else 0
            is_code = row[6] if len(row) > 6 else False
            line_x1 = row[7] if len(row) > 7 else 0

            # 表格占位：直接插入 markdown 表格
            if line_text.startswith("__TABLE__"):
                if in_code_block:
                    _flush_code_block()
                    lines.append("```")
                    in_code_block = False
                if prev_line:
                    lines.append("")
                tbl_content = line_text.replace("__TABLE__\n", "").replace("\n__TABLE_END__", "")
                lines.append(tbl_content)
                lines.append("")
                prev_line = tbl_content
                prev_y = line_y
                prev_page = page_num
                continue

            # 代码块处理
            if is_code and not in_code_block:
                if prev_line:
                    lines.append("")
                lines.append("```")
                in_code_block = True
            elif not is_code and in_code_block:
                _flush_code_block()
                lines.append("```")
                lines.append("")
                in_code_block = False

            if is_code:
                code_block_buf.append(line_text)
                prev_line = line_text
                prev_y = line_y
                prev_page = page_num
                continue

            # 页面切换或大间距时加空行
            if page_num != prev_page or (line_y - prev_y > paragraph_gap and prev_line):
                if prev_line:
                    lines.append("")
            prev_y = line_y
            prev_page = page_num

            # 检查该行是否在引用区域内
            in_quote = False
            if page_num in page_quotes:
                for qy0, qy1 in page_quotes[page_num]:
                    if qy0 - 2 <= line_y <= qy1 + 2:
                        in_quote = True
                        break

            # 检查该行是否有对应的 checkbox
            checkbox_status = None
            if page_num in page_checkboxes:
                for cb_y, cb_x, checked in page_checkboxes[page_num]:
                    if abs(cb_y - line_y) < 10 and cb_x < line_x:
                        checkbox_status = checked
                        break

            # 检查该行是否有对应的无序列表圆点
            bullet_level = 0
            if checkbox_status is None and page_num in page_bullets:
                for bt_y, bt_x, bt_lvl in page_bullets[page_num]:
                    if abs(bt_y - line_y) < 10 and bt_x < line_x:
                        bullet_level = bt_lvl
                        break

            rounded_size = round(line_size, 1)
            level = size_to_level.get(rounded_size, 0)

            if in_quote:
                lines.append(f"> {line_text}")
            elif checkbox_status is not None:
                mark = "x" if checkbox_status else " "
                lines.append(f"- [{mark}] {line_text}")
            elif bullet_level > 0:
                indent = "  " * (bullet_level - 1)
                lines.append(f"{indent}- {line_text}")
            elif level > 0 and line_bold:
                if prev_line:
                    lines.append("")
                lines.append(f"{'#' * level} {line_text}")
                lines.append("")
            elif line_bold and rounded_size > body_size:
                best_level = 3
                for hs in heading_sizes:
                    if rounded_size >= hs:
                        best_level = size_to_level[hs]
                        break
                if prev_line:
                    lines.append("")
                lines.append(f"{'#' * best_level} {line_text}")
                lines.append("")
            else:
                # 如果上一行写满了页面宽度（自动换行），合并到上一行
                if lines and prev_line and _is_line_full(prev_x1) and page_num == prev_page and (line_y - prev_y) <= paragraph_gap:
                    lines[-1] = lines[-1] + line_text
                else:
                    lines.append(line_text)

            prev_line = line_text
            prev_x1 = line_x1

        # 关闭未关闭的代码块
        if in_code_block:
            _flush_code_block()
            lines.append("```")

        doc.close()
        # 清理多余空行
        result = "\n".join(lines)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()
    except Exception as e:
        log(f"  [警告] PDF 提取失败: {e}")
        return ""


def html_file_to_markdown(html_path: str) -> str:
    """从 HTML 文件提取内容转为 Markdown"""
    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return html_to_markdown(content)
    except Exception as e:
        log(f"  [警告] HTML 转换失败: {e}")
        return ""


def read_file_content(filepath: str) -> str:
    """根据文件类型读取内容，统一转为 Markdown 文本"""
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".md":
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    elif ext == ".pdf":
        return pdf_to_markdown(filepath)
    elif ext in (".html", ".htm"):
        return html_file_to_markdown(filepath)
    elif ext == ".txt":
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    elif ext == ".note":
        # .note 文件可能是 HTML 格式
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if content.strip().startswith("<"):
            return html_to_markdown(content)
        return content
    else:
        return ""


class ExportImporter:
    def __init__(self):
        self.client = Client(auth=config.NOTION_TOKEN)
        self.parent_page_id = config.NOTION_PARENT_PAGE_ID
        self.folder_page_map = {}
        self.pushed_files = set()
        self._load_state()

    def _state_file(self):
        return os.path.join(config.OUTPUT_DIR, "import_progress.json")

    def _load_state(self):
        if os.path.exists(self._state_file()):
            with open(self._state_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
                self.folder_page_map = data.get("folder_map", {})
                self.pushed_files = set(data.get("pushed_files", []))

    def _save_state(self):
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        with open(self._state_file(), "w", encoding="utf-8") as f:
            json.dump({
                "folder_map": self.folder_page_map,
                "pushed_files": list(self.pushed_files),
            }, f, ensure_ascii=False, indent=2)

    def _upload_local_images(self, blocks: list[dict]) -> list[dict]:
        """扫描 blocks，将 __LOCAL__: 占位图片上传到 Notion file upload API"""
        import requests as req
        NOTION_VERSION = "2025-09-03"
        headers_json = {
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        headers_upload = {
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
        }

        for block in blocks:
            if block.get("type") != "image":
                continue
            img_data = block["image"]
            if img_data.get("type") != "file_upload":
                continue
            fid = img_data.get("file_upload", {}).get("id", "")
            if not fid.startswith("__LOCAL__:"):
                continue

            local_path = fid[len("__LOCAL__:"):]
            if not os.path.exists(local_path):
                # 转为文字占位
                block["type"] = "paragraph"
                block["paragraph"] = {"rich_text": [{"type": "text", "text": {"content": "[图片文件缺失]"}}]}
                del block["image"]
                continue

            filename = os.path.basename(local_path)
            content_type = "image/jpeg" if local_path.endswith(".jpg") else "image/png"

            try:
                # Step 1: Create file upload
                r1 = req.post("https://api.notion.com/v1/file_uploads", headers=headers_json,
                              json={"filename": filename, "content_type": content_type})
                if r1.status_code != 200:
                    raise Exception(f"Create upload failed: {r1.status_code}")
                upload_id = r1.json()["id"]

                # Step 2: Send file
                with open(local_path, "rb") as f:
                    r2 = req.post(f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
                                  headers=headers_upload,
                                  files={"file": (filename, f, content_type)})
                if r2.status_code != 200:
                    raise Exception(f"Upload send failed: {r2.status_code}")

                # Step 3: Update block to use uploaded file
                img_data["file_upload"]["id"] = upload_id
                time.sleep(config.NOTION_REQUEST_DELAY)
            except Exception as e:
                log(f"  [警告] 图片上传失败 {filename}: {e}")
                block["type"] = "paragraph"
                block["paragraph"] = {"rich_text": [{"type": "text", "text": {"content": f"[图片上传失败: {filename}]"}}]}
                if "image" in block:
                    del block["image"]

        return blocks

    def _notion_call(self, func, *args, **kwargs):
        for attempt in range(3):
            try:
                result = func(*args, **kwargs)
                time.sleep(config.NOTION_REQUEST_DELAY)
                return result
            except Exception as e:
                if "rate_limited" in str(e).lower():
                    time.sleep(2 ** (attempt + 1))
                else:
                    if attempt == 2:
                        log(f"  [错误] Notion API: {e}")
                        return None
                    time.sleep(1)
        return None

    def ensure_folder(self, rel_path: str) -> str:
        if not rel_path:
            return self.parent_page_id
        if rel_path in self.folder_page_map:
            return self.folder_page_map[rel_path]

        parts = rel_path.replace("\\", "/").split("/")
        current_parent = self.parent_page_id
        current_path = ""

        for part in parts:
            if not part:
                continue
            current_path = f"{current_path}/{part}" if current_path else part
            if current_path in self.folder_page_map:
                current_parent = self.folder_page_map[current_path]
                continue

            result = self._notion_call(
                self.client.pages.create,
                parent={"page_id": current_parent},
                properties={"title": [{"text": {"content": part}}]},
            )
            if result is None:
                return current_parent

            page_id = result["id"]
            self.folder_page_map[current_path] = page_id
            current_parent = page_id
            log(f"  创建文件夹: {current_path}")

        self._save_state()
        return current_parent

    def push_file(self, filepath: str, base_dir: str) -> bool:
        rel_path = os.path.relpath(filepath, base_dir).replace("\\", "/")

        if rel_path in self.pushed_files:
            return True

        content = read_file_content(filepath)
        if not content.strip():
            log(f"  [跳过] 内容为空")
            return True  # 跳过空文件，标记为已处理

        # 文件夹和标题
        parts = rel_path.rsplit("/", 1)
        if len(parts) == 2:
            folder_path, filename = parts
        else:
            folder_path, filename = "", parts[0]

        # 去掉文件扩展名作为标题
        title = os.path.splitext(filename)[0]

        parent_id = self.ensure_folder(folder_path)

        result = self._notion_call(
            self.client.pages.create,
            parent={"page_id": parent_id},
            properties={"title": [{"text": {"content": title}}]},
        )
        if result is None:
            return False

        page_id = result["id"]

        blocks = markdown_to_notion_blocks(content)
        # 上传本地图片到 Notion file upload API
        blocks = self._upload_local_images(blocks)
        for i in range(0, len(blocks), config.NOTION_BATCH_SIZE):
            batch = blocks[i:i + config.NOTION_BATCH_SIZE]
            self._notion_call(
                self.client.blocks.children.append,
                block_id=page_id,
                children=batch,
            )

        self.pushed_files.add(rel_path)
        self._save_state()
        return True

    def import_from_dir(self, export_dir: str):
        """从导出目录导入所有文件到 Notion"""
        log(f"===== 导入有道云导出文件到 Notion =====\n")
        log(f"导出目录: {export_dir}")
        log(f"已导入: {len(self.pushed_files)} 篇\n")

        # 收集所有支持的文件
        supported_ext = {".md", ".pdf", ".html", ".htm", ".txt", ".note"}
        all_files = []
        for root, dirs, files in os.walk(export_dir):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in supported_ext:
                    all_files.append(os.path.join(root, f))

        all_files.sort()
        new_files = [
            f for f in all_files
            if os.path.relpath(f, export_dir).replace("\\", "/") not in self.pushed_files
        ]

        # 统计文件类型
        ext_count = {}
        for f in all_files:
            ext = os.path.splitext(f)[1].lower()
            ext_count[ext] = ext_count.get(ext, 0) + 1

        log(f"共发现 {len(all_files)} 个文件:")
        for ext, count in sorted(ext_count.items()):
            log(f"  {ext}: {count} 个")
        log(f"待导入: {len(new_files)} 个\n")

        for i, filepath in enumerate(new_files, 1):
            rel = os.path.relpath(filepath, export_dir).replace("\\", "/")
            ext = os.path.splitext(filepath)[1].lower()
            log(f"[{i}/{len(new_files)}] {rel}")
            ok = self.push_file(filepath, export_dir)
            if not ok:
                log(f"  [失败]")

        log(f"\n导入完成！共导入 {len(self.pushed_files)} 篇到 Notion")


def main():
    if len(sys.argv) < 2:
        log("用法: python import_export.py <导出文件夹路径>")
        log("示例: python import_export.py D:\\youdao_export")
        return

    export_dir = sys.argv[1]
    if not os.path.isdir(export_dir):
        log(f"[错误] 目录不存在: {export_dir}")
        return

    importer = ExportImporter()
    importer.import_from_dir(export_dir)


if __name__ == "__main__":
    main()
