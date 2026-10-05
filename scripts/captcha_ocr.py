#!/usr/bin/env python3
"""站内验证码免费识别原型（ddddocr 本地 OCR）。

语料来源：``cf_challenge.save_captcha_sample`` 攒到 ``data/captcha_corpus/`` 的
站内验证页 HTML（最多滚动保留 50 个）。

用法::

    python scripts/captcha_ocr.py --dir data/captcha_corpus

行为：
1) 读语料目录下的 HTML，提取 ``<img>`` 验证码图片：
   - ``src`` 内联 base64（data:image/...;base64,...）直接解码；
   - 同目录相对路径（``captcha.jpg``、``./img/x.png``）按 HTML 所在目录解析读盘；
   - http/https 外链（及 ``//host/path``）下载，失败跳过；
2) 本机有 ``ddddocr`` 就调 ``classification`` 识别（try 包住）；
   没有就只输出"图片已提取、缺引擎"，绝不安装任何东西；
3) 全部 try 包住：缺目录、缺文件、坏 HTML、坏图片都不炸。
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys

_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)
_DATA_URL_RE = re.compile(r"^data:image/[^;,]+;base64,(.*)$", re.IGNORECASE | re.DOTALL)


def _extract_img_srcs(html: str) -> list:
    """抽取 HTML 里所有 <img src>，异常返回空列表，绝不抛。"""
    try:
        if not html:
            return []
        return [m.group(2).strip() for m in _IMG_SRC_RE.finditer(html) if m.group(2).strip()]
    except Exception:
        return []


def _bytes_from_data_url(src: str):
    """内联 base64 转字节串，失败返回 None（不抛）。"""
    try:
        m = _DATA_URL_RE.match(src.strip())
        if not m:
            return None
        return base64.b64decode(m.group(1), validate=False)
    except Exception:
        return None


def _bytes_from_relative(src: str, html_path: str):
    """同目录相对路径读盘，失败返回 None（不抛）。"""
    try:
        clean = src.split("?", 1)[0].split("#", 1)[0].strip()
        if not clean or clean.startswith("data:"):
            return None
        base_dir = os.path.dirname(os.path.abspath(html_path))
        for cand in (os.path.join(base_dir, clean), os.path.join(os.getcwd(), clean)):
            try:
                if os.path.isfile(cand):
                    with open(cand, "rb") as fh:
                        return fh.read()
            except Exception:
                continue
        return None
    except Exception:
        return None


def _bytes_from_url(src: str, timeout: float = 10.0):
    """http(s) 外链下载，失败返回 None（不抛）。"""
    try:
        from urllib.request import Request, urlopen

        url = src.strip()
        try:
            if url.startswith("//"):
                url = "https:" + url
        except Exception:
            pass
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as resp:
            try:
                return resp.read()
            except Exception:
                return None
    except Exception:
        return None


def load_image_bytes(src: str, html_path: str):
    """按 src 类型取图片字节串：内联 base64 / 相对路径 / http 外链；失败返回 None。"""
    try:
        s = (src or "").strip()
        if not s:
            return None
        if s.lower().startswith("data:"):
            return _bytes_from_data_url(s)
        low = s.lower()
        if low.startswith("http://") or low.startswith("https://") or s.startswith("//"):
            return _bytes_from_url(s)
        return _bytes_from_relative(s, html_path)
    except Exception:
        return None


def probe_engine():
    """探测 ddddocr：返回 (可用, 提示串)。先 import，没有就 pip show 看，都没有也不安装。"""
    try:
        import ddddocr  # noqa: F401

        return True, "ddddocr 可用"
    except Exception as e1:
        try:
            import subprocess

            try:
                out = subprocess.run(
                    [sys.executable, "-m", "pip", "show", "ddddocr"],
                    capture_output=True, text=True, timeout=20,
                )
                if out.returncode == 0 and (out.stdout or "").strip():
                    return True, "ddddocr 可用（pip show 可见）"
            except Exception:
                pass
        except Exception:
            pass
        return False, "图片已提取、缺引擎（本机无 ddddocr，未安装）"


def ocr_images(blobs: list):
    """有引擎时逐张 classification；返回 [(ok, 文本或报错), ...]；绝不抛。"""
    results = []
    try:
        import ddddocr

        try:
            ocr = ddddocr.DdddOcr(show_ad=False)
        except Exception:
            try:
                ocr = ddddocr.DdddOcr()
            except Exception as e:
                return [(False, "引擎初始化失败：%s" % e) for _ in blobs]
        for b in blobs:
            try:
                text = ocr.classification(b)  # 一行调用
                results.append((True, str(text)))
            except Exception as e:
                results.append((False, "识别失败：%s" % e))
        return results
    except Exception as e:
        return [(False, "图片已提取、缺引擎：%s" % e) for _ in blobs]


def process_file(html_path: str, engine_ok: bool):
    """处理单个 HTML：返回 dict（绝不抛，外层也包 try）。"""
    info = {"file": os.path.basename(html_path), "found": 0, "items": []}
    try:
        try:
            with open(html_path, "r", encoding="utf-8", errors="replace") as fh:
                html = fh.read()
        except Exception as e:
            info["items"].append({"src": "", "status": "读 HTML 失败：%s" % e})
            return info
        srcs = _extract_img_srcs(html)
        blobs = []
        descs = []
        for s in srcs:
            try:
                b = load_image_bytes(s, html_path)
            except Exception:
                b = None
            if b:
                blobs.append(b)
                short = s if len(s) <= 80 else (s[:77] + "...")
                kind = "内联" if s.lower().startswith("data:") else (
                    "外链" if s.lower().startswith("http") or s.startswith("//") else "相对路径")
                descs.append("%s:%s" % (kind, short))
        info["found"] = len(blobs)
        if not blobs:
            info["items"].append({"src": "", "status": "未找到可用验证码图片（共 %d 个 <img>）" % len(srcs)})
            return info
        if not engine_ok:
            for d in descs:
                info["items"].append({"src": d, "status": "图片已提取、缺引擎"})
            return info
        try:
            recs = ocr_images(blobs)
        except Exception as e:
            recs = [(False, "识别异常：%s" % e)] * len(blobs)
        for d, (ok, txt) in zip(descs, recs):
            info["items"].append({"src": d, "status": ("识别结果：%s" % txt) if ok else txt})
        return info
    except Exception as e:
        info["items"].append({"src": "", "status": "处理异常：%s" % e})
        return info


def main(argv=None) -> int:
    try:
        ap = argparse.ArgumentParser(description="站内验证码免费识别原型（ddddocr）")
        ap.add_argument("--dir", default="data/captcha_corpus", help="语料 HTML 目录")
        args = ap.parse_args(argv)
        corpus = args.dir
        try:
            files = sorted(
                os.path.join(corpus, n) for n in os.listdir(corpus)
                if n.lower().endswith((".html", ".htm"))
            )
        except Exception as e:
            print("语料目录不可读：%s（%s）" % (corpus, e))
            return 0
        if not files:
            print("语料目录为空：%s（先由 cf_challenge.save_captcha_sample 攒样本，最多 50 个）" % corpus)
            return 0
        try:
            engine_ok, engine_msg = probe_engine()
        except Exception as e:
            engine_ok, engine_msg = False, "引擎探测异常：%s" % e
        print("引擎状态：%s；待处理 %d 页" % (engine_msg, len(files)))
        total_imgs = 0
        for fp in files:
            try:
                info = process_file(fp, engine_ok)
            except Exception as e:
                print("[%s] 处理异常：%s" % (os.path.basename(fp), e))
                continue
            total_imgs += info.get("found", 0)
            print("[%s] 找到图片 %d 张" % (info["file"], info.get("found", 0)))
            try:
                for it in info.get("items", []):
                    src = it.get("src", "")
                    suffix = (" " + src) if src else ""
                    print("    - %s%s" % (it.get("status", ""), suffix))
            except Exception:
                continue
        print("共 %d 页，找到图片 %d 张。%s" % (
            len(files), total_imgs,
            "识别完成。" if engine_ok else "图片已提取、缺引擎（本机无 ddddocr）。"))
        return 0
    except Exception as e:
        try:
            print("运行异常（已兜底）：%s" % e)
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
