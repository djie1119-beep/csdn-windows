"""
CSDN 文章解锁与文件下载工具
支持解锁 CSDN 博客文章内容和下载 CSDN 资源文件
"""

import re
import sys
import os
import argparse
import json
import urllib.parse
from html.parser import HTMLParser

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests beautifulsoup4")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("请先安装依赖: pip install requests beautifulsoup4")
    sys.exit(1)


ARTICLE_PATTERN = re.compile(
    r"https?://blog\.csdn\.net/[^/]+/article/details/(\d+)"
)
DOWNLOAD_PATTERN = re.compile(
    r"https?://download\.csdn\.net/download/[^/]+/(\d+)"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.csdn.net/",
}

# 保存文章时文件名允许的最大字符数（避免文件系统限制）
MAX_FILENAME_LENGTH = 80
# 下载文件时文件名允许的最大字符数（稍短，为资源 ID 前缀留出空间）
MAX_DOWNLOAD_FILENAME_LENGTH = 60
# 在终端打印文章内容时的最大字符数
MAX_CONSOLE_CONTENT_LENGTH = 2000


class HTMLStripper(HTMLParser):
    """将 HTML 转换为纯文本。"""

    def __init__(self):
        super().__init__()
        self.reset()
        self._fed = []

    def handle_data(self, d):
        self._fed.append(d)

    def get_text(self):
        return "".join(self._fed)


def strip_html(html_str):
    """移除 HTML 标签，返回纯文本。"""
    s = HTMLStripper()
    s.feed(html_str)
    return s.get_text()


class CSDNUnlocker:
    """解锁 CSDN 博客文章内容。"""

    MOBILE_API = "https://m.blog.csdn.net/article/details/{article_id}"
    PC_URL = "https://blog.csdn.net/{username}/article/details/{article_id}"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _fetch(self, url: str) -> requests.Response:
        """发送 GET 请求，返回响应对象。"""
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp

    def parse_article_id(self, url: str):
        """从 CSDN 文章 URL 中提取文章 ID 和用户名。

        Returns:
            tuple: (username, article_id) 或在解析失败时引发 ValueError。
        """
        m = ARTICLE_PATTERN.match(url)
        if not m:
            raise ValueError(f"无效的 CSDN 文章链接: {url}")
        article_id = m.group(1)
        # 提取用户名
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        username = parts[0] if parts else "unknown"
        return username, article_id

    def unlock_article(self, url: str) -> dict:
        """解锁并获取 CSDN 文章完整内容。

        Returns:
            dict: 包含 title, content, author, article_id 等字段。
        """
        username, article_id = self.parse_article_id(url)

        # 优先尝试 PC 页面（不需要登录即可读取部分内容）
        pc_url = self.PC_URL.format(username=username, article_id=article_id)
        try:
            resp = self._fetch(pc_url)
            return self._parse_pc_page(resp.text, article_id)
        except Exception:
            pass

        # 回退：使用移动版 API
        mobile_url = self.MOBILE_API.format(article_id=article_id)
        resp = self._fetch(mobile_url)
        return self._parse_mobile_page(resp.text, article_id)

    def _parse_pc_page(self, html: str, article_id: str) -> dict:
        """解析 PC 版 CSDN 文章页面。"""
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("h1", class_="title-article") or soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "未知标题"

        author_tag = soup.find("a", class_="follow-nickName") or soup.find(
            "span", class_="nick-name"
        )
        author = author_tag.get_text(strip=True) if author_tag else "未知作者"

        # 移除付费遮罩
        for mask in soup.select(
            ".article-show-more, .hide-article-box, .article-end-ele, "
            "#article_content .hide"
        ):
            mask.decompose()

        content_tag = soup.find("div", id="article_content") or soup.find(
            "div", class_="article_content"
        )
        if content_tag:
            content = content_tag.get_text(separator="\n", strip=True)
        else:
            content = strip_html(html)

        return {
            "article_id": article_id,
            "title": title,
            "author": author,
            "content": content,
            "source": "pc",
        }

    def _parse_mobile_page(self, html: str, article_id: str) -> dict:
        """解析移动版 CSDN 文章页面。"""
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("h1") or soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else "未知标题"

        content_tag = (
            soup.find("div", class_="article-content")
            or soup.find("div", id="article_content")
            or soup.find("article")
        )
        content = (
            content_tag.get_text(separator="\n", strip=True)
            if content_tag
            else strip_html(html)
        )

        return {
            "article_id": article_id,
            "title": title,
            "author": "未知作者",
            "content": content,
            "source": "mobile",
        }

    def save_article(self, result: dict, output_dir: str = ".") -> str:
        """将文章保存到文本文件。

        Returns:
            str: 保存的文件路径。
        """
        os.makedirs(output_dir, exist_ok=True)
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", result["title"])[:MAX_FILENAME_LENGTH]
        filename = f"{result['article_id']}_{safe_title}.txt"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"标题: {result['title']}\n")
            f.write(f"作者: {result['author']}\n")
            f.write(f"文章ID: {result['article_id']}\n")
            f.write("=" * 60 + "\n\n")
            f.write(result["content"])
        return filepath


class CSDNDownloader:
    """下载 CSDN 资源文件。"""

    RESOURCE_API = (
        "https://download.csdn.net/api/detail/detail?resourceId={resource_id}"
    )
    RESOURCE_PAGE = "https://download.csdn.net/download/{username}/{resource_id}"

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _fetch(self, url: str) -> requests.Response:
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp

    def parse_resource_id(self, url: str):
        """从 CSDN 资源下载 URL 中提取资源 ID 和用户名。

        Returns:
            tuple: (username, resource_id) 或在解析失败时引发 ValueError。
        """
        m = DOWNLOAD_PATTERN.match(url)
        if not m:
            raise ValueError(f"无效的 CSDN 资源链接: {url}")
        resource_id = m.group(1)
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        username = parts[1] if len(parts) > 1 else "unknown"
        return username, resource_id

    def get_resource_info(self, url: str) -> dict:
        """获取 CSDN 资源详细信息（标题、描述、大小等）。

        Returns:
            dict: 资源信息字段。
        """
        username, resource_id = self.parse_resource_id(url)
        page_url = self.RESOURCE_PAGE.format(
            username=username, resource_id=resource_id
        )
        resp = self._fetch(page_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        title_tag = soup.find("div", class_="resource_title") or soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "未知资源"

        desc_tag = soup.find("div", class_="resource_description") or soup.find(
            "div", class_="desc"
        )
        description = desc_tag.get_text(strip=True) if desc_tag else ""

        return {
            "resource_id": resource_id,
            "username": username,
            "title": title,
            "description": description,
            "page_url": page_url,
        }

    def download(self, url: str, output_dir: str = ".") -> str:
        """尝试下载 CSDN 资源文件。

        首先获取资源信息，然后尝试直接下载链接。
        注意：部分资源需要登录或积分，此工具提供尽力而为的下载尝试。

        Returns:
            str: 保存的文件路径，或在无法下载时引发 RuntimeError。
        """
        info = self.get_resource_info(url)
        os.makedirs(output_dir, exist_ok=True)

        # 尝试从资源页面找到直接下载链接
        username, resource_id = self.parse_resource_id(url)
        page_url = self.RESOURCE_PAGE.format(
            username=username, resource_id=resource_id
        )
        resp = self._fetch(page_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        download_link = None
        for tag in soup.select("a[href*='download']"):
            href = tag.get("href", "")
            try:
                parsed = urllib.parse.urlparse(href)
                host = parsed.hostname or ""
                # 仅接受 CSDN 自身域名，防止开放重定向或恶意链接
                if (host == "download.csdn.net" or host.endswith(".csdn.net")) and resource_id in href:
                    download_link = href
                    break
            except Exception:
                continue

        if not download_link:
            # 尝试构造标准下载 URL
            download_link = f"https://download.csdn.net/download/{username}/{resource_id}"

        try:
            dl_resp = self.session.get(
                download_link, timeout=self.timeout, stream=True, allow_redirects=True
            )
            dl_resp.raise_for_status()

            content_type = dl_resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                raise RuntimeError(
                    "该资源需要登录或积分才能下载。请登录后使用浏览器下载，"
                    f"资源页面: {page_url}"
                )

            # 推断文件名
            cd = dl_resp.headers.get("Content-Disposition", "")
            filename_match = re.search(r'filename[^;=\n]*=([\'"]?)([^\'";\n]+)\1', cd)
            if filename_match:
                filename = filename_match.group(2).strip()
            else:
                safe_title = re.sub(r'[\\/:*?"<>|]', "_", info["title"])[:MAX_DOWNLOAD_FILENAME_LENGTH]
                filename = f"{resource_id}_{safe_title}"

            filepath = os.path.join(output_dir, filename)
            with open(filepath, "wb") as f:
                for chunk in dl_resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return filepath
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"下载失败: {exc}。请尝试手动访问: {page_url}"
            ) from exc


def _print_article(result: dict):
    print(f"\n{'=' * 60}")
    print(f"标题: {result['title']}")
    print(f"作者: {result['author']}")
    print(f"文章ID: {result['article_id']}")
    print(f"{'=' * 60}\n")
    # 只打印前 2000 个字符避免终端输出过长
    content = result["content"]
    if len(content) > MAX_CONSOLE_CONTENT_LENGTH:
        print(content[:MAX_CONSOLE_CONTENT_LENGTH])
        print(f"\n... (内容已截断，完整内容请查看保存的文件)")
    else:
        print(content)


def main():
    parser = argparse.ArgumentParser(
        description="CSDN 文章解锁与资源下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  解锁文章:
    python csdn_unlock.py article https://blog.csdn.net/user/article/details/12345678

  下载资源:
    python csdn_unlock.py download https://download.csdn.net/download/user/12345678

  解锁并保存文章到指定目录:
    python csdn_unlock.py article https://blog.csdn.net/user/article/details/12345678 -o ./output
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # article 子命令
    article_parser = subparsers.add_parser("article", help="解锁 CSDN 博客文章")
    article_parser.add_argument("url", help="CSDN 文章 URL")
    article_parser.add_argument(
        "-o", "--output", default=".", help="保存目录 (默认: 当前目录)"
    )
    article_parser.add_argument(
        "--no-save", action="store_true", help="仅显示内容，不保存文件"
    )

    # download 子命令
    download_parser = subparsers.add_parser("download", help="下载 CSDN 资源文件")
    download_parser.add_argument("url", help="CSDN 资源下载 URL")
    download_parser.add_argument(
        "-o", "--output", default=".", help="保存目录 (默认: 当前目录)"
    )

    args = parser.parse_args()

    if args.command == "article":
        print(f"正在解锁文章: {args.url}")
        unlocker = CSDNUnlocker()
        try:
            result = unlocker.unlock_article(args.url)
            _print_article(result)
            if not args.no_save:
                filepath = unlocker.save_article(result, args.output)
                print(f"\n文章已保存到: {filepath}")
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        except requests.RequestException as e:
            print(f"网络请求失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "download":
        print(f"正在获取资源信息: {args.url}")
        downloader = CSDNDownloader()
        try:
            info = downloader.get_resource_info(args.url)
            print(f"资源标题: {info['title']}")
            print(f"正在下载...")
            filepath = downloader.download(args.url, args.output)
            print(f"下载完成: {filepath}")
        except ValueError as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        except RuntimeError as e:
            print(f"下载失败: {e}", file=sys.stderr)
            sys.exit(1)
        except requests.RequestException as e:
            print(f"网络请求失败: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
