<div align="center">

# 夸克更新助手

盯一个夸克网盘分享：有新版就自动下载并启动，没新版就直接开本地那份。

![python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)
![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows&logoColor=white)
![by](https://img.shields.io/badge/by-Echo-orange)

</div>

---

![界面](ui_final.png)

## ✨ 特点

- **双击即用** — 打开就查，有更新自动下、自动开；没更新直接启动本地最新版
- **不内置账号** — 首次运行弹微信二维码，扫一次就行；凭据只存本机
- **不留杂物** — 配置和日志都在 `%LOCALAPPDATA%\QuarkUpdateHelper`，桌面只有程序本身
- **改名也能追上** — 文件名支持 `*` 通配，对方换名字不用重新打包

---

## 🚀 使用

1. 把 `夸克更新助手.exe` 放到任意目录（下载的文件会落在同一目录）
2. 双击打开，首次用微信扫码登录
3. 之后每次双击，剩下的事它自己办

> 目标程序若要求管理员权限，UAC 弹窗要点「是」。点「否」会报「操作已被用户取消」。

---

## 📖 文档

- [技术细节](docs/技术细节.md) — 接口、踩过的坑、构建方式
- [更新记录](docs/更新记录.md)

---

<div align="center">by **Echo**</div>
