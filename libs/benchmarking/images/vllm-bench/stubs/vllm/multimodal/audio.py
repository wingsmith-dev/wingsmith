# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/multimodal/audio.py, without its torch-dependent internals; see README.md.
def get_audio_duration(y=None, sr=None, **kwargs) -> float:
    return len(y) / float(sr)
