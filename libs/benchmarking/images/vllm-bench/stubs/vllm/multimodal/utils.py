import base64
from io import BytesIO


def fetch_image(image_url: str, **kwargs):
    raise NotImplementedError("multimodal datasets are not supported in this slim build")


def encode_image_url(image, image_format: str = "JPEG") -> str:
    buffered = BytesIO()
    image.save(buffered, format=image_format)
    encoded = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/{image_format.lower()};base64,{encoded}"
