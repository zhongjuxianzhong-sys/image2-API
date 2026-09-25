"""Generate the Windows icon from the GPT Image brand mark."""

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "assets" / "GptImageStudio.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int) -> Image.Image:
    scale = 4
    canvas_size = size * scale
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    teal = (15, 118, 110, 255)
    white = (255, 255, 255, 255)
    radius = max(2, canvas_size // 5)
    inset = max(1, canvas_size // 8)
    stroke = max(1, canvas_size // 16)

    draw.rounded_rectangle(
        (0, 0, canvas_size - 1, canvas_size - 1),
        radius=radius,
        fill=teal,
    )
    frame = (inset * 2, inset * 2, canvas_size - inset * 2 - 1, canvas_size - inset * 2 - 1)
    draw.rounded_rectangle(frame, radius=max(2, radius // 2), outline=white, width=stroke)

    dot_radius = max(1, canvas_size // 14)
    dot_center = (inset * 2 + canvas_size // 5, inset * 2 + canvas_size // 5)
    draw.ellipse(
        (
            dot_center[0] - dot_radius,
            dot_center[1] - dot_radius,
            dot_center[0] + dot_radius,
            dot_center[1] + dot_radius,
        ),
        fill=white,
    )

    left = inset * 2 + stroke
    bottom = canvas_size - inset * 2 - stroke
    peak = (canvas_size // 2 + canvas_size // 12, canvas_size // 2 - canvas_size // 6)
    points = [
        (left, bottom),
        (canvas_size // 2 - canvas_size // 8, canvas_size // 2 + canvas_size // 8),
        peak,
        (canvas_size - inset * 2 - stroke, bottom),
    ]
    draw.line(points, fill=white, width=stroke, joint="curve")
    draw.line(
        [(left, bottom), (canvas_size // 2, bottom - canvas_size // 7), (canvas_size - inset * 2, bottom)],
        fill=white,
        width=stroke,
        joint="curve",
    )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
    images = [draw_icon(size) for size in SIZES]
    images[-1].save(ICON_PATH, format="ICO", sizes=[(size, size) for size in SIZES])


if __name__ == "__main__":
    main()