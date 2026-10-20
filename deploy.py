# -*- coding: utf-8 -*-
"""部署：dist/QuarkWatcher.exe -> 桌面的「夸克更新助手.exe」，并把配置里的
download_dir 清空（= 跟随 exe 所在目录），删掉旧的待办txt。"""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(os.environ["USERPROFILE"]) / ".workbuddy" / "quark-watch"
SRC = ROOT / "dist" / "QuarkWatcher.exe"
DESK = Path(os.environ["USERPROFILE"]) / "Desktop"
DST = DESK / "夸克更新助手.exe"
CFG = Path(os.environ["LOCALAPPDATA"]) / "QuarkUpdateHelper" / "config.json"

assert SRC.exists(), f"找不到产物 {SRC}"
print("产物:", SRC.stat().st_size, "bytes")

# 关掉可能占用目标的进程
os.system('taskkill /F /IM 夸克更新助手.exe >nul 2>&1')

shutil.copy2(SRC, DST)
print("已部署:", DST, DST.stat().st_size, "bytes")

cfg = json.loads(CFG.read_text(encoding="utf-8"))
cfg["download_dir"] = ""          # 跟随 exe
cfg["flat"] = True
cfg["auto_launch"] = True
cfg["launch_pattern"] = "猫薄荷*.exe"
cfg["proxy"] = "auto"
CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("配置已更新: download_dir='' flat=True")
print("cookie 长度:", len(cfg.get("cookie", "")))

# 清掉旧版可能留在桌面的杂文件
for junk in ("待手动下载.txt", "config.json", "state.json", "夸克同步"):
    p = DESK / junk
    if p.exists():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
        print("已清理桌面杂物:", junk)

# 隐藏数据目录
try:
    import ctypes
    ctypes.windll.kernel32.SetFileAttributesW(str(CFG.parent), 0x02)
except Exception:
    pass

print("\n桌面 exe 检查:")
for p in sorted(DESK.glob("*.exe")):
    print(f"  {p.name}  {p.stat().st_size:,}")
