"""
单元测试：CSDN 文章解锁与资源下载工具
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csdn_unlock import (
    ARTICLE_PATTERN,
    DOWNLOAD_PATTERN,
    CSDNDownloader,
    CSDNUnlocker,
    HTMLStripper,
    strip_html,
)


class TestHTMLStripper(unittest.TestCase):
    def test_strip_simple_tags(self):
        self.assertEqual(strip_html("<p>Hello</p>"), "Hello")

    def test_strip_nested_tags(self):
        self.assertEqual(strip_html("<div><p>Hello <b>World</b></p></div>"), "Hello World")

    def test_plain_text_passthrough(self):
        self.assertEqual(strip_html("plain text"), "plain text")

    def test_empty_string(self):
        self.assertEqual(strip_html(""), "")


class TestURLPatterns(unittest.TestCase):
    def test_article_pattern_matches_valid(self):
        url = "https://blog.csdn.net/user123/article/details/12345678"
        m = ARTICLE_PATTERN.match(url)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "12345678")

    def test_article_pattern_no_match_on_invalid(self):
        self.assertIsNone(ARTICLE_PATTERN.match("https://csdn.net/other/path"))

    def test_download_pattern_matches_valid(self):
        url = "https://download.csdn.net/download/user123/87654321"
        m = DOWNLOAD_PATTERN.match(url)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "87654321")

    def test_download_pattern_no_match_on_invalid(self):
        self.assertIsNone(DOWNLOAD_PATTERN.match("https://csdn.net/download/other"))


class TestCSDNUnlocker(unittest.TestCase):
    def setUp(self):
        self.unlocker = CSDNUnlocker()

    def test_parse_article_id_valid(self):
        url = "https://blog.csdn.net/johndoe/article/details/99887766"
        username, article_id = self.unlocker.parse_article_id(url)
        self.assertEqual(article_id, "99887766")
        self.assertEqual(username, "johndoe")

    def test_parse_article_id_invalid(self):
        with self.assertRaises(ValueError):
            self.unlocker.parse_article_id("https://example.com/not-csdn")

    def test_parse_pc_page(self):
        html = """
        <html><body>
          <h1 class="title-article">测试标题</h1>
          <a class="follow-nickName">测试作者</a>
          <div id="article_content">
            <p>这是文章正文内容。</p>
            <div class="hide-article-box">付费内容</div>
          </div>
        </body></html>
        """
        result = self.unlocker._parse_pc_page(html, "12345")
        self.assertEqual(result["title"], "测试标题")
        self.assertEqual(result["author"], "测试作者")
        self.assertIn("这是文章正文内容", result["content"])
        self.assertNotIn("付费内容", result["content"])
        self.assertEqual(result["article_id"], "12345")
        self.assertEqual(result["source"], "pc")

    def test_parse_mobile_page(self):
        html = """
        <html><body>
          <h1>移动端标题</h1>
          <div class="article-content">
            <p>移动端文章内容。</p>
          </div>
        </body></html>
        """
        result = self.unlocker._parse_mobile_page(html, "67890")
        self.assertEqual(result["title"], "移动端标题")
        self.assertIn("移动端文章内容", result["content"])
        self.assertEqual(result["article_id"], "67890")
        self.assertEqual(result["source"], "mobile")

    def test_save_article(self):
        result = {
            "article_id": "11111",
            "title": "测试保存标题",
            "author": "测试作者",
            "content": "这是保存测试的文章内容。",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self.unlocker.save_article(result, tmpdir)
            self.assertTrue(os.path.exists(filepath))
            with open(filepath, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("测试保存标题", text)
            self.assertIn("测试作者", text)
            self.assertIn("这是保存测试的文章内容", text)

    def test_save_article_sanitizes_filename(self):
        result = {
            "article_id": "22222",
            "title": 'Title/With\\Invalid:Chars*?<>|"',
            "author": "作者",
            "content": "内容",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self.unlocker.save_article(result, tmpdir)
            self.assertTrue(os.path.exists(filepath))
            # 文件名不应包含非法字符
            basename = os.path.basename(filepath)
            for ch in r'\/:*?"<>|':
                self.assertNotIn(ch, basename)

    @patch("csdn_unlock.requests.Session.get")
    def test_unlock_article_calls_pc_first(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = """
        <html><body>
          <h1 class="title-article">网络请求标题</h1>
          <a class="follow-nickName">网络作者</a>
          <div id="article_content"><p>网络内容。</p></div>
        </body></html>
        """
        mock_get.return_value = mock_resp

        url = "https://blog.csdn.net/testuser/article/details/55556666"
        result = self.unlocker.unlock_article(url)
        self.assertEqual(result["article_id"], "55556666")
        self.assertEqual(result["title"], "网络请求标题")
        # PC 页面应当是第一个被调用
        first_call_url = mock_get.call_args_list[0][0][0]
        self.assertIn("blog.csdn.net", first_call_url)


class TestCSDNDownloader(unittest.TestCase):
    def setUp(self):
        self.downloader = CSDNDownloader()

    def test_parse_resource_id_valid(self):
        url = "https://download.csdn.net/download/johndoe/11223344"
        username, resource_id = self.downloader.parse_resource_id(url)
        self.assertEqual(resource_id, "11223344")
        self.assertEqual(username, "johndoe")

    def test_parse_resource_id_invalid(self):
        with self.assertRaises(ValueError):
            self.downloader.parse_resource_id("https://example.com/not-csdn")

    @patch("csdn_unlock.requests.Session.get")
    def test_get_resource_info(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = """
        <html><body>
          <div class="resource_title">测试资源标题</div>
          <div class="resource_description">资源描述信息</div>
        </body></html>
        """
        mock_get.return_value = mock_resp

        url = "https://download.csdn.net/download/testuser/99998888"
        info = self.downloader.get_resource_info(url)
        self.assertEqual(info["resource_id"], "99998888")
        self.assertEqual(info["title"], "测试资源标题")
        self.assertIn("资源描述", info["description"])

    @patch("csdn_unlock.requests.Session.get")
    def test_download_raises_when_html_returned(self, mock_get):
        """当服务器返回 HTML（登录页）时应抛出 RuntimeError。"""
        page_html = """
        <html><body>
          <div class="resource_title">需要登录的资源</div>
        </body></html>
        """

        def make_html_resp():
            r = MagicMock()
            r.raise_for_status.return_value = None
            r.text = page_html
            return r

        # get_resource_info 和 download 内部各调用一次 _fetch（页面），
        # 第三次调用是实际下载请求，返回 HTML 内容（模拟需要登录的页面）
        dl_resp = MagicMock()
        dl_resp.raise_for_status.return_value = None
        dl_resp.headers = {"Content-Type": "text/html; charset=utf-8"}

        mock_get.side_effect = [make_html_resp(), make_html_resp(), dl_resp]

        with self.assertRaises(RuntimeError) as ctx:
            self.downloader.download(
                "https://download.csdn.net/download/testuser/77776666", "/tmp"
            )
        self.assertIn("登录", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
