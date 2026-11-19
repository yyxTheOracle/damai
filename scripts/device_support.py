"""运行入口和诊断工具共用的 Mac/Windows ADB 与已适配手机信息。"""
from pathlib import Path
import shutil
import sys


XIAOMI_15_MODEL = "24129PN74C"
SCREEN_SIZES = {"24117RK2CC": (1440, 3200), XIAOMI_15_MODEL: (1200, 2670)}
# 2026-11-16，大麦 9.0.38：已核对详情、票档确定、订单提交三个按钮的共同覆盖区域。
XIAOMI_15_MAIN_ACTION_POINT = (930, 2502)


def default_adb_path() -> Path:
    """优先沿用项目内 Platform-Tools，否则使用系统 PATH 的原生 adb。"""
    executable = "adb.exe" if sys.platform == "win32" else "adb"
    local = Path(__file__).resolve().parent.parent / "platform-tools" / executable
    if local.is_file():
        return local
    return Path(shutil.which("adb") or local)
