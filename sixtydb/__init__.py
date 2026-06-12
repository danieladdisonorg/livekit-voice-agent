"""LiveKit Agents TTS plugin for 60dB (https://60db.ai).

A drop-in replacement for `livekit.plugins.elevenlabs.TTS`, built on the same
`livekit.agents.tts` base classes so it plugs into a `VoicePipelineAgent`
identically. Streams over the 60dB WebSocket TTS API using LINEAR16 PCM, which
feeds straight into the LiveKit audio pipeline with no decode step.
"""

from .tts import (
    DEFAULT_VOICE_ID,
    TTS,
    ChunkedStream,
    SynthesizeStream,
    Voice,
)
from .version import __version__

__all__ = [
    "TTS",
    "ChunkedStream",
    "SynthesizeStream",
    "Voice",
    "DEFAULT_VOICE_ID",
    "__version__",
]
