# Running the harness on another machine

The harness (K0B0L's `rt_harness` + `thinlizzy.py`) runs wherever you
sit; the GPU box is only an inference server. This repo is the
harness — clone it on the client machine and point it at the GPU box.

## The GPU box

One machine (currently `192.168.99.38` on the LAN, host `oVo`) serves
both modalities over HTTP — no harness code runs there:

| port | service |
| --- | --- |
| 11434 | llama.cpp router — text models on demand (OpenAI `/v1` shaped) |
| 7860 | image API (sd.cpp's sd-server) — *only while the card is swapped to images* |
| 7861 | the GPU swap supervisor — swaps the card between text and images |

All three are systemd-managed on the GPU box (`docs/gpu-services.md`)
and the firewall allows exactly those ports.

## Client setup (this repo, on the other machine)

```bash
git clone https://github.com/eldeucebag/K0B0L.git
cd K0B0L
pip install -r requirements.txt

# point the harness at the GPU box (one of):
export API_URL="http://192.168.99.38:11434/v1"     # explicit, always wins
# or answer the dead-localhost prompt once, and it is saved
#   as the default in ~/.k0b0l-apis.json

# for image generation over the API, also:
export K0B0L_IMAGE_API="http://192.168.99.38:7860"
export K0B0L_SWAP_API="http://192.168.99.38:7861"

python3 thinlizzy.py
```

With those two image variables set, `generate_image` and `/image` run
entirely over the network: when the image service is down, the client
asks the supervisor to swap the GPU (`POST /swap/image`), generates via
`POST /v1/images/generations`, and swaps the card back to the text
models — no SSH, no manual load/unload. The local-subprocess fallback
stays dormant because it would find no GPU and no weights on a client
box; the API path is the only one that ever runs there.

## Notes

- The `192.168.99.38` address is DHCP — if the GPU box's IP changes,
  update `API_URL`/`K0B0L_IMAGE_API`/`K0B0L_SWAP_API`, or give the box
  a static lease.
- The saved-default mechanism (`~/.k0b0l-apis.json` on the *client*)
  is how the dead-localhost prompt's answer persists; an explicit
  `API_URL` in the environment always overrides it.
- Nothing in `~/.k0b0l-*` on the client is shared with the GPU box:
  memory, sessions, skills, and theme are all client-side state.
