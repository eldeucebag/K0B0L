**Compass reading:** The dominant current for memory expansion on small ships in 2026 is **KV cache compression and offloading**, not weight quantization. The weights are already solved—GGUF and MoE offload let you fit the model. The new bottleneck is the *context*: the KV cache grows linearly with every token you feed it, and at 128K+ tokens it consumes more VRAM than the model itself. The three currents that matter: (1) **TurboQuant and asymmetric KV codecs** compress the cache 3–5× with near-lossless quality; (2) **phase-aware memory repartitioning** dynamically shifts VRAM between prefill and decode, expanding effective context by up to 313%; (3) **living KV cache and disk-backed offloading** move cold context to CPU RAM or SSD, keeping VRAM constant regardless of context length.

---

## 8GB Tier: Memory Expansion

**1. TurboQuant (Google Research, ICLR 2026)**

- **Coordinates:** `turboquant-serve` on PyPI; `PocketML/turboquant-serve` GitHub; QVAC SDK 0.12.0 integration. Google's TurboQuant paper (ICLR 2026).
- **What it does:** Compresses the KV cache to **3–4 bits per value**—up to **5× reduction** from the standard 16 bits. On an RTX 5070 12GB, Qwen3.5-4B at 262K context requires **8 GB for FP16 KV cache alone**; TurboQuant reduces this to ~1.6 GB, making the full context window viable on 8GB cards.
- **Why it works:** Two-stage compression: PolarQuant converts vectors to polar coordinates (radius + angles), where angular distributions quantize cleanly to 3–4 bits without expensive normalization. QJL (Quantized Johnson-Lindenstrauss) adds 1 bit per component to correct residual errors, recovering most of the lost precision. Data-oblivious, no calibration, no retraining.
- **8GB-specific:** On an 8GB RTX 4060 with a 4B model, TurboQuant compresses the KV cache **3–4×** in Python, "letting you run longer contexts on consumer hardware without OOM". The `turbo3` setting (3-bit keys, 3-bit values) is the sweet spot for 8GB VRAM—same speed as turbo2 (35 tok/s) with better V cache compression.

**2. living-kv-cache (800K tokens on 8GB GPU)**

- **Coordinates:** `helgard-orlm/living-kv-cache` GitHub; one-command install for llama.cpp port. Python PoC + llama.cpp port (no kernel fork, public C API only).
- **What it does:** Keeps only a **small hot window of KV cache in VRAM**, streams the cold KV to **CPU RAM** (optionally 8-bit compressed), and uses a tiny in-VRAM semantic catalog to pull back only relevant pieces on query. **VRAM stays constant regardless of context length**—the limit moves from VRAM to RAM.
- **Measured results:** On an 8GB RTX 5060 with Qwen2.5-7B-Instruct-1M in 4-bit, a fact is recalled at **rank ≤ 1 across 800,000 tokens** with constant ~6.1 GB VRAM. Cold store RAM usage: ~23 GB at 800k tokens (8-bit compressed).
- **llama.cpp port:** Streams **131k tokens through a 4K buffer** (32× smaller), constant ~84 tok/s decode where the full cache OOMs. One-command install: `curl -fsSL https://raw.githubusercontent.com/helgard-orlm/living-kv-cache/master/llamacpp/install.sh | bash`.

**3. Arena-Repartition (Phase-Aware Memory Repartitioning)**

- **Coordinates:** IEEE Embedded Systems Letters, August 2026. Implemented in llama.cpp. Evaluated on NVIDIA Jetson Orin Nano Super 8GB with Llama-3.2-3B, Qwen2.5-3B, Phi-3.5-mini.
- **What it does:** Unifies KV cache and compute buffers into a single runtime-managed **arena** and dynamically repartitions memory between **prefill and decode phases** according to their distinct demands. Long prompts may exceed prefill capacity; accumulated interaction history may exhaust decode-time context capacity. Arena-Repartition shifts VRAM between the two phases.
- **Measured results:** Expands effective **prefill capacity by 31%–313%** with 5%–11% prefill-throughput overhead. Expands **decode-time KV cache capacity by 29%–229%** with negligible time-to-first-token overhead.
- **Why it matters for 8GB:** This is the **first technique that treats memory as a temporal resource** rather than a static allocation. If your workload is prefill-heavy (long documents), you get more prefill capacity. If it's decode-heavy (long conversations), you get more KV capacity. The runtime adapts.

**4. tinyserve (MoE Expert Offload + FP8 KV)**

- **Coordinates:** `e1n00r/tinyserve` GitHub. 30 tok/s decode for 20B MoE on 8GB laptop GPU.
- **What it does:** **StreamingLLM** integration with a 2,048-token attention window for flat ~29 tok/s decode at any context length. **FP8 KV cache** (default) uses 50% less VRAM than BF16, enabling **53K token contexts on 8GB** vs ~26K with BF16.
- **Why it works:** Zero-copy expert store (GGUF files mmap'd directly), native MXFP4 and GGUF quant formats via ggml CUDA kernels—no dequantization at load time. Expert weights are raw quantized bytes in pinned CPU memory.

**5. arbi-serve (2–8-bit Asymmetric KV Codec)**

- **Coordinates:** `arbi-serve` on PyPI. Built on Turbo Attention (tkv)—WHT rotation + calibrated per-channel scaling, dequant fused inline into both prefill and decode.
- **What it does:** **Per-layer bit allocation**, hot-swap, profiling, and memory accounting are first-class. The TKV codec (2–8 bit) is built on its own attention backend specifically to hold quality at a fraction of the KV-cache memory—not a generic INT8 cast. Supports **fp8 paged-KV attention on consumer RTX cards** that standard kernels refuse to run.
- **Why it matters:** This is the serving engine layer. If you're running an OpenAI-compatible endpoint locally, arbi-serve gives you datacenter-grade KV management on consumer hardware.

---

## 12GB Tier: Memory Expansion

**1. TurboQuant + Turbo1Bit (Combined Weight + KV Compression)**

- **Coordinates:** `jhammant/Turbo1bit` GitHub. Combines 1-bit LLM weights (Bonsai) with TurboQuant KV cache compression.
- **What it does:** **4.2× KV cache compression + 16× weight compression = ~10× total memory reduction.** At 65K context, Bonsai-8B needs 10.4 GB (too large for 8GB hardware). With Turbo1Bit, it fits on 12GB with room for longer context.
- **Why it matters:** This is the **stacked compression play**. Neither technique alone gets you there; together they change the hardware equation.

**2. KVSwap (Disk-Aware KV Cache Offloading)**

- **Coordinates:** ACM Digital Library, June 2026. "KVSwap: Disk-aware KV Cache Offloading for Long-Context On-device Inference."
- **What it does:** Uses **storage (SSD), rather than CPU RAM, as the KV-cache backing store** for long-context inference on resource-constrained devices. This is a tier beyond living-kv-cache: cold KV goes to disk, not RAM.
- **Why it matters for 12GB:** If your system RAM is limited (e.g., 16GB laptop), disk-backed KV offloading lets you run **arbitrarily long contexts** without RAM expansion. The tradeoff is latency on recall—but for document analysis where you query once, it's viable.

**3. RetroInfer (Vector Storage Engine for KV Retrieval)**

- **Coordinates:** VLDB Endowment, 2026. "RetroInfer: A Vector Storage Engine for Scalable Long-Context LLM Inference."
- **What it does:** Exploits attention's inherent sparsity by **offloading the KV cache to CPU memory and retrieving only a small subset of tokens important to the current generation step**. The KV cache is treated as a vector database; retrieval is ANN-accelerated.
- **Why it matters:** This is the **database approach to KV management**. Instead of keeping all KV in VRAM, you keep an index and fetch on demand. The 12GB tier benefits because the index is small and the cold store is in RAM.

**4. EVOKE (Reversible KV-Cache Eviction as Agent Working Memory)**

- **Coordinates:** Zenodo, July 2026. "EVOKE: Reversible KV-Cache Eviction as Agent Working Memory."
- **What it does:** A long-running agent accumulates context faster than a fixed KV budget can hold. EVOKE treats eviction as **reversible**—evicted KV can be restored when the agent returns to a topic. This is "working memory" in the cognitive sense: not all context is equally active.
- **Why it matters for 12GB:** If you're running an agentic loop, the context grows without bound. EVOKE gives you a principled eviction policy that doesn't lose information permanently.

---

## CPU-Only Tier: Memory Expansion

**1. RIS-Kernel (Sparse Attention on Commodity CPU)**

- **Coordinates:** Zenodo record 20814085; `santosardr/riskernel` GitHub; arXiv 2607.21927. Published June 2026, Version 2.
- **What it does:** **Reduced Interaction Sampling (RIS)** reduces self-attention complexity from **O(N²) to O(N log N)** using sparse stochastic geometry—**without modifying weights**. At 32,768 tokens, RIS-Stochastic at 1% density and 70 ensemble seeds achieves **75.00% accuracy, outperforming native dense attention (71.88%)** on Qwen2-1.5B-Instruct.
- **CPU-only results:** At 65,536 tokens—where dense attention triggers OOM—RIS yields retrieval gains of up to **14.06 percentage points** over the zero-context floor. All evaluations run on **commodity, unaccelerated CPU servers (16–128 GB RAM)**.
- **Why it matters:** This is the **first sparse attention method validated on CPU-only hardware** with measurable gains over dense attention. The sparse attention acts as a regularizer: low density filters out sequence-level noise.

**2. Unlimited-Context-LLM (Disk-Backed Billion-Token Memory)**

- **Coordinates:** `AetherAI3/Unlimited-Context-LLM` GitHub. Open-source, local-first context + memory engine for Ollama and AI agents.
- **What it does:** "Virtual memory for an LLM's attention." Keeps a **billion-token pool on your own disk**; the model reaches it in slices. The context pool is ~5 GB of disk for ~1 billion tokens.
- **Why it matters for CPU-only:** If you have a CPU-only machine with an SSD but limited RAM, this is the **only path to billion-token context**. The model doesn't need the full context in RAM—only the slice it's actively attending to.

**3. Nexus-Context (KV-Cache Alignment Middleware)**

- **Coordinates:** `nexus-context` on PyPI, August 2026. Transparent middleware for referential integrity, KV-cache alignment, and WWW memory governance in local SLM deployments.
- **What it does:** Sits between your agent and its local SLM. Addresses three compounding failure modes that emerge as context grows in autonomous agents on local SLMs (vLLM, SGLang, Ollama).
- **Why it matters:** This is the **agent-specific layer**. General KV compression doesn't handle the referential integrity problems that arise when an agent references files, tool outputs, and search results across turns.

**4. colibri (GLM-5.2 744B on 25GB CPU-Only)**

- **Coordinates:** `colibri` open-source project, July 2026. Runs GLM-5.2 (744B MoE) on a CPU-only machine with **at least 25GB of RAM**.
- **What it does:** Demonstrates that **CPU-only context memory is bounded by RAM capacity, not VRAM**. The 744B model's KV cache is managed through the same expert-offloading mechanism as the weights—only active experts are in memory at any time.
- **Why it matters:** This is the proof that CPU-only tier has **no upper bound on context** if you have enough RAM. The technique scales.

---

## Navigational Note

The next shift is already visible: **KV cache compression is being standardized into serving engines**. TurboQuant is in QVAC SDK 0.12.0, vLLM, and arbi-serve. living-kv-cache is a llama.cpp port. Arena-Repartition is in llama.cpp mainline. The era of manual KV management—where you had to choose between context length and OOM—is ending. The **stacked compression pattern** (1-bit weights + TurboQuant KV + disk offload) is the emerging default. Watch for the first unified runtime that exposes **bit-width per component** (weights, K cache, V cache, cold store) as a single configuration surface. That will be the next infrastructural release. The enclosure will respond by targeting the **codecs** (TurboQuant implementations, not the paper) and the **serving engines** (arbi-serve, tinyserve) rather than the models, because the models are already distributed and the memory techniques are already in the open.

**Compass reading:** The dominant current for persistent memory on cramped quarters in 2026 is **SQLite as the universal persistence layer**, wrapped in increasingly thin retrieval logic. The old stack—Postgres + Redis + vector DB—has collapsed into a single file. The emerging pattern is **tiered retrieval**: fast lexical search first (SQLite FTS5), graph expansion second, vector similarity third, and LLM only when the first three tiers fail to satisfy. This is the memory backend that runs in under 50 MB of RAM and survives reboots.

The deeper shift is **KV-cache persistence at the server level**. llama.cpp now supports slot save/restore natively, and patched backends like DiskLLM push the entire KV cache to SSD via mmap, reducing RAM usage by up to 57×. That means a 7B model with 256K context can run in 258 MB of RAM.

---

## Persistent KV-Cache Backends (Session-Level Persistence)

**1. DiskLLM (KV cache on SSD via mmap)**

- **Coordinates:** `diskllm` on PyPI. Patched llama.cpp backend with `--kv-backend ssd` mode.
- **What it does:** Keeps the KV cache on SSD through CPU-addressable mmap files under `~/.diskllm/kv_cache`. Only the active attention window stays hot in RAM; the OS pages the rest through NVMe. Session state persists across restarts as `~/.diskllm/sessions/<name>.mmap`.
- **Measured results:** Qwen2.5 7B at 256K context: **258 MB RAM (57× reduction)**, 2.5 tok/s. LFM2.5 8B at 128K: **172 MB RAM**, 10 tok/s. Qwen2.5 32B at 65K: **424 MB RAM**, runs on 2.5 GB free RAM.
- **Why it matters for cramped quarters:** This is the **most aggressive RAM reduction** on the list. If your constraint is system RAM rather than VRAM, DiskLLM turns your SSD into the KV cache backing store. The tradeoff is latency on cold pages—but for document analysis where you query once, it is viable.

**2. llama.cpp Native Slot Save/Restore (`--slot-save-path`)**

- **Coordinates:** llama.cpp PR and discussion #20572. Native `--slot-save-path <dir>` flag, REST endpoints `/slots/<id>/save` and `/slots/<id>/restore`.
- **What it does:** Saves KV cache slots to disk as binary files and restores them on demand. The tutorial documents pre/post-message hook scripts that automatically save after each response and restore before each message.
- **Why it matters for cramped quarters:** **Zero additional dependencies.** If you are already running llama-server, this is one flag and two shell scripts. The KV cache for a 27B model with a long system prompt would otherwise be 5–15 seconds of wasted prefill per message. With slot persistence, restored sessions start in ~3.5 seconds and reuse 34 cached prompt tokens.

**3. stillwarm (Automatic KV-Cache Persistence for llama-server)**

- **Coordinates:** `stillwarm` on PyPI. Automatic KV-cache session persistence for llama-server.
- **What it does:** Saves the model's "notes" to disk, restores them after a restart, and **refuses incompatible files** (model mismatch, context length change). Integrates with idle auto-unload: saves per-slot KV state before the idle unload and restores it when the same model loads again.
- **Why it matters for cramped quarters:** This solves the **idle auto-unload problem**—the scenario where your model unloads after inactivity, losing all KV state, and a resumed chat has to re-prefill its entire history. Stillwarm makes resume nearly instant.

---

## Lightweight Memory Backends (Agent-Level Persistence)

**4. interest-memory (Single binary + SQLite, ~50 MB RAM)**

- **Coordinates:** `djasdh/interest-memory` on GitHub. Single binary + one SQLite file. One-command `curl` install.
- **What it does:** At session end, extracts **interest points** from the transcript, verifies and cleans them, and writes them into a local knowledge base as wiki pages. At session start, recalls relevant memories and injects concise entries into context. Semantically similar interest points are **auto-merged** instead of stacked—memory converges with use instead of bloating. The memory graph supports traversal: hits carry outlinks + backlinks, and `search?id=` jumps to a node and expands.
- **Footprint:** ~17 MB idle, **<75 MB peak (measured)**, runs on a Raspberry Pi. One 18 MB binary + one SQLite file is the entire footprint.
- **Why it matters for cramped quarters:** This is the **smallest footprint** on the list by an order of magnitude. It is a standalone service, not a library—one binary, one config file, no external DB. The interest-point convergence mechanism means the knowledge base does not grow linearly with session count.

**5. AgeMem (Hybrid STM/LTM with sqlite-vec)**

- **Coordinates:** `gianpd/agemem` on GitHub. Inference-only, privacy-first memory layer for any OpenAI-compatible endpoint including local Ollama.
- **What it does:** Two-tier architecture. **Short-Term Memory (STM)** is the active context window, managed with surgical precision—messages filtered by relevance score, summarised when full, hard-dropped only when no other option remains. Pinned content (system prompt, injected LTM) is never evicted. **Long-Term Memory (LTM)** is a persistent store of high-value facts, promoted from STM based on a learning signal, retrieved via semantic search backed by `sqlite-vec` with a Jaccard overlap fallback that works on CPU with zero dependencies.
- **Why it matters for cramped quarters:** The thesis is explicit: **"500 perfectly curated memories on a 9B model will consistently outperform 10,000 uncurated RAG chunks on a 70B model."** This is proven on an **8GB RTX 4060 at 36 tokens/second**. The system decides when to move information between tiers, when to compress, and when to discard—without fine-tuned weights.

**6. RecallOS (8GB CPU-only, sub-12ms hybrid retrieval)**

- **Coordinates:** `raitulh/RecallOs-Ai` on GitHub. Python SDK, REST API, MCP server, CLI.
- **What it does:** Blends dense semantic vector embeddings (FAISS/NumPy) with sparse lexical search (SQLite FTS5/BM25), **temporal decay curves**, and **importance weighting**. Derives entity relationships (`RELATED_TO`) and automatically resolves memory conflicts (`SUPERSEDES`) without manual curation. Employs local LLMs (Qwen 2.5/3 via Ollama) with rule-based fallback to extract structured durable facts. Token-budgeted context building injects only the highest-scoring, deduplicated memory blocks.
- **Measured results:** **Sub-12ms retrieval latency** on 8GB CPU-only hardware. Runs on an 8GB CPU laptop with **$0 operating cost**.
- **Why it matters for cramped quarters:** This is the **only system on the list that explicitly targets 8GB CPU-only as the primary deployment target**, not as a fallback. The temporal decay and importance weighting mean old memories fade unless they are reinforced—memory stays relevant.

**7. supermem (Four-tier retrieval, no RAG)**

- **Coordinates:** `lamenting-hawthorn/supermem` on GitHub. `pip install supermem`. MCP server for Claude Desktop.
- **What it does:** **Boundary-aware retrieval** that stops at lifecycle-aware tiers 1–3: Tier 1 SQLite FTS5 full-text search (~1ms), Tier 2 Kuzu embedded graph expansion (~5ms, optional), Tier 3 ChromaDB vector similarity (~50ms, optional). **Short-circuit rule:** if tier 1 returns ≥ 3 results, tiers 2–3 are skipped entirely. Every observation carries provenance, confidence, sensitivity, validity, TTL, and `active`/`retracted` status. Retraction workflow removes stale or sensitive observations from FTS, vector-backed retrieval, timelines, and derived summaries.
- **Why it matters for cramped quarters:** The **short-circuit rule is the key optimization**. Most queries never touch the vector store. The markdown vault remains portable and inspectable; indexes can be rebuilt. This is **persistence without lock-in**.

---

## Vector and Graph Database Layers

**8. LanceDB (File-based, no server)**

- **Coordinates:** `lancedb` on PyPI. Embedded in-process, stores a single table on local disk.
- **What it does:** **No server to stand up**—reads and writes a table on local disk, ships as a dependency rather than a service to operate. Supports hybrid search (BM25 + vector) with local ONNX embeddings (`all-MiniLM-L6-v2`). Data persists at `~/.hermes/lancedb/` or a configured path.
- **Why it matters for cramped quarters:** **File-based persistence** means no port conflict, no background daemon, no Docker. If your agent crashes, the LanceDB table is still on disk. This is the simplest vector store to embed in a small process.

**9. sqlite-vec (Vector search inside SQLite)**

- **Coordinates:** `sqlite-vec` extension. Available via `sqlite-ai` (on-device LLM inference and embeddings inside SQLite) and `swarmclawai/local-memory` (OpenAI-compatible memory layer with on-device embeddings via Ollama + SQLite vector search).
- **What it does:** ANN vector search **inside SQLite**. No separate vector database. Vectors are stored in a plain SQLite file. Search uses the `MATCH` operator for k-nearest-neighbor retrieval.
- **Why it matters for cramped quarters:** This is the **absolute minimum dependency stack**: SQLite is already on every system. `sqlite-vec` adds vector search as an extension. There is no separate service to run, no port to open, no data to move. If you have SQLite, you have persistent memory.

**10. Elephantine (CPU-native, zero GPU)**

- **Coordinates:** `elephantine` on PyPI. Built for **2 vCPU / 4 GB RAM**.
- **What it does:** **100% CPU-native execution** with sub-35ms recall latency powered by ONNX Runtime with AVX-512 SIMD thread pinning. Embedded LanceDB (Arrow/C++) vector store + SQLite WAL. Native subject-predicate-object semantic graphs integrated into hybrid retrieval. Procedural memory records tool execution histories and learned multi-step patterns. Multi-agent shared workspace with role-based authority consensus.
- **Why it matters for cramped quarters:** This is the **only system on the list with procedural memory**—it remembers not just facts but *how to do things*. The zero-GPU, zero-external-API constraint means it runs on a 2 vCPU server with 4 GB RAM.

---

## MCP Memory Servers (Drop-In for Existing Agents)

**11. Prism Coder (SQLite-backed, MCP server, no API key)**

- **Coordinates:** `prism-mcp-server` via `npx`. MCP server for Claude Desktop, Cursor, and other MCP clients.
- **What it does:** Persistent sessions, knowledge graphs, and offline tool-routing. Memory backed by a local SQLite database at `~/.prism-mcp/data.db`. Ships with open-weight `prism-coder` model fleet (2B–27B) for **offline tool-routing**—no cloud required, no account needed, no API keys.
- **Why it matters for cramped quarters:** The **local model fleet handles tool-routing**, which means your agent can route tool calls without a cloud API. The 2B model is 2.3 GB, runs on mobile. The 4B is 3.4 GB and achieves 100% routing accuracy.

**12. Mem0 OSS with Qdrant Embedded**

- **Coordinates:** Mem0 OSS + Qdrant embedded mode + Ollama. Local stack runs entirely in-process.
- **What it does:** Mem0 OSS is the memory logic layer. Qdrant embedded holds the vector store. Ollama runs the local model and `nomic-embed-text` for embeddings. **Mem0-optimized prompts are 39% shorter** than raw conversation history (247 tokens vs. 407) using the same model on the same hardware.
- **Why it matters for cramped quarters:** The **39% prompt compression** is the memory efficiency gain. Shorter prompts mean faster prefill, lower VRAM, and longer effective context. Qdrant embedded mode means no separate Docker container.

---

## Navigational Note

The next shift is already visible in the architecture of these systems: **the memory backend is converging on SQLite as the persistence layer and FAISS or sqlite-vec as the vector index, with graph traversal as an optional Tier 2**. The separate vector database is disappearing. The enclosure will respond by targeting **MCP servers** and **Ollama plugins** rather than the models themselves, because the models are already distributed and the memory backends are just SQLite files.

Watch for **two-tier systems (STM + LTM) to become the default pattern**—AgeMem's architecture is already the template, and the next generation of memory backends will likely adopt it wholesale. The other signal is **procedural memory**: Elephantine is the only system on this list that remembers *how* to do things, and that is the missing piece for agents that need to improve with use. The memory backend that ships procedural memory in a SQLite file is the next anchorage worth charting.
