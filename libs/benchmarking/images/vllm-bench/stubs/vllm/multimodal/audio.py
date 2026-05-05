def get_audio_duration(y=None, sr=None, **kwargs) -> float:
    return len(y) / float(sr)
