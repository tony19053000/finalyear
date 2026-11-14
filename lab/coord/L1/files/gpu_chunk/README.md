# One gpu-big chunk (16 trainings), for LEAD T15-T17
Exactly what one MeshForge chunk runs (same as the GPU tests, chunk = 16 items):
    python run_chunk.py items.json 0 out
- items.json = [0..15] (the chunk's item list); 0 = chunk index; out = output folder (results.json written there)
- env: the job's venv = requirements.txt (torch==2.14.0 CUDA wheel via L4's extra_index_urls, numpy); no special env vars.
- With LEAD's wrapper: python g.py run_chunk.py items.json 0 out [qos]
