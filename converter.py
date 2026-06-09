"""
格式转换模块
- HTML（有道云富文本）→ Markdown
- Markdown → Notion API Blocks
"""

import re
from markdownify import markdownify as md
from bs4 import BeautifulSoup


def html_to_markdown(html_content: str) -> str:
    """将有道云笔记的 HTML 富文本转为 Markdown"""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    # 移除有道云笔记特有的空标签和样式垃圾
    for tag in soup.find_all(style=True):
        del tag["style"]
    for tag in soup.find_all("colgroup"):
        tag.decompose()
    markdown_text = md(str(soup), heading_style="ATX", bullets="-", code_language="python")
    # 清理多余空行
    markdown_text = re.sub(r"\n{3,}", "\n\n", markdown_text)
    return markdown_text.strip()


def extract_image_urls(content: str) -> list[str]:
    """从 HTML 或 Markdown 内容中提取图片 URL"""
    urls = []
    # HTML img 标签
    urls.extend(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', content))
    # Markdown 图片语法
    urls.extend(re.findall(r'!\[[^\]]*\]\(([^)]+)\)', content))
    return list(set(urls))


def _split_text(text: str, max_len: int = 2000) -> list[str]:
    """将长文本按 max_len 拆分，尽量在换行处断开"""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # 尝试在换行处断开
        idx = text.rfind("\n", 0, max_len)
        if idx == -1:
            idx = max_len
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def _rich_text(text: str) -> list[dict]:
    """构建 Notion rich_text 数组，自动处理 2000 字符限制"""
    parts = _split_text(text, 2000)
    return [{"type": "text", "text": {"content": p}} for p in parts]


def _parse_inline(text: str) -> list[dict]:
    """解析行内 Markdown 格式（粗体、斜体、行内代码、链接）为 Notion rich_text"""
    result = []
    # 简化处理：用正则分段解析常见行内格式
    pattern = re.compile(
        r'(`[^`]+`)'                       # 行内代码
        r'|(\*\*\*.+?\*\*\*)'             # 粗斜体
        r'|(\*\*.+?\*\*)'                 # 粗体
        r'|(\*.+?\*)'                      # 斜体
        r'|(\[([^\]]+)\]\(([^)]+)\))'     # 链接
    )
    pos = 0
    for m in pattern.finditer(text):
        # 前面的普通文本
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                for chunk in _split_text(plain, 2000):
                    result.append({"type": "text", "text": {"content": chunk}})
        if m.group(1):  # 行内代码
            code = m.group(1)[1:-1]
            for chunk in _split_text(code, 2000):
                result.append({"type": "text", "text": {"content": chunk}, "annotations": {"code": True}})
        elif m.group(2):  # 粗斜体
            t = m.group(2)[3:-3]
            for chunk in _split_text(t, 2000):
                result.append({"type": "text", "text": {"content": chunk}, "annotations": {"bold": True, "italic": True}})
        elif m.group(3):  # 粗体
            t = m.group(3)[2:-2]
            for chunk in _split_text(t, 2000):
                result.append({"type": "text", "text": {"content": chunk}, "annotations": {"bold": True}})
        elif m.group(4):  # 斜体
            t = m.group(4)[1:-1]
            for chunk in _split_text(t, 2000):
                result.append({"type": "text", "text": {"content": chunk}, "annotations": {"italic": True}})
        elif m.group(5):  # 链接
            link_text = m.group(6)
            link_url = m.group(7)
            for chunk in _split_text(link_text, 2000):
                result.append({"type": "text", "text": {"content": chunk, "link": {"url": link_url}}})
        pos = m.end()
    # 剩余普通文本
    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            for chunk in _split_text(remaining, 2000):
                result.append({"type": "text", "text": {"content": chunk}})
    if not result:
        return [{"type": "text", "text": {"content": text[:2000]}}]
    return result


def markdown_to_notion_blocks(md_text: str) -> list[dict]:
    """将 Markdown 文本转为 Notion API Block 数组"""
    blocks = []
    lines = md_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # 空行跳过
        if not line.strip():
            i += 1
            continue

        # 代码块
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结束的 ```
            code_content = "\n".join(code_lines)
            # 代码块也有 2000 字符限制
            for chunk in _split_text(code_content, 2000):
                blocks.append({
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": chunk}}],
                        "language": lang if lang else "plain text"
                    }
                })
            continue

        # Markdown 表格
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r'^\|[\s\-:|]+\|$', lines[i + 1].strip()):
            # 收集所有表格行
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                row_line = lines[i].strip()
                # 跳过分隔行 |---|---|
                if re.match(r'^\|[\s\-:|]+\|$', row_line):
                    i += 1
                    continue
                cells = [c.strip() for c in row_line.strip("|").split("|")]
                table_rows.append(cells)
                i += 1
            if table_rows:
                col_count = max(len(r) for r in table_rows)
                # Notion table block
                table_children = []
                for row in table_rows:
                    # 补齐列数
                    while len(row) < col_count:
                        row.append("")
                    table_children.append({
                        "object": "block",
                        "type": "table_row",
                        "table_row": {
                            "cells": [[{"type": "text", "text": {"content": cell[:2000]}}] for cell in row]
                        }
                    })
                blocks.append({
                    "object": "block",
                    "type": "table",
                    "table": {
                        "table_width": col_count,
                        "has_column_header": True,
                        "has_row_header": False,
                        "children": table_children,
                    }
                })
            continue

        # 标题
        heading_match = re.match(r'^(#{1,3})\s+(.+)$', line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            heading_type = f"heading_{level}"
            blocks.append({
                "object": "block",
                "type": heading_type,
                heading_type: {"rich_text": _parse_inline(text)}
            })
            i += 1
            continue

        # 图片
        img_match = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)$', line.strip())
        if img_match:
            url = img_match.group(2)
            alt = img_match.group(1) or "图片"
            if url.startswith("http"):
                blocks.append({
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "external",
                        "external": {"url": url}
                    }
                })
            elif url.startswith("local://"):
                # 本地图片，需要通过 Notion file upload API 上传
                local_path = url[len("local://"):]
                blocks.append({
                    "object": "block",
                    "type": "image",
                    "image": {
                        "type": "file_upload",
                        "file_upload": {"id": "__LOCAL__:" + local_path}
                    }
                })
            else:
                # 其他本地路径，转为文字提示
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"[图片: {alt}]"}}]}
                })
            i += 1
            continue

        # 辅助：判断一行是否是"结构行"（不应被合并为前一行的续行）
        def _is_structural(ln):
            ln = ln.strip() if ln else ""
            if not ln:
                return True
            if re.match(r'^#{1,3}\s+', ln):
                return True
            if re.match(r'^[\-\*\+]\s+\[([ xX])\]\s+', ln):
                return True
            if re.match(r'^ *[\-\*\+]\s+', ln):
                return True
            if re.match(r'^\d+\.\s+', ln):
                return True
            if ln.startswith("```"):
                return True
            if re.match(r'^>\s*', ln):
                return True
            if re.match(r'^!\[', ln):
                return True
            if ln.startswith("|"):
                return True
            # 以特定 emoji/符号开头的行视为独立行（❌✅✓☐☑等）
            if ln and ln[0] in "❌✅✓✗☐☑☒⚠️⭐🔴🟢🟡💡📌🎯":
                return True
            return False

        # 辅助：从 i+1 开始收集续行（非结构行），合并为完整文本
        # parent_indent: 当前行的缩进级别，续行不能比它缩进更少
        def _collect_continuation(start_i, first_text, parent_indent=0):
            text = first_text
            j = start_i
            while j < len(lines):
                nxt = lines[j]
                if _is_structural(nxt):
                    break
                # 续行的缩进不能少于父行的缩进（否则是独立段落）
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if parent_indent > 0 and nxt_indent < parent_indent:
                    break
                text += nxt.strip()
                j += 1
            return text, j

        # Todo / Checkbox（- [x] 或 - [ ]）
        todo_match = re.match(r'^[\-\*\+]\s+\[([ xX])\]\s+(.+)$', line)
        if todo_match:
            checked = todo_match.group(1).lower() == "x"
            text, i = _collect_continuation(i + 1, todo_match.group(2))
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": _parse_inline(text),
                    "checked": checked,
                }
            })
            continue

        # 无序列表（支持最多 3 级嵌套：缩进 2 空格为一级）
        list_match = re.match(r'^( *)[\-\*\+]\s+(.+)$', line)
        if list_match:
            indent = len(list_match.group(1))
            nest_level = min(indent // 2, 2)  # 0, 1, 2
            text, i = _collect_continuation(i + 1, list_match.group(2), parent_indent=indent)
            item_block = {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _parse_inline(text)}
            }
            if nest_level == 0:
                blocks.append(item_block)
            elif nest_level == 1 and blocks and blocks[-1].get("type") == "bulleted_list_item":
                # 二级：挂到上一个一级列表项的 children
                blocks[-1]["bulleted_list_item"].setdefault("children", []).append(item_block)
            elif nest_level >= 2 and blocks and blocks[-1].get("type") == "bulleted_list_item":
                # 三级：挂到上一个一级列表项的最后一个二级子项的 children
                parent_children = blocks[-1]["bulleted_list_item"].get("children", [])
                if parent_children and parent_children[-1].get("type") == "bulleted_list_item":
                    parent_children[-1]["bulleted_list_item"].setdefault("children", []).append(item_block)
                else:
                    # 没有二级父项，降级为二级
                    blocks[-1]["bulleted_list_item"].setdefault("children", []).append(item_block)
            else:
                # 找不到父项，作为一级
                blocks.append(item_block)
            continue

        # 有序列表
        olist_match = re.match(r'^\d+\.\s+(.+)$', line)
        if olist_match:
            text, i = _collect_continuation(i + 1, olist_match.group(1))
            blocks.append({
                "object": "block",
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _parse_inline(text)}
            })
            continue

        # 引用
        quote_match = re.match(r'^>\s*(.*)', line)
        if quote_match:
            text = quote_match.group(1)
            blocks.append({
                "object": "block",
                "type": "quote",
                "quote": {"rich_text": _parse_inline(text)}
            })
            i += 1
            continue

        # 分割线
        if re.match(r'^---+$', line.strip()):
            blocks.append({
                "object": "block",
                "type": "divider",
                "divider": {}
            })
            i += 1
            continue

        # 普通段落
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _parse_inline(line)}
        })
        i += 1

    return blocks
