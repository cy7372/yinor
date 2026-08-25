"""从 yinor/static/favicon-32.png 生成 packaging/yinor.ico（构建时自动跑）。"""

from pathlib import Path

from PIL import Image


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    src = root / "yinor" / "static" / "favicon-32.png"
    out = root / "packaging" / "yinor.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src).convert("RGBA")
    # 多尺寸打包（48/256 由 32px 放大，质量一般但作为窗口/任务栏图标够用）
    # Image.LANCZOS 在 Pillow 10+ 移到 Image.Resampling.LANCZOS，取双兼容路径
    lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)]
    frames = [img.resize(s, lanczos) for s in sizes]
    frames[2].save(out, format="ICO", sizes=sizes, append_images=frames)
    print(f"icon -> {out}")


if __name__ == "__main__":
    main()
