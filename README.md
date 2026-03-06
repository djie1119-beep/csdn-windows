# csdn-windows
csdn解锁下载

## 功能

- **文章解锁**：获取 CSDN 博客文章完整内容（移除登录遮罩），并保存为本地文本文件
- **资源下载**：获取 CSDN 资源下载页面信息，尝试直接下载资源文件

## 环境要求

- Python 3.8+
- 依赖库：`requests`、`beautifulsoup4`

## 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方式

### 解锁文章

```bash
python csdn_unlock.py article https://blog.csdn.net/用户名/article/details/文章ID
```

可选参数：

| 参数 | 说明 |
|------|------|
| `-o <目录>` | 指定保存目录（默认：当前目录） |
| `--no-save` | 仅在终端显示内容，不保存文件 |

示例：

```bash
# 解锁并保存到 ./output 目录
python csdn_unlock.py article https://blog.csdn.net/user/article/details/12345678 -o ./output

# 仅显示，不保存
python csdn_unlock.py article https://blog.csdn.net/user/article/details/12345678 --no-save
```

### 下载资源

```bash
python csdn_unlock.py download https://download.csdn.net/download/用户名/资源ID
```

可选参数：

| 参数 | 说明 |
|------|------|
| `-o <目录>` | 指定保存目录（默认：当前目录） |

示例：

```bash
python csdn_unlock.py download https://download.csdn.net/download/user/87654321 -o ./downloads
```

> **注意**：部分资源需要登录或积分才能下载，工具会在无法下载时给出提示并附上资源页面链接。

## 运行测试

```bash
pip install pytest
python -m pytest tests/ -v
```
