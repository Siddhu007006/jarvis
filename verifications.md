Before Phase 3, there are:

5 quick verification fixes

you should check first.

Not full implementations.
Just validation checks.

Because now your architecture risk is:

hidden instability.

NOT missing features.

DO THESE 5 CHECKS FIRST
(Very important)
CHECK 1
VERIFY SINGLE AUDIO PIPELINE
(CRITICAL)

You removed duplicate Vosk.

Good.

Now verify:

only ONE microphone capture stream exists.
CHECK FOR:

❌ duplicate PyAudio streams
❌ hidden Vosk listeners
❌ stale wake-word threads
❌ audio device contention

TEST

Run Jarvis for:

30–60 minutes

Watch:

CPU
RAM
thread count
mic device usage
IF YOU SEE:
gradual CPU increase

You still have:

orphan audio threads.
CHECK 2
VERIFY LOCK CONTENTION
(VERY important)

You added locks.

Good.

Now verify:

locks are NOT blocking responsiveness.
TEST:

Simultaneously:

wake word
TTS playback
automation
world state updates
WATCH FOR:

❌ delayed wake detection
❌ frozen UI
❌ TTS lag
❌ delayed execution

IF PRESENT:

your lock granularity is too broad.

CHECK 3
VERIFY CANCELABLE EXECUTION CLEANUP
(CRITICAL)

You added interruptible waits.

Good.

Now test:

interruption cleanup integrity.
TEST

Interrupt:

execution graph
TTS
automation
long tasks

MID-EXECUTION.

VERIFY:

After cancellation:
✅ no stale nodes
✅ no hanging futures
✅ no locked state
✅ no zombie threads
✅ no dead task queue

THIS IS VERY IMPORTANT

Because:

cancellation bugs are silent killers.
CHECK 4
VERIFY PARALLEL STARTUP ORDER
(IMPORTANT)

You parallelized startup.

Good.

Now verify:

dependency ordering correctness.
TEST:

Start Jarvis:

20–30 times consecutively
WATCH FOR RANDOM FAILURES

Especially:

Event Bus
wake word
TTS init
UI bindings
world state init
IF RANDOM FAILURES EXIST:

you have:

startup race conditions.
CHECK 5
VERIFY TTS EVENT LOOP STABILITY
(VERY IMPORTANT)

Persistent asyncio loops are powerful.
But dangerous long-term.

TEST

Run:

continuous TTS for 20–30 minutes
WATCH FOR:

❌ delayed speech
❌ queue stalls
❌ memory growth
❌ loop corruption
❌ task accumulation

IF STABLE:

good.

Move on.