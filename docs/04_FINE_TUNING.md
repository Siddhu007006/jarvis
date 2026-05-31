# Jarvis Local Model Tuning

This repo now supports a safe two-step path:

1. Immediate local behavior tuning with an Ollama Modelfile.
2. Later LoRA fine-tuning once a GGUF model is exported from Colab.

## Create the tuned local model

```powershell
powershell -ExecutionPolicy Bypass -File scripts/create_jarvis_model.ps1 -ModelName jarvis
```

To switch the active ignored `config/settings.json` to the new model too:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/create_jarvis_model.ps1 -ModelName jarvis -SwitchSettings
```

The Modelfile keeps Jarvis concise, direct, Windows-aware, and tuned for short structured responses.

## Generate seed training data

```powershell
python scripts/generate_training_data.py --out training_data
```

This creates four JSONL files:

- `tool_calling.jsonl`
- `planning.jsonl`
- `conversation.jsonl`
- `proactive.jsonl`

The current generator is deterministic and safe to run offline. A teacher-model expansion step can build on this format later.

## Benchmark local models

```powershell
python scripts/benchmark_jarvis.py --models qwen2.5-coder:3b jarvis
```

The benchmark checks:

- tool-routing accuracy
- restraint on conversational prompts
- planner JSON parseability
- average latency

## Fine-tuned GGUF deployment

After exporting a merged GGUF from Colab, place it at:

```text
config/jarvis-qwen2.5-coder-3b-finetuned-Q4_K_M.gguf
```

Then create the Ollama model:

```powershell
ollama create jarvis-finetuned -f config/Modelfile.jarvis-finetuned
```

Keep the stock model or the Modelfile-only `jarvis` model available as a fallback until benchmarks show the fine-tuned model is better.
