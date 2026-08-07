# Credential files for device import

Place files here (or set equivalent variables in `pumpd/.env`):

| File | How to obtain |
|------|----------------|
| `tinytuya.json` | Run `python -m tinytuya wizard` on a PC; copy the generated file here |
| `devices.json` | Same wizard — lists devices with local keys and IPs |

## .env alternative

```bash
# pumpd/.env
SMARTTHINGS_PAT=your_token_here

TUYA_API_KEY=...
TUYA_API_SECRET=...
TUYA_API_REGION=us
TUYA_API_DEVICE_ID=any_device_id_from_smart_life
```

After adding credentials, restart Docker so `.env` is reloaded:

```bash
docker compose up -d
```

Then open **Admin → Import pumps → Discover devices**.

You only need **one** Tuya path (`devices.json` upload is enough for local keys). SmartThings PAT is optional but recommended for cloud fallback control.
