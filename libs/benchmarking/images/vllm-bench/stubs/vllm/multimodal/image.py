def convert_image_mode(image, to_mode: str):
    return image if image.mode == to_mode else image.convert(to_mode)
