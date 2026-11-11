# peek

**像 QuPath 一样缩放浏览服务器上的大图：切图在服务器上完成，你在自己电脑的浏览器里看，不用下载文件。**

[English](README.md)

## 安装

在你自己的电脑上（macOS、Linux 或 Windows WSL），不需要 sudo：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxiaZzz/peek/main/install.sh | bash
```

## 使用

```bash
peek gpu-server:/data/slides            # 浏览服务器上的文件夹（缩略图网格）
peek gpu-server:/data/slides/a.ome.tif  # 直接打开一张
peek ~/Pictures/big.png                 # 本机的图
```

`gpu-server` 是 ssh 能连的任何名字：`~/.ssh/config` 里的名字，或 `user@host`。

浏览器里：滚轮缩放、拖动平移；← / → 上一张/下一张；Esc 回文件夹；`0` 适应窗口；`f` 全屏；⌘+点击缩略图在新标签页打开，方便并排对比。终端里按 Ctrl-C 结束，关掉终端也会自动清理。

## 服务器需要什么

只需要 Python 和几个常见的包：PNG/JPEG 需要 `pillow`；OME-TIFF 需要 `tifffile` `zarr` `imagecodecs`；`.svs` `.ndpi` 等需要 `openslide-python` `openslide-bin`。peek 会自动在你的 conda 环境里挑装得最全的那个（CLAM、TRIDENT、CellViT 这类环境一般都有）。缺的话会问你要不要装，或者手动运行 `peek --setup gpu-server`（`pip --user`，只装在你的用户目录）。

## 原理

peek 通过 ssh 把一个小 Python 程序临时传到服务器上运行（不安装），它只监听服务器本机的 127.0.0.1，再经 ssh 隧道转到你的电脑。多分辨率文件（OME-TIFF、.svs 等）每块瓦片都从最接近的那一层读取，缩小时不会去解码全分辨率图像，所以几 GB 的切片也流畅。

## 许可证

Apache-2.0
