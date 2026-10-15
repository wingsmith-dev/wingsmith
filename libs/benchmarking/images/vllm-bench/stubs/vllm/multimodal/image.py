# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/multimodal/image.py, without its torch-dependent internals; see README.md.
def convert_image_mode(image, to_mode: str):
    return image if image.mode == to_mode else image.convert(to_mode)
