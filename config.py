import os

# =============================================================
#  有道云笔记 → Notion 迁移工具 — 配置文件
#  请根据注释填写你的信息后再运行迁移脚本
# =============================================================

# ---------- Notion 配置 ----------
# 获取方式：
#   1. 打开 https://www.notion.so/my-integrations
#   2. 创建一个新的 Integration，记下 Internal Integration Token
#   3. 在 Notion 中创建一个空页面，点击右上角 ··· → Connections → 添加你的 Integration
#   4. 复制该页面的链接，其中的 32 位十六进制字符串就是页面 ID
#      例如: https://www.notion.so/My-Page-1234567890abcdef1234567890abcdef
#      页面 ID 就是: 1234567890abcdef1234567890abcdef
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "")

# ---------- 输出目录 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ---------- 运行参数 ----------
# Notion API 请求间隔（秒），官方限制约 3 req/s
NOTION_REQUEST_DELAY = 0.35
# 失败重试次数
MAX_RETRIES = 3
# 每批追加到 Notion 页面的 block 数量（API 限制最多 100）
NOTION_BATCH_SIZE = 100
