"""用标准 JDK 17+ 和固定版本的 Google 构建工具生成仅含 classes.dex 的 jar。

用法：选好 JAVA_HOME 后，运行 python3 build.py [工具缓存目录]。
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent
R8 = ("https://dl.google.com/dl/android/maven2/com/android/tools/r8/8.7.18/r8-8.7.18.jar",
      "58366f77067207c39a17d469de7b05701d2877212a9c55201bcb0af43e59e903")
PLATFORM = ("https://dl.google.com/android/repository/platform-35_r02.zip",
            "0988cacad01b38a18a47bac14a0695f246bc76c1b06c0eeb8eb0dc825ab0c8e0")


def verified_tool(cache, name, source):
    """首次从 Google 下载工具。每次使用前校验下载内容或本地缓存。"""
    path = cache / name
    url, digest = source
    if not path.exists():
        with urllib.request.urlopen(url, timeout=90) as response, path.open("wb") as target:
            shutil.copyfileobj(response, target)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError(f"{path} SHA-256 校验失败，请删除该文件后重试")
    return path


def build(cache):
    """用同一 SDK 和 D8 构建，把 dex 以固定时间戳写入运行包，同时输出校验文件。"""
    cache.mkdir(parents=True, exist_ok=True)
    r8 = verified_tool(cache, "r8.jar", R8)
    platform = verified_tool(cache, "platform-35_r02.zip", PLATFORM)
    java_bin = Path(os.environ["JAVA_HOME"]) / "bin" if "JAVA_HOME" in os.environ else Path()
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        android = work / "android.jar"
        with zipfile.ZipFile(platform) as archive, android.open("wb") as target:
            with archive.open("android-35/android.jar") as source:
                shutil.copyfileobj(source, target)
        subprocess.run([java_bin / "javac", "--release", "8", "-g:none", "-encoding", "UTF-8",
                        "-cp", android, "-d", work, HERE / "ScopedInput.java"], check=True)
        subprocess.run([java_bin / "java", "-cp", r8, "com.android.tools.r8.D8", "--min-api", "33",
                        "--release", "--lib", android, "--output", work,
                        *sorted(work.rglob("*.class"))], check=True)
        jar = HERE / "scoped_input.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(zipfile.ZipInfo("classes.dex"), (work / "classes.dex").read_bytes())
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        (HERE / "scoped_input.jar.sha256").write_text(f"{digest}  {jar.name}\n", encoding="ascii")
        print(digest, jar)


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".cache/damai-scoped-input")
