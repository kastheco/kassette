import pytest

from kassette.domain import AudioChunk


def test_audio_chunk_accepts_complete_pcm_frames() -> None:
    chunk = AudioChunk(audio=b"\x00\x00" * 160, sample_rate=16_000, num_channels=1)

    assert chunk.sample_rate == 16_000


def test_audio_chunk_rejects_partial_pcm_frame() -> None:
    with pytest.raises(ValueError, match="complete"):
        AudioChunk(audio=b"\x00", sample_rate=16_000, num_channels=1)
