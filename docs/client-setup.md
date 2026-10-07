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
# or answer the dead-localhost prompt once on first start -- the
#   address is saved as the default in ~/.k0b0l-apis.json

python3 thinlizzy.py
```

That one address is all image generation needs too: the image API
(`:7860`) and the swap supervisor (`:7861`) are the same GPU box on
fixed sibling ports, so they **derive from the configured endpoint's
host**. With the harness pointed at the GPU box, `generate_image` and
`/image` run entirely over the network: when the image service is
down, the client asks the supervisor to swap the GPU, generates via
`POST /v1/images/generations`, and swaps the card back — no SSH, no
manual load/unload, no extra variables.

(`K0B0L_IMAGE_API` / `K0B0L_SWAP_API` still exist as explicit overrides
for the day the services ever live on separate boxes; normally you
never set them.)

## Notes

- The `192.168.99.38` address is DHCP — if the GPU box's IP changes,
  update `API_URL`/`K0B0L_IMAGE_API`/`K0B0L_SWAP_API`, or give the box
  a static lease.
- The saved-default mechanism (`~/.k0b0l-apis.json` on the *client*)
  is how the dead-localhost prompt's answer persists; an explicit
  `API_URL` in the environment always overrides it.
- Nothing in `~/.k0b0l-*` on the client is shared with the GPU box:
  memory, sessions, skills, and theme are all client-side state.
