import os
import re
import random
import logging
import aiohttp
from datetime import datetime
from typing import Annotated

from dotenv import load_dotenv
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
    metrics,
)
from livekit.agents.pipeline import AgentCallContext, VoicePipelineAgent
from livekit.rtc import ParticipantKind
from livekit.plugins import openai, deepgram, elevenlabs, silero, turn_detector


load_dotenv(dotenv_path=".env.local")
logger = logging.getLogger("voice-agent")


class AssistantFnc(llm.FunctionContext):
    # the llm.ai_callable decorator marks this function as a tool available to the LLM
    # by default, it'll use the docstring as the function's description
    @llm.ai_callable()
    async def get_weather(
        self,
        # by using the Annotated type, arg description and type are available to the LLM
        location: Annotated[
            str, llm.TypeInfo(description="The location to get the weather for")
        ],
    ):
        """Called when the user asks about the weather. This function will return the weather for the given location."""
        # Clean the location string of special characters
        location = re.sub(r"[^a-zA-Z0-9]+", " ", location).strip()

        # When a function call is running, there are a couple of options to inform the user
        # that it might take awhile:
        # Option 1: you can use .say filler message immediately after the call is triggered
        # Option 2: you can prompt the agent to return a text response when it's making a function call
        agent = AgentCallContext.get_current().agent

        if (
            not agent.chat_ctx.messages
            or agent.chat_ctx.messages[-1].role != "assistant"
        ):
            # skip if assistant already said something
            filler_messages = [
                "Let me check the weather in {location} for you.",
                "Let me see what the weather is like in {location} right now.",
                # LLM will complete this sentence if it is added to the end of the chat context
                "The current weather in {location} is ",
            ]
            message = random.choice(filler_messages).format(location=location)
            logger.info(f"saying filler message: {message}")

            # NOTE: set add_to_chat_ctx=True will add the message to the end
            #   of the chat context of the function call for answer synthesis
            speech_handle = await agent.say(message, add_to_chat_ctx=True)  # noqa: F841

        logger.info(f"getting weather for {location}")
        url = f"https://wttr.in/{location}?format=%C+%t"
        weather_data = ""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    weather_data = (f"The weather in {location} is {await response.text()}.")
                    logger.info(f"weather data: {weather_data}")
                else:
                    raise f"Failed to get weather data, status code: {response.status}"
                
        # (optional) To wait for the speech to finish before giving results of the function call
        # await speech_handle.join()
        return weather_data
    
    @llm.ai_callable()
    def get_time(self):
        """called to retrieve the current local time"""
        return datetime.now().strftime("%H:%M:%S")


def build_tts():
    """Select the TTS engine via the TTS_PROVIDER env var.

    TTS_PROVIDER=elevenlabs (default) -> ElevenLabs
    TTS_PROVIDER=60db                 -> 60dB (local `sixtydb` plugin)
    """
    provider = os.getenv("TTS_PROVIDER", "elevenlabs").strip().lower()
    if provider in ("60db", "sixtydb", "60"):
        # imported lazily so ElevenLabs-only setups don't need 60dB configured
        import sixtydb

        logger.info("using 60dB TTS")
        return sixtydb.TTS()

    logger.info("using ElevenLabs TTS")
    return elevenlabs.TTS()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    initial_ctx = llm.ChatContext().append(
        role="system",
        text=(
            "You are a voice assistant created by LiveKit. Your interface with users will be voice. "
            "You should use short and concise responses, and avoiding usage of unpronouncable punctuation. "
            "You were created as a demo to showcase the capabilities of LiveKit's agents framework."
        ),
    )

    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Wait for the first participant to connect
    participant = await ctx.wait_for_participant()
    logger.info(f"starting voice assistant for participant {participant.identity}")
    logger.info(f"participant.name: {participant.name}")
    logger.info(f"participant.attributes: {participant.attributes}")

    dg_model = "nova-2-general"
    if participant.kind == ParticipantKind.PARTICIPANT_KIND_SIP:
        # use a model optimized for telephony
        dg_model = "nova-2-phonecall"

    # This project is configured to use Deepgram STT, OpenAI LLM and ElevenLabs TTS plugins
    # Other providers exist like Cerebras, Cartesia, Groq, Play.ht, Rime, and more
    # Learn more and pick the best one for your app:
    # https://docs.livekit.io/agents/plugins
    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model=dg_model),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=build_tts(),
        turn_detector=turn_detector.EOUModel(),
        # minimum delay for endpointing, used when turn detector believes the user is done with their turn
        min_endpointing_delay=0.5,
        # maximum delay for endpointing, used when turn detector does not believe the user is done with their turn
        max_endpointing_delay=5.0,
        chat_ctx=initial_ctx,
        fnc_ctx=AssistantFnc(),
    )

    usage_collector = metrics.UsageCollector()

    @agent.on("metrics_collected")
    def on_metrics_collected(agent_metrics: metrics.AgentMetrics):
        metrics.log_metrics(agent_metrics)
        usage_collector.collect(agent_metrics)

    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Summary Usage: {summary}")

    # At shutdown, generate and log the summary from the usage collector
    ctx.add_shutdown_callback(log_usage)

    agent.start(ctx.room, participant)

    # The agent greeting when the user joins
    await agent.say("Hey, how can I help you today?", allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="inbound-agent",
        ),
    )
