# Copyright 2024 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""60dB Text-to-Speech plugin for the LiveKit Agents framework.

This mirrors the structure of `livekit.plugins.elevenlabs.tts` so it behaves
identically inside a `VoicePipelineAgent`:

  * `synthesize()` returns a `ChunkedStream` (one-shot, used by `agent.say` when
    streaming is bypassed).
  * `stream()` returns a `SynthesizeStream` that streams LLM tokens into the
    60dB WebSocket API and emits audio frames as they arrive.

Both paths use the WebSocket API (`wss://api.60db.ai/ws/tts`) with `LINEAR16`
PCM output, which the LiveKit pipeline consumes natively (no MP3/OGG decode).

API reference: https://docs.60db.ai/websocket-api/tts
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import weakref
from dataclasses import dataclass
from typing import Any, List, Literal, Optional

import aiohttp
from livekit import rtc
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    tokenize,
    tts,
    utils,
)

from .log import logger

# Audio encodings supported by the 60dB WebSocket API.
SixtyDBEncoding = Literal["LINEAR16", "PCM", "MULAW", "ULAW", "OGG_OPUS"]

# Sample rates the LINEAR16/PCM encodings accept.
SixtyDBSampleRate = Literal[8000, 16000, 24000, 48000]

# Default voice documented by 60dB for the WebSocket TTS endpoint.
DEFAULT_VOICE_ID = "fbb75ed2-975a-40c7-9e06-38e30524a9a1"

API_WS_URL = "wss://api.60db.ai/ws/tts"


@dataclass
class Voice:
    """Minimal voice descriptor returned by `TTS.list_voices()`."""

    id: str
    name: str
    model: str | None = None
    language: str | None = None
    gender: str | None = None
    accent: str | None = None


@dataclass
class _TTSOptions:
    api_key: str
    voice_id: str
    base_url: str
    sample_rate: int
    encoding: SixtyDBEncoding
    speed: float
    stability: float
    similarity: float
    word_tokenizer: tokenize.WordTokenizer


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        voice_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        encoding: SixtyDBEncoding = "LINEAR16",
        sample_rate: SixtyDBSampleRate = 24000,
        speed: float = 1.0,
        stability: float = 50.0,
        similarity: float = 75.0,
        word_tokenizer: tokenize.WordTokenizer = tokenize.basic.WordTokenizer(
            ignore_punctuation=False  # punctuation helps intonation
        ),
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Create a new instance of 60dB TTS.

        Args:
            voice_id: 60dB voice identifier. Falls back to the `SIXTYDB_VOICE_ID`
                env var, then the documented default voice.
            api_key: 60dB API key. Falls back to the `SIXTYDB_API_KEY` env var.
            base_url: Override the WebSocket base URL. Falls back to the
                `SIXTYDB_WS_URL` env var, then `wss://api.60db.ai/ws/tts`.
            encoding: Audio encoding. `LINEAR16` is recommended (raw PCM straight
                into the LiveKit pipeline).
            sample_rate: Output sample rate in Hz (8000/16000/24000/48000).
            speed: Speech rate multiplier, 0.5–2.0.
            stability: Voice consistency, 0–100 (higher = more consistent).
            similarity: Voice matching fidelity, 0–100.
            word_tokenizer: Tokenizer used to segment streamed text.
            http_session: Optional shared aiohttp session.
        """
        if encoding not in ("LINEAR16", "PCM"):
            # MULAW/ULAW are 8kHz G.711 and OGG_OPUS needs a decoder; the raw
            # PCM path is the only one we can feed to AudioByteStream directly.
            raise ValueError(
                f"sixtydb plugin only supports LINEAR16/PCM encoding, got {encoding!r}"
            )

        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )

        api_key = api_key or os.environ.get("SIXTYDB_API_KEY")
        if not api_key:
            raise ValueError(
                "60dB API key is required, either as argument or set the "
                "SIXTYDB_API_KEY environment variable"
            )

        self._opts = _TTSOptions(
            api_key=api_key,
            voice_id=voice_id
            or os.environ.get("SIXTYDB_VOICE_ID")
            or DEFAULT_VOICE_ID,
            base_url=base_url or os.environ.get("SIXTYDB_WS_URL") or API_WS_URL,
            sample_rate=sample_rate,
            encoding=encoding,
            speed=speed,
            stability=stability,
            similarity=similarity,
            word_tokenizer=word_tokenizer,
        )
        self._session = http_session
        self._pool = utils.ConnectionPool[aiohttp.ClientWebSocketResponse](
            connect_cb=self._connect_ws,
            close_cb=self._close_ws,
        )
        self._streams = weakref.WeakSet[SynthesizeStream]()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        session = self._ensure_session()
        return await asyncio.wait_for(
            session.ws_connect(_ws_url(self._opts)),
            self._conn_options.timeout,
        )

    async def _close_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.close()

    async def list_voices(self) -> List[Voice]:
        """Fetch the authenticated user's voices from the 60dB REST API."""
        # REST host derived from the WS host (wss://api.60db.ai -> https://api.60db.ai)
        rest_base = "https://api.60db.ai"
        async with self._ensure_session().get(
            f"{rest_base}/myvoices",
            headers={"Authorization": f"Bearer {self._opts.api_key}"},
        ) as resp:
            return _dict_to_voices_list(await resp.json())

    def update_options(
        self,
        *,
        voice_id: str | None = None,
        speed: float | None = None,
        stability: float | None = None,
        similarity: float | None = None,
    ) -> None:
        self._opts.voice_id = voice_id or self._opts.voice_id
        self._opts.speed = speed if speed is not None else self._opts.speed
        self._opts.stability = (
            stability if stability is not None else self._opts.stability
        )
        self._opts.similarity = (
            similarity if similarity is not None else self._opts.similarity
        )

    def synthesize(
        self,
        text: str,
        *,
        conn_options: Optional[APIConnectOptions] = None,
    ) -> "ChunkedStream":
        return ChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
            opts=self._opts,
            session=self._ensure_session(),
        )

    def stream(
        self, *, conn_options: Optional[APIConnectOptions] = None
    ) -> "SynthesizeStream":
        stream = SynthesizeStream(tts=self, pool=self._pool, opts=self._opts)
        self._streams.add(stream)
        return stream

    async def aclose(self) -> None:
        for stream in list(self._streams):
            await stream.aclose()
        self._streams.clear()
        await self._pool.aclose()
        await super().aclose()


class ChunkedStream(tts.ChunkedStream):
    """One-shot synthesis over the 60dB WebSocket API."""

    def __init__(
        self,
        *,
        tts: TTS,
        input_text: str,
        opts: _TTSOptions,
        session: aiohttp.ClientSession,
        conn_options: Optional[APIConnectOptions] = None,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._opts, self._session = opts, session

    async def _run(self) -> None:
        request_id = utils.shortuuid()
        context_id = utils.shortuuid()
        bstream = utils.audio.AudioByteStream(
            sample_rate=self._opts.sample_rate, num_channels=1
        )

        try:
            ws = await asyncio.wait_for(
                self._session.ws_connect(_ws_url(self._opts)),
                self._conn_options.timeout,
            )
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except Exception as e:
            raise APIConnectionError() from e

        async with ws:
            try:
                await ws.send_str(_create_context_pkt(self._opts, context_id))
                await ws.send_str(
                    json.dumps(
                        {
                            "send_text": {
                                "context_id": context_id,
                                "text": self._input_text,
                            }
                        }
                    )
                )
                # flush triggers synthesis; we end on `flush_completed` rather
                # than `close_context` (which the server says tears down the
                # whole socket). The `async with ws` block closes it for us.
                await ws.send_str(
                    json.dumps({"flush_context": {"context_id": context_id}})
                )

                while True:
                    msg = await ws.receive()
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    data = json.loads(msg.data)
                    if "audio_chunk" in data:
                        if not _ctx_matches(data["audio_chunk"], context_id):
                            continue
                        audio = base64.b64decode(data["audio_chunk"]["audioContent"])
                        for frame in bstream.write(audio):
                            self._event_ch.send_nowait(
                                tts.SynthesizedAudio(
                                    request_id=request_id, frame=frame
                                )
                            )
                    elif "flush_completed" in data:
                        if not _ctx_matches(data["flush_completed"], context_id):
                            continue
                        break
                    elif "error" in data:
                        raise APIStatusError(
                            data["error"].get("message", "60db reported an error"),
                            request_id=request_id,
                        )
                    # connecting / connection_established / context_created /
                    # flush_completed -> ignored

                for frame in bstream.flush():
                    self._event_ch.send_nowait(
                        tts.SynthesizedAudio(request_id=request_id, frame=frame)
                    )

            except asyncio.TimeoutError as e:
                raise APITimeoutError() from e
            except aiohttp.ClientResponseError as e:
                raise APIStatusError(
                    message=e.message,
                    status_code=e.status,
                    request_id=request_id,
                    body=None,
                ) from e
            except APIStatusError:
                raise
            except Exception as e:
                raise APIConnectionError() from e


class SynthesizeStream(tts.SynthesizeStream):
    """Streamed synthesis over the 60dB WebSocket API."""

    def __init__(
        self,
        *,
        tts: TTS,
        pool: utils.ConnectionPool[aiohttp.ClientWebSocketResponse],
        opts: _TTSOptions,
    ) -> None:
        super().__init__(tts=tts)
        self._opts, self._pool = opts, pool

    async def _run(self) -> None:
        request_id = utils.shortuuid()
        self._segments_ch = utils.aio.Chan[tokenize.WordStream]()

        @utils.log_exceptions(logger=logger)
        async def _tokenize_input():
            """Split text from the input channel into per-segment word streams."""
            word_stream = None
            async for input in self._input_ch:
                if isinstance(input, str):
                    if word_stream is None:
                        # new segment (e.g. after a flush)
                        word_stream = self._opts.word_tokenizer.stream()
                        self._segments_ch.send_nowait(word_stream)
                    word_stream.push_text(input)
                elif isinstance(input, self._FlushSentinel):
                    if word_stream is not None:
                        word_stream.end_input()
                    word_stream = None
            self._segments_ch.close()

        @utils.log_exceptions(logger=logger)
        async def _process_segments():
            async for word_stream in self._segments_ch:
                await self._run_ws(word_stream, request_id)

        tasks = [
            asyncio.create_task(_tokenize_input()),
            asyncio.create_task(_process_segments()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.TimeoutError as e:
            raise APITimeoutError() from e
        except aiohttp.ClientResponseError as e:
            raise APIStatusError(
                message=e.message,
                status_code=e.status,
                request_id=request_id,
                body=None,
            ) from e
        except APIStatusError:
            raise
        except Exception as e:
            raise APIConnectionError() from e
        finally:
            await utils.aio.gracefully_cancel(*tasks)

    async def _run_ws(
        self,
        word_stream: tokenize.WordStream,
        request_id: str,
    ) -> None:
        async with self._pool.connection() as ws_conn:
            context_id = utils.shortuuid()
            segment_id = utils.shortuuid()

            await ws_conn.send_str(_create_context_pkt(self._opts, context_id))

            async def send_task():
                async for data in word_stream:
                    # 60dB accumulates send_text calls; one per word, trailing
                    # space preserves word boundaries (same convention as 11labs).
                    self._mark_started()
                    await ws_conn.send_str(
                        json.dumps(
                            {
                                "send_text": {
                                    "context_id": context_id,
                                    "text": f"{data.token} ",
                                }
                            }
                        )
                    )
                # flush triggers synthesis. We deliberately do NOT send
                # close_context: per the 60dB spec the socket closes right after
                # context_closed, which would kill this pooled connection. Ending
                # on flush_completed keeps the socket alive for the next segment
                # (a fresh context_id is created per segment).
                await ws_conn.send_str(
                    json.dumps({"flush_context": {"context_id": context_id}})
                )

            async def recv_task():
                audio_bstream = utils.audio.AudioByteStream(
                    sample_rate=self._opts.sample_rate,
                    num_channels=1,
                )
                last_frame: rtc.AudioFrame | None = None

                def _send_last_frame(*, is_final: bool) -> None:
                    nonlocal last_frame
                    if last_frame is not None:
                        self._event_ch.send_nowait(
                            tts.SynthesizedAudio(
                                request_id=request_id,
                                segment_id=segment_id,
                                frame=last_frame,
                                is_final=is_final,
                            )
                        )
                        last_frame = None

                while True:
                    msg = await ws_conn.receive()
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        raise APIStatusError(
                            "60db connection closed unexpectedly, not all tokens "
                            "have been consumed",
                            request_id=request_id,
                        )
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    data = json.loads(msg.data)
                    if "audio_chunk" in data:
                        if not _ctx_matches(data["audio_chunk"], context_id):
                            continue
                        audio = base64.b64decode(data["audio_chunk"]["audioContent"])
                        for frame in audio_bstream.write(audio):
                            _send_last_frame(is_final=False)
                            last_frame = frame
                    elif "flush_completed" in data:
                        if not _ctx_matches(data["flush_completed"], context_id):
                            continue
                        for frame in audio_bstream.flush():
                            _send_last_frame(is_final=False)
                            last_frame = frame
                        _send_last_frame(is_final=True)
                        break
                    elif "error" in data:
                        if not _ctx_matches(data["error"], context_id):
                            continue
                        raise APIStatusError(
                            data["error"].get("message", "60db reported an error"),
                            request_id=request_id,
                        )
                    # connecting / connection_established / context_created
                    # -> ignored (we end the segment on flush_completed)

            tasks = [
                asyncio.create_task(send_task()),
                asyncio.create_task(recv_task()),
            ]
            try:
                await asyncio.gather(*tasks)
            except asyncio.TimeoutError as e:
                raise APITimeoutError() from e
            except aiohttp.ClientResponseError as e:
                raise APIStatusError(
                    message=e.message,
                    status_code=e.status,
                    request_id=request_id,
                    body=None,
                ) from e
            except APIStatusError:
                raise
            except Exception as e:
                raise APIConnectionError() from e
            finally:
                await utils.aio.gracefully_cancel(*tasks)


def _ws_url(opts: _TTSOptions) -> str:
    return f"{opts.base_url}?apiKey={opts.api_key}"


def _create_context_pkt(opts: _TTSOptions, context_id: str) -> str:
    return json.dumps(
        {
            "create_context": {
                "context_id": context_id,
                "voice_id": opts.voice_id,
                "audio_config": {
                    "audio_encoding": opts.encoding,
                    "sample_rate_hertz": opts.sample_rate,
                },
                "speed": opts.speed,
                "stability": opts.stability,
                "similarity": opts.similarity,
            }
        }
    )


def _ctx_matches(payload: dict[str, Any], context_id: str) -> bool:
    """Pooled connections can carry multiple contexts; only act on ours.

    Treats a missing context_id as a match (some frames may omit it).
    """
    ctx = payload.get("context_id")
    return ctx is None or ctx == context_id


def _dict_to_voices_list(data: dict[str, Any]) -> List[Voice]:
    voices: List[Voice] = []
    for voice in data.get("data", []):
        labels = voice.get("labels", {}) or {}
        voices.append(
            Voice(
                id=voice["voice_id"],
                name=voice.get("name", ""),
                model=voice.get("model"),
                language=labels.get("language"),
                gender=labels.get("gender"),
                accent=labels.get("accent"),
            )
        )
    return voices
