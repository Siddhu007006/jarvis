"""
core/stream_tts.py — Streaming LLM → TTS Pipeline
──────────────────
Streaming LLM → TTS pipeline.
TTS starts speaking the first sentence while LLM is still generating the rest.
Perceived response time drops from ~2.8s to ~0.9s.

Concept — How streaming works:
  Traditional: LLM generates full response (1500ms) → TTS speaks it (800ms) = 2.3s dead time
  Streaming:   LLM sends token by token → buffer into sentence → TTS speaks immediately
               → user hears first word at ~500ms while LLM is still generating

Architecture:
  1. Token producer: providers.llm_stream() yields tokens one at a time
  2. Sentence buffer: _extract_speakable_chunk() detects sentence boundaries
  3. TTS worker: background thread consumes sentence queue, speaks each via core.tts.speak_now()

  LLM (streaming) ──tokens──▶ [sentence buffer] ──sentences──▶ [TTS queue] ──audio──▶ speaker

Dependencies:
  - core.providers.llm_stream() for token generation
  - core.tts.speak_now() for audio output
"""

import re
import queue
import logging
import threading

log = logging.getLogger("JARVIS.stream_tts")

# Sentence boundary characters — start speaking when we hit one
SENTENCE_ENDS = re.compile(r'([.!?;])\s')
# Minimum chars before we speak a chunk (avoid speaking single words)
MIN_CHUNK_LEN = 25


class StreamingPipeline:
    """
    Streams LLM output token by token → buffers into sentences → speaks each.
    The user hears the first words at ~500ms instead of ~2.8s.

    Usage:
        from core.stream_tts import StreamingPipeline
        pipeline = StreamingPipeline()
        full_text = pipeline.respond("What is Python?", system_prompt="You are Jarvis.")
    """

    def __init__(self):
        """
        Initialize the streaming pipeline.

        No external dependencies are injected — we import core.providers
        and core.tts at call time to avoid circular imports and to always
        use the latest settings.
        """
        self._speech_q = queue.Queue()
        self._speaking = False

        # Phase 2: Cancel event for barge-in interruption.
        # Concept: When the user speaks during Jarvis's TTS playback,
        # external code (engine.py) calls cancel() which sets this event.
        # Both the token consumer (respond) and TTS worker check it,
        # so cancellation propagates within one queue poll cycle (~0.5s).
        self._cancel_event = threading.Event()

        # Start TTS worker thread (daemon — dies with main process)
        t = threading.Thread(target=self._tts_worker, daemon=True, name="stream-tts")
        t.start()

    # ── Main: stream response ─────────────────────────────

    def respond(self, user_input: str, system_prompt: str = "",
                context: str = "") -> str:
        """
        Stream LLM response and speak it in real time.

        Concept: We call providers.llm_stream() which yields tokens
        one at a time. As tokens arrive, we buffer them. When a
        sentence boundary is detected, the sentence is pushed to the
        TTS queue and spoken immediately — while the LLM continues
        generating the next sentence.

        Args:
            user_input: The user's text query
            system_prompt: System prompt for the LLM
            context: Optional screen context to include

        Returns:
            Full response text (for saving to conversation memory)
        """
        full_response = ""
        buffer = ""

        # Reset cancel state for this new response
        self._cancel_event.clear()

        # Build the prompt with optional screen context
        prompt = user_input
        if context:
            prompt = f"[Screen context: {context}]\n\n{user_input}"

        try:
            from core.providers import llm_stream

            for token in llm_stream(prompt, system=system_prompt):
                # Phase 2: check cancel between tokens
                if self._cancel_event.is_set():
                    log.info("Streaming cancelled mid-generation")
                    break
                full_response += token
                buffer += token

                # Check if we have a complete sentence to speak
                chunk = self._extract_speakable_chunk(buffer)
                if chunk:
                    buffer = buffer[len(chunk):]
                    clean = self._clean_for_speech(chunk)
                    if clean:
                        log.debug("Streaming chunk to TTS: '%s...'", clean[:40])
                        self._speech_q.put(clean)

            # Speak any remaining buffer
            if buffer.strip():
                clean = self._clean_for_speech(buffer)
                if clean:
                    self._speech_q.put(clean)

        except Exception as e:
            log.error("Streaming pipeline error: %s", e)
            # Fallback: non-streaming full response
            fallback = self._non_streaming_fallback(user_input, system_prompt)
            from core.tts import speak_now
            speak_now(fallback)
            return fallback

        return full_response

    def wait_until_done(self):
        """Block until all queued speech has been spoken."""
        self._speech_q.join()

    def cancel(self):
        """
        Cancel current TTS playback and drain the queue.

        Phase 2: Called by engine.py when barge-in is detected.
        Concept: Sets the cancel event, which the TTS worker and
        respond() token loop both check. The worker drains any
        remaining queued chunks without speaking them.
        """
        self._cancel_event.set()
        # Drain the queue so task_done() counts stay balanced
        drained = 0
        while True:
            try:
                self._speech_q.get_nowait()
                self._speech_q.task_done()
                drained += 1
            except queue.Empty:
                break
        if drained:
            log.info("Cancelled TTS: drained %d queued chunks", drained)
        self._speaking = False

    # ── Sentence extraction ───────────────────────────────

    def _extract_speakable_chunk(self, buffer: str) -> str:
        """
        Extract the first complete sentence from the buffer.

        Concept: We look for sentence-ending punctuation (.!?;) followed
        by whitespace. This is a simple but effective heuristic that works
        for 95% of conversational English. For very long unpunctuated text
        (e.g., code), we force a split at 150 chars to avoid buffering
        too much before speaking.

        Returns the sentence, or empty string if no complete sentence yet.
        """
        if len(buffer) < MIN_CHUNK_LEN:
            return ""

        # Find sentence boundary
        match = SENTENCE_ENDS.search(buffer)
        if match:
            return buffer[:match.end()]

        # If buffer is very long without punctuation, speak it anyway
        if len(buffer) > 150:
            # Find last space to avoid splitting words
            last_space = buffer.rfind(" ", 0, 150)
            if last_space > MIN_CHUNK_LEN:
                return buffer[:last_space + 1]

        return ""

    # ── TTS worker ────────────────────────────────────────

    def _tts_worker(self):
        """
        Background thread: speaks chunks from the queue as they arrive.

        Concept: Runs in parallel with LLM generation. While the LLM
        is generating sentence N+1, this thread is speaking sentence N.
        Uses core.tts.speak_now() which is blocking — it generates audio
        and plays it, then returns. This ensures sentences are spoken
        in order without overlapping.

        Phase 2: Checks _cancel_event before each chunk. If set,
        discards remaining chunks instead of speaking them.
        """
        while True:
            try:
                chunk = self._speech_q.get(timeout=0.5)

                # Check cancel before speaking
                if self._cancel_event.is_set():
                    self._speech_q.task_done()
                    continue

                self._speaking = True
                from core.tts import speak_now
                speak_now(chunk)
                self._speaking = False
                self._speech_q.task_done()
            except queue.Empty:
                self._speaking = False

    # ── Cleanup ───────────────────────────────────────────

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """
        Remove markdown, code blocks, URLs, and tool-call artifacts
        before speaking.

        These elements are visual — they make no sense when read aloud.

        V5 Fix: If the streaming path accidentally receives a tool-call
        response (due to routing heuristic miss), the <tool_call> JSON
        must be stripped so Jarvis doesn't speak raw JSON or function
        names like "_call_goal" aloud.
        """
        # ── V5 safety net: strip tool-call artifacts ──
        text = re.sub(r'<tool_call>[\s\S]*?</tool_call>', '', text)
        text = re.sub(r'</?tool_call>', '', text)          # orphaned tags
        text = re.sub(r'_call_\w+', '', text)              # _call_goal etc.
        text = re.sub(r'\{[^}]*"tool"[^}]*\}', '', text)  # orphaned JSON

        # ── Original cleanup ──
        text = re.sub(r'```[\s\S]*?```', 'code block', text)
        text = re.sub(r'`[^`]+`', '', text)
        text = re.sub(r'\*+', '', text)
        text = re.sub(r'#+\s*', '', text)
        text = re.sub(r'https?://\S+', 'link', text)
        text = re.sub(r'\n+', ' ', text)
        return text.strip()

    def _non_streaming_fallback(self, user_input: str, system: str) -> str:
        """Non-streaming fallback if streaming fails."""
        try:
            from core.providers import llm_generate
            return llm_generate(user_input, system=system)
        except Exception:
            return "I had trouble generating a response."

    @property
    def is_speaking(self) -> bool:
        """True if TTS is currently speaking or has queued chunks."""
        return self._speaking or not self._speech_q.empty()
