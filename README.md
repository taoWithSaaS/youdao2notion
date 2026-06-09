# youdao2notion

有道云笔记批量迁移到 Notion，保留目录结构。

## 支持的格式

- PDF（有道云桌面客户端导出的 `.note.pdf`）
  - 标题层级（根据字体大小自动识别）
  - 粗体/斜体/行内代码/链接
  - 代码块（灰底等宽字体区域）
  - 表格
  - 引用块（左侧彩色竖线）
  - 待办事项 / Checkbox（矢量绘图检测）
  - 图片（自动提取、压缩到 5MB 以内并上传到 Notion）
  - 有序/无序列表
- Markdown（`.md`）
- HTML（`.html` / `.htm`）
- 纯文本（`.txt`）
- 有道云笔记格式（`.note`）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 从有道云桌面客户端导出笔记

1. 打开有道云笔记桌面客户端
2. 选中要导出的笔记或文件夹 → 右键 → 导出

### 3. 获取 Notion Token

1. 打开 https://www.notion.so/my-integrations
2. 点击「New integration」创建一个集成
3. 记下 Internal Integration Token（以 `ntn_` 开头）

### 4. 准备 Notion 目标页面

1. 在 Notion 中创建一个空页面（或选择已有页面）作为迁移目标
2. 点击页面右上角 `···` → `Connections` → 添加你刚创建的 Integration
3. 复制该页面的链接，提取其中的页面 ID
   - 例如：`https://www.notion.so/My-Page-1234567890abcdef1234567890abcdef`
   - 页面 ID 就是最后的 32 位十六进制字符串：`1234567890abcdef1234567890abcdef`

### 5. 配置

编辑 `config.py`，填入你的配置：

```python
NOTION_TOKEN = "ntn_xxxxx"           # 你的 Notion Integration Token
NOTION_PARENT_PAGE_ID = "xxxxx"      # 目标页面 ID
```

也可以通过环境变量设置：
```bash
export NOTION_TOKEN="ntn_xxxxx"
export NOTION_PARENT_PAGE_ID="xxxxx"
```

### 6. 运行

```bash
python import_export.py "导出文件夹路径"
```

## 功能特点

- **断点续传**：中断后重新运行，自动跳过已导入的笔记
- **目录结构保留**：有道云的文件夹结构原样还原到 Notion
- **图片自动上传**：PDF 中的图片会提取并通过 Notion File Upload API 上传
- **大文件自动拆分**：超过 Notion API 限制（2000 字符/block，100 blocks/请求）时自动处理
- **速率限制**：内置请求限速，避免触发 Notion API 频率限制

## 项目结构

```
youdao2notion/
├── config.py          # 配置文件（Notion Token、目标页面 ID）
├── import_export.py   # 主脚本：从导出文件夹导入到 Notion
├── converter.py       # Markdown ↔ Notion Blocks 格式转换
└── requirements.txt   # Python 依赖
```

## 常见问题

**Q：导入中断了怎么办？**
重新运行同样的命令即可，工具会自动跳过已成功导入的笔记。

**Q：能导入到 Notion 根目录吗？**
不能。Notion API 要求所有页面必须有父页面，需要指定一个目标页面作为迁移根目录。

**Q：图片显示不了？**
PDF 中的图片会自动提取并上传到 Notion。Notion 免费版单文件限制 5MB，工具会自动压缩超大图片。

## License

MIT
