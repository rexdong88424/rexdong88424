#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
libdb_crawler.py
================
一键爬取 https://libdb.csuft.edu.cn/ 全站 HTML + 菜单路由 JSON
输出目录：E:\\图书馆系统每日监测\\<YYYY-MM-DD_HH-MM-SS>\\
          ├── html\          ← 各页面 HTML（文件名由 URL 编码而来）
          ├── json\          ← 捕获到的 JSON 接口响应
          └── manifest.json ← 汇总索引（url -> 文件名映射）

依赖安装（仅需一次）：
    pip install requests beautifulsoup4 playwright
    playwright install chromium

使用方式：
    python libdb_crawler.py

可选参数（直接在下方 CONFIG 区调整）：
    BASE_URL      起始 URL
    OUTPUT_ROOT   本地保存根目录
    MAX_PAGES     最多爬取页面数（0 = 不限制）
    REQUEST_DELAY 每次请求间隔秒数（礼貌爬取）
    TIMEOUT       单页超时秒数
    SAME_DOMAIN   True = 只爬同域链接
"""

# ─────────────────────────── CONFIG ────────────────────────────
BASE_URL      = "https://libdb.csuft.edu.cn/"
OUTPUT_ROOT   = r"E:\图书馆系统每日监测"   # Windows 路径；Linux/Mac 改成 /xxx/xxx
MAX_PAGES     = 0        # 0 表示不限制
REQUEST_DELAY = 0.5      # 秒
TIMEOUT       = 30       # 秒（Playwright 页面超时）
SAME_DOMAIN   = True     # 只爬同域子页面
# ───────────────────────────────────────────────────────────────

import json
import os
import re
import sys
import time
import urllib.parse
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[ERROR] 缺少依赖，请先运行：pip install requests beautifulsoup4")
    sys.exit(1)


# ──────────────────────────── helpers ──────────────────────────

def sanitize_filename(url: str, suffix: str) -> str:
    """把 URL 转成合法文件名（保留可读性，截断过长部分）."""
    name = re.sub(r"[^\w\-._]", "_", url)
    return name[:180] + suffix


def get_domain(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return p.scheme + "://" + p.netloc


def normalize_url(url: str, base: str) -> str | None:
    """补全相对路径，过滤非 http(s) 链接."""
    url = url.strip()
    if not url or url.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    full = urllib.parse.urljoin(base, url)
    parsed = urllib.parse.urlparse(full)
    if parsed.scheme not in ("http", "https"):
        return None
    # 去掉 fragment
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def extract_links(html: str, current_url: str, base_domain: str) -> list[str]:
    """从 HTML 中提取所有内部链接."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("a", href=True):
        norm = normalize_url(tag["href"], current_url)
        if norm and (not SAME_DOMAIN or norm.startswith(base_domain)):
            links.append(norm)
    return links


# ──────────────────────── 方案 A：纯 requests ──────────────────

def crawl_with_requests(out_dir: Path, json_dir: Path) -> dict:
    """
    用 requests + BeautifulSoup 静态爬取。
    不执行 JS，适合服务端渲染页面。
    返回 manifest dict。
    """
    base_domain = get_domain(BASE_URL)
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    })

    visited: set[str] = set()
    queue: deque[str] = deque([BASE_URL])
    manifest: dict[str, str] = {}

    print(f"[requests] 开始爬取，起始 URL: {BASE_URL}")
    print(f"[requests] HTML 保存目录: {out_dir}")

    while queue:
        if MAX_PAGES and len(visited) >= MAX_PAGES:
            print(f"[requests] 已达 MAX_PAGES={MAX_PAGES} 限制，停止。")
            break

        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception as e:
            print(f"  [WARN] 请求失败 {url}: {e}")
            continue

        ct = resp.headers.get("Content-Type", "")
        fname = sanitize_filename(url, ".html")

        if "json" in ct:
            # 把意外遇到的 JSON 接口也保存下来
            jfname = sanitize_filename(url, ".json")
            jpath = json_dir / jfname
            jpath.write_text(resp.text, encoding="utf-8")
            print(f"  [JSON] {url} -> json/{jfname}")
            manifest[url] = f"json/{jfname}"
        elif "html" in ct or not ct:
            fpath = out_dir / fname
            fpath.write_text(resp.text, encoding="utf-8")
            print(f"  [HTML] {url} -> html/{fname}")
            manifest[url] = f"html/{fname}"

            new_links = extract_links(resp.text, url, base_domain)
            for lnk in new_links:
                if lnk not in visited:
                    queue.append(lnk)
        else:
            print(f"  [SKIP] {url} ({ct})")

        time.sleep(REQUEST_DELAY)

    return manifest


# ──────────────────────── 方案 B：Playwright ──────────────────

def crawl_with_playwright(out_dir: Path, json_dir: Path) -> dict:
    """
    用 Playwright (Chromium) 爬取，自动执行 JS，拦截 XHR/fetch 接口。
    返回 manifest dict。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERROR] 未安装 playwright，请运行：pip install playwright && playwright install chromium")
        sys.exit(1)

    base_domain = get_domain(BASE_URL)
    visited: set[str] = set()
    queue: deque[str] = deque([BASE_URL])
    manifest: dict[str, str] = {}
    json_counter = [0]

    print(f"[playwright] 开始爬取，起始 URL: {BASE_URL}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        # 拦截所有响应，把 JSON 接口存下来
        def on_response(response):
            ct = response.headers.get("content-type", "")
            url = response.url
            if "json" in ct:
                try:
                    body = response.body()
                    json_counter[0] += 1
                    jfname = sanitize_filename(url, f"_{json_counter[0]}.json")
                    jpath = json_dir / jfname
                    jpath.write_bytes(body)
                    print(f"  [JSON] {url} -> json/{jfname}")
                    manifest[url] = f"json/{jfname}"
                except Exception:
                    pass

        page = context.new_page()
        page.on("response", on_response)

        while queue:
            if MAX_PAGES and len(visited) >= MAX_PAGES:
                print(f"[playwright] 已达 MAX_PAGES={MAX_PAGES} 限制，停止。")
                break

            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            try:
                page.goto(url, timeout=TIMEOUT * 1000, wait_until="networkidle")
            except Exception as e:
                print(f"  [WARN] 页面加载失败 {url}: {e}")
                continue

            html = page.content()
            fname = sanitize_filename(url, ".html")
            fpath = out_dir / fname
            fpath.write_text(html, encoding="utf-8")
            print(f"  [HTML] {url} -> html/{fname}")
            manifest[url] = f"html/{fname}"

            new_links = extract_links(html, url, base_domain)
            for lnk in new_links:
                if lnk not in visited:
                    queue.append(lnk)

            time.sleep(REQUEST_DELAY)

        browser.close()

    return manifest


# ──────────────────────────── main ────────────────────────────

def main():
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir   = Path(OUTPUT_ROOT) / timestamp
    html_dir  = run_dir / "html"
    json_dir  = run_dir / "json"
    html_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(f"  图书馆系统爬取脚本")
    print(f"  目标: {BASE_URL}")
    print(f"  输出: {run_dir}")
    print("=" * 60)

    # 优先使用 Playwright（JS 渲染 + JSON 拦截），失败则降级为 requests
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        print("[*] 检测到 Playwright，使用 JS 渲染模式（推荐）")
        manifest = crawl_with_playwright(html_dir, json_dir)
    except ImportError:
        print("[*] 未检测到 Playwright，使用静态 requests 模式")
        print("    （若需 JS 渲染 + 接口拦截，请运行：pip install playwright && playwright install chromium）")
        manifest = crawl_with_requests(html_dir, json_dir)

    # 保存汇总索引
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print()
    print("=" * 60)
    print(f"  爬取完成！共处理 {len(manifest)} 个资源")
    print(f"  manifest: {manifest_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
